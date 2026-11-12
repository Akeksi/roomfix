"""Automatic measurement: level finding, sweeps and timing.

Safety rules enforced here:
* every signal for a driver with a lower limit passes a 4th-order high-pass at
  that limit, so a tweeter never receives low frequencies;
* levels start very low and rise only until the microphone hears enough;
* nothing louder than the configured maximum level is ever played.

Timing: each sweep is preceded by a short chirp from one fixed reference
driver. All impulse responses are stored relative to the arrival of that
chirp, which makes delays comparable between drivers without a loopback cable.
"""
import numpy as np
from scipy import signal

from . import config, dsp

PRE = 0.02          # seconds kept before the reference arrival in each IR
IR_LEN = 1 << 15
START_DB = -54.0
TARGET_SNR = 35.0


class Cancelled(Exception):
    pass


def drivers(cfg):
    """All drivers with their protection filter and usable sweep range."""
    out = []
    multi = len(cfg["ways"]) > 1
    for si, spk in enumerate(cfg["speakers"]):
        for wi, way in enumerate(cfg["ways"]):
            low = way.get("min_freq", 0) if wi > 0 else 0
            out.append({
                "key": f"s{si}w{wi}", "si": si, "wi": wi, "channel": spk["channels"][wi],
                "name": f"{spk['name']} {way['name'].lower()}" if multi else spk["name"],
                "protect": dsp.butterworth4("Highpass", low) if low else [],
                "f1": low / 3 if low else 15.0, "f2": 22000.0,
            })
    return out


def protect_response(cfg, wi, f):
    low = cfg["ways"][wi].get("min_freq", 0) if wi > 0 else 0
    if not low:
        return np.ones(len(f), complex)
    return dsp.response(dsp.butterworth4("Highpass", low), cfg["sample_rate"], f)


class Measurer:
    def __init__(self, cfg, audio, progress=None, prompt=None):
        errors = config.validate(cfg)
        if errors:
            raise RuntimeError(" ".join(errors))
        self.cfg, self.audio, self.fs = cfg, audio, cfg["sample_rate"]
        self.progress = progress or (lambda frac, msg, active=None: None)
        self.prompt = prompt or (lambda msg: True)
        self.drivers = drivers(cfg)
        self.n_out = max(d["channel"] for d in self.drivers) + 1
        self.limit = 10 ** (cfg["max_level_db"] / 20)
        self.ref = [d for d in self.drivers if d["si"] == 0][-1]  # top driver of first speaker
        lo = max(2000.0, 2 * (cfg["ways"][-1].get("min_freq") or 0))
        self.probe = dsp.log_sweep(self.fs, lo, min(4 * lo, 16000.0), 0.1)
        self.ambient = None

    # ---- low level -------------------------------------------------------
    def _play(self, out, extra=1.0):
        peak = np.max(np.abs(out)) if len(out) else 0.0
        if peak > self.limit * 1.0001:
            raise RuntimeError("Safety stop: test signal above the maximum level.")
        return self.audio.playrec(out, extra)

    def _driver_signal(self, drv, x, amp):
        """Signal for one driver: band-protected, scaled, never above the limit."""
        y = dsp.apply(drv["protect"], self.fs, x) * amp
        scale = min(1.0, self.limit / max(np.max(np.abs(y)), 1e-12))
        return y * scale, amp * scale

    def _band_rms(self, x, band):
        sos = signal.butter(4, band, "bandpass", fs=self.fs, output="sos")
        y = signal.sosfilt(sos, x)
        return np.sqrt(np.mean(y[len(y) // 5:] ** 2))

    def _auto_level(self, drv, band, start_db, target_snr=TARGET_SNR):
        """Raise the level in 6 dB steps until the mic hears the driver well."""
        if self.ambient is None:
            self.ambient = self._play(np.zeros((int(0.4 * self.fs), self.n_out)), extra=0.2)
        noise = self._band_rms(self.ambient, band)
        burst = dsp.log_sweep(self.fs, band[0], band[1], 0.4)
        level = max(start_db, START_DB)
        while True:
            level = min(level, self.cfg["max_level_db"])
            out = np.zeros((len(burst), self.n_out))
            out[:, drv["channel"]] = self._driver_signal(drv, burst, 10 ** (level / 20))[0]
            rec = self._play(out, extra=0.5)
            snr = dsp.db(self._band_rms(rec, band) / noise)
            if np.max(np.abs(rec)) > 0.7:
                return level - 6, snr - 6
            if snr >= target_snr or level >= self.cfg["max_level_db"]:
                return level, snr
            level += 6 if snr > target_snr - 12 else 12

    def _band(self, drv):
        lo = max(drv["f1"] * 3, 100.0)
        return [lo, min(max(8 * lo, 3000.0), 12000.0)]

    # ---- steps -----------------------------------------------------------
    def identify(self):
        """Wiring check: a hiss that is safe for any driver, one driver at a time."""
        lo = max(2000.0, 2 * max(w.get("min_freq") or 0 for w in self.cfg["ways"]))
        band = [lo, min(3 * lo, 16000.0)]
        rng = np.random.default_rng(0)
        sos = signal.butter(6, band, "bandpass", fs=self.fs, output="sos")
        noise = signal.sosfilt(sos, rng.standard_normal(int(1.2 * self.fs)))
        noise = dsp.fade(noise / np.max(np.abs(noise)), self.fs, 0.1, 0.1)
        for i, drv in enumerate(self.drivers):
            self.progress(i / len(self.drivers), f"Now playing: {drv['name']}", drv["key"])
            safe = dict(drv, protect=dsp.butterworth4("Highpass", lo))
            level, snr = self._auto_level(safe, band, START_DB, target_snr=25.0)
            if snr < 10:
                raise RuntimeError(f"No sound detected from {drv['name']} (DAC channel "
                                   f"{drv['channel'] + 1}). Check cables, amplifier and microphone.")
            out = np.zeros((len(noise), self.n_out))
            out[:, drv["channel"]] = noise * min(10 ** (level / 20), self.limit)
            self._play(out, extra=0.3)

    def find_levels(self):
        levels, start = {}, START_DB
        for i, drv in enumerate(self.drivers):
            self.progress(0.1 * i / len(self.drivers), f"Setting level: {drv['name']}", drv["key"])
            level, snr = self._auto_level(drv, self._band(drv), start)
            if snr < 15:
                raise RuntimeError(f"No sound detected from {drv['name']} (DAC channel "
                                   f"{drv['channel'] + 1}). Check cables, amplifier volume and microphone.")
            levels[drv["key"]] = level
            start = min(levels.values()) - 12
        return levels

    def _capture(self, sweep, placed, ref_amp):
        """Play reference chirp + sweep signals, return the IR relative to the reference.

        placed: list of (channel, signal) of the same length as `sweep`.
        """
        fs = self.fs
        lead, gap = int(0.05 * fs), int(0.3 * fs)
        start = lead + len(self.probe) + gap
        out = np.zeros((start + len(sweep) + int(0.3 * fs), self.n_out))
        chirp = self._driver_signal(self.ref, self.probe, ref_amp)[0]
        out[lead:lead + len(chirp), self.ref["channel"]] = chirp
        for ch, sig in placed:
            out[start:start + len(sweep), ch] += sig
        rec = self._play(out)
        t_ref, snr = dsp.find_arrival(rec[:start + int(0.5 * fs)], self.probe)
        if snr < 12:
            raise RuntimeError("Timing reference not detected. Reduce background noise or "
                               "raise the amplifier volume.")
        s0 = int(round(t_ref)) + (start - lead) - int(PRE * fs)
        n_seg = len(sweep) + IR_LEN
        seg = rec[max(s0, 0):s0 + n_seg]
        if s0 < 0 or len(seg) < n_seg - fs // 10:
            raise RuntimeError("Recording misaligned; audio latency is too high.")
        n = 1 << int(np.ceil(np.log2(n_seg)))
        h = dsp.deconvolve(seg, sweep, n)
        h *= np.exp(-2j * np.pi * np.fft.rfftfreq(n) * (round(t_ref) - t_ref))
        ir = np.fft.irfft(h, n)[:IR_LEN]
        ir[-IR_LEN // 8:] *= np.linspace(1, 0, IR_LEN // 8) ** 2
        return ir, float(np.max(np.abs(rec)))

    def measure(self):
        """Measure every driver at every microphone position."""
        fs, n_pos = self.fs, self.cfg["positions"]
        levels = self.find_levels()
        ref_amp = 10 ** (levels[self.ref["key"]] / 20)
        meas = {"fs": fs, "levels": levels, "irs": {d["key"]: [] for d in self.drivers}, "warnings": []}
        total, step = n_pos * len(self.drivers), 0
        for pos in range(n_pos):
            if pos > 0 and not self.prompt(
                    f"Move the microphone to position {pos + 1} of {n_pos} "
                    "(30-50 cm away from the previous one), then continue."):
                raise Cancelled()
            if hasattr(self.audio, "position"):
                self.audio.position = pos
            for drv in self.drivers:
                self.progress(0.1 + 0.9 * step / total, f"Measuring {drv['name']}"
                              + (f" (position {pos + 1}/{n_pos})" if n_pos > 1 else ""), drv["key"])
                octaves = np.log2(drv["f2"] / drv["f1"])
                sweep = dsp.log_sweep(fs, drv["f1"], drv["f2"], max(1.5, 0.35 * octaves))
                sig, amp = self._driver_signal(drv, sweep, 10 ** (levels[drv["key"]] / 20))
                ir, peak = self._capture(sweep, [(drv["channel"], sig)], ref_amp)
                ir /= amp
                noise = np.sqrt(np.mean(ir[:int(PRE * fs / 2)] ** 2))
                if peak > 0.95:
                    meas["warnings"].append(f"{drv['name']}: microphone clipped.")
                elif dsp.db(np.max(np.abs(ir)) / noise) < 25:
                    meas["warnings"].append(f"{drv['name']}: noisy measurement.")
                meas["irs"][drv["key"]].append(ir.astype(np.float32))
                step += 1
        self.progress(1.0, "Measurement finished")
        return meas

    def verify(self, design, levels):
        """Measure each speaker through the complete correction, at position 1."""
        from .design import render
        fs = self.fs
        ref_amp = 10 ** (levels[self.ref["key"]] / 20)
        sweep = dsp.log_sweep(fs, 15.0, 22000.0, 3.7)
        irs = []
        if hasattr(self.audio, "position"):
            self.audio.position = 0
        for si, spk in enumerate(design["speakers"]):
            self.progress(si / len(design["speakers"]), f"Verifying {spk['name']}", f"s{si}w0")
            for way, cfg_way in zip(spk["ways"], self.cfg["ways"]):
                hp = [f["freq"] for f in way["filters"] if f["type"].startswith("Highpass")]
                if cfg_way.get("min_freq") and way is not spk["ways"][0] and (
                        not hp or min(hp) < cfg_way["min_freq"]):
                    raise RuntimeError("Safety stop: correction lacks the driver protection filter.")
            amp = 10 ** (min(levels[d["key"]] for d in self.drivers if d["si"] == si) / 20)
            rendered = render(spk, sweep * amp, fs)
            scale = min(1.0, self.limit / max(np.max(np.abs(rendered)), 1e-12))
            placed = [(way["channel"], rendered[:, wi] * scale) for wi, way in enumerate(spk["ways"])]
            ir, _ = self._capture(sweep, placed, ref_amp)
            irs.append((ir / (amp * scale)).astype(np.float32))
        self.progress(1.0, "Verification finished")
        return irs
