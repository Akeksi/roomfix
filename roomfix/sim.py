"""Simulated speakers, room and microphone (demo mode and tests).

Mimics the real audio backend: play a multichannel signal, get the microphone
recording back, with unknown latency, room reflections, modes and noise.
"""
import time

import numpy as np
from scipy import signal

from . import dsp

N_IR = 1 << 14


def _delay(ir, samples):
    f = np.fft.rfftfreq(len(ir))
    return np.fft.irfft(np.fft.rfft(ir) * np.exp(-2j * np.pi * f * samples), len(ir))


class SimAudio:
    simulated = True

    def __init__(self, cfg, seed=1, noise_db=-75.0, realtime=False, flipped=()):
        self.flipped = set(flipped)  # (speaker, way) pairs wired with reversed polarity
        self.realtime = realtime
        self.fs = cfg["sample_rate"]
        self.cfg = cfg
        self.noise = 10 ** (noise_db / 20)
        self.rng = np.random.default_rng(seed)
        self.position = 0
        self.history = []  # every signal ever played, for safety tests
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

    def playrec(self, out, extra=0.6):
        out = np.asarray(out, float)
        if self.realtime:
            time.sleep(len(out) / self.fs / 3)
        self.history.append(out.copy())
        latency = int(self.rng.integers(1500, 6000))
        rec = np.zeros(len(out) + int(extra * self.fs))
        for si, spk in enumerate(self.cfg["speakers"]):
            for wi, ch in enumerate(spk["channels"]):
                if ch < out.shape[1] and np.any(out[:, ch]):
                    y = signal.fftconvolve(out[:, ch], self._ir(si, wi, self.position))
                    m = min(len(y), len(rec) - latency)
                    rec[latency:latency + m] += y[:m]
        return rec + self.noise * self.rng.standard_normal(len(rec))
