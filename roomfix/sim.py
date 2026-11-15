"""Simulated speakers, room and microphone (demo mode and tests).

Mimics the real audio backend: play a multichannel signal, get the microphone
recording back, with unknown latency, room reflections, modes and noise.
"""
import time

import numpy as np
from scipy import signal

from . import config, dsp, engine

N_IR = 1 << 14


def _delay(ir, samples):
    f = np.fft.rfftfreq(len(ir))
    return np.fft.irfft(np.fft.rfft(ir) * np.exp(-2j * np.pi * f * samples), len(ir))


class SimAudio:
    simulated = True

    def __init__(self, cfg, seed=1, noise_db=-75.0, realtime=False, flipped=(), engine_latency_ms=(150.0, 70.0),
                 windows_volume_db=(0.0, 0.0), rumble_db=None):
        self.rumble = 10 ** (rumble_db / 20) if rumble_db is not None else 0.0  # room noise below 35 Hz
        # Windows volume of the DAC and of the subwoofer's device: applies to test signals
        # played directly (shared mode), not to the playback engine (exclusive mode)
        self.windows_volume = tuple(10 ** (v / 20) for v in windows_volume_db)
        self.flipped = set(flipped)  # (speaker, way) pairs wired with reversed polarity
        self.realtime = realtime
        self.fs = cfg["sample_rate"]
        self.cfg = cfg
        self.noise = 10 ** (noise_db / 20)
        self.rng = np.random.default_rng(seed)
        self.position = 0
        self.history = []  # every signal ever played on the DAC, for safety tests
        self.history_other = []  # ... and on the subwoofer's own output device
        # latency of the playback engine + device: the DAC's, the subwoofer device's
        self.engine_latency_ms = tuple(engine_latency_ms)
        self._irs = {}

    def _driver(self, way, n_ways):
        """(filters, sensitivity dB, acoustic offset ms) of a plausible driver."""
        hp, lp = dsp.butterworth4, lambda f: [{"type": "Lowpass", "freq": f, "q": 0.6}]
        if n_ways == 1:
            return [{"type": "Highpass", "freq": 48, "q": 0.8}] + lp(19000), 0.0, 0.0
        if way == 0:
            top = 3500 if n_ways == 2 else 900
            return [{"type": "Highpass", "freq": 42, "q": 0.8}] + lp(top), 0.0, 0.25
        if way == n_ways - 1:
            return [{"type": "Highpass", "freq": 1100, "q": 0.9}] + lp(21000), 4.0, 0.0
        return [{"type": "Highpass", "freq": 280, "q": 0.7}] + lp(6000), 2.0, 0.1

    def _ir(self, si, wi, pos):
        key = (si, wi, pos)
        if key in self._irs:
            return self._irs[key]
        fs, n_ways = self.fs, len(self.cfg["ways"])
        if si == "sub":  # a subwoofer a little further away than the speakers
            filters, sens, offset_ms = [{"type": "Highpass", "freq": 25, "q": 0.8},
                                        {"type": "Lowpass", "freq": 200, "q": 0.6}], 6.0, 0.0
            si = 2
        else:
            filters, sens, offset_ms = self._driver(wi, n_ways)
        rng = np.random.default_rng(1000 * si + pos)  # same room for all ways of a speaker
        # room modes: different per speaker, slightly different per mic position
        modes = [(43, 9, 7), (68, 6, 6), (97, -7, 5), (128, 5, 5), (171, -4, 4), (236, 3, 4)]
        for f0, g, q in modes:
            filters = filters + [{"type": "Peaking", "freq": f0 * (1 + 0.04 * si), "q": q,
                                  "gain": g * (1 - 0.25 * si) * rng.uniform(0.7, 1.1)}]
        x = np.zeros(N_IR)
        x[0] = 10 ** ((sens - 1.5 * si) / 20) * (-1 if (si, wi) in self.flipped else 1)
        direct = dsp.apply(filters, fs, x)
        dist_ms = (2.8 + 0.3 * si) / 343 * 1000 + rng.uniform(0, 0.15) * (pos > 0)
        ir = _delay(direct, (dist_ms + offset_ms) * fs / 1000)
        for _ in range(6):  # early reflections
            refl = signal.lfilter([0.35], [1, -0.65], direct) * rng.uniform(0.15, 0.4) * rng.choice([-1, 1])
            ir += _delay(refl, (dist_ms + offset_ms + rng.uniform(1.5, 25)) * fs / 1000)
        t = np.arange(N_IR) / fs  # diffuse tail
        tail = rng.standard_normal(N_IR) * np.exp(-t / 0.05) * 0.004 * (t > dist_ms / 1000)
        ir += dsp.apply(filters[:3], fs, tail)
        self._irs[key] = ir
        return ir

    def _noise(self, n):
        x = self.noise * self.rng.standard_normal(n)
        if self.rumble:
            sos = signal.butter(4, [12, 35], "bandpass", fs=self.fs, output="sos")
            low = signal.sosfilt(sos, self.rng.standard_normal(n + self.fs))[self.fs:]
            x += self.rumble * low / np.sqrt(np.mean(low ** 2))
        return x

    def device_channels(self):
        return {}

    def _drivers(self, device):
        """(ir key, channel) of everything connected to an output device."""
        sub, other = config.sub(self.cfg), config.sub_device(self.cfg)
        found = []
        if device in (None, self.cfg["output_device"]):
            found = [((si, wi), ch) for si, spk in enumerate(self.cfg["speakers"]) for wi, ch in enumerate(spk["channels"])]
        if sub and (other or self.cfg["output_device"]) == (device or self.cfg["output_device"]):
            found.append((("sub", 0), sub["channel"]))
        return found

    def _mic(self, out, device, rec, latency):
        for (si, wi), ch in self._drivers(device):
            if ch < out.shape[1] and np.any(out[:, ch]):
                y = signal.fftconvolve(out[:, ch], self._ir(si, wi, self.position))
                m = min(len(y), len(rec) - latency)
                rec[latency:latency + m] += y[:m]

    def playrec(self, out, extra=0.6, device=None):
        out = np.asarray(out, float)
        if self.realtime:
            time.sleep(len(out) / self.fs / 3)
        other = device not in (None, self.cfg["output_device"])
        (self.history_other if other else self.history).append(out.copy())
        rec = np.zeros(len(out) + int(extra * self.fs))
        self._mic(out * self.windows_volume[other], device, rec, int(self.rng.integers(1500, 6000)))
        return rec + self._noise(len(rec))

    def system_playrec(self, configs, outs, extra=1.0, settle=False):
        """Play through simulated playback engines, one per output device.

        Each engine adds its own latency. One that has just started (settle=False) still
        holds a random surplus in its buffer, which it reports like the real engine does.
        """
        surplus = {dev: 0 if settle else int(self.rng.integers(-250, 250)) for dev, _ in configs}
        recs = []
        for out in outs:
            if self.realtime:
                time.sleep(len(out) / self.fs / 3)
            rec = np.zeros(len(out) + int(extra * self.fs))
            cable = int(self.rng.integers(500, 2000))
            for i, (dev, conf) in enumerate(configs):
                played = engine.simulate(conf, out, self.fs)
                (self.history_other if i else self.history).append(played.copy())
                self._mic(played, dev, rec, cable + int(self.engine_latency_ms[i] * self.fs / 1000) + surplus[dev])
            recs.append(rec + self._noise(len(rec)))
        return recs, {dev: engine.TARGET_LEVEL + extra_samples for dev, extra_samples in surplus.items()}
