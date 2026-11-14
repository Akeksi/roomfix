"""Automatic measurement: level finding, sweeps and timing.

Safety rules enforced here:
* every signal for a driver with a lower limit passes a 4th-order high-pass at
  that limit, so a tweeter never receives low frequencies;
* levels start very low and rise only until the microphone hears enough;
* nothing louder than the configured maximum level is ever played (the
  subwoofer can have its own, lower maximum).

Timing: each sweep is preceded by a short chirp from one fixed reference
driver. All impulse responses are stored relative to the arrival of that
chirp, which makes delays comparable between drivers without a loopback cable.

A subwoofer on a second output device cannot be timed that way: two devices do
not start together and the playback engine adds a different latency to each.
Its response is measured on its own, and its delay is then measured through
the playback engines themselves (`_sync`): chirp and subwoofer sweep enter the
system audio path together, exactly as music will.
"""
import numpy as np
from scipy import signal

from . import config, dsp, engine

PRE = 0.02          # seconds kept before the reference arrival in each IR
IR_LEN = 1 << 15
START_DB = -54.0
TARGET_SNR = 35.0


class Cancelled(Exception):
    pass


def drivers(cfg):
    """All drivers with their protection filter, level limit and usable sweep range."""
    out = []
    multi = len(cfg["ways"]) > 1
    for si, spk in enumerate(cfg["speakers"]):
        for wi, way in enumerate(cfg["ways"]):
            low = way.get("min_freq", 0) if wi > 0 else 0
            out.append({
                "key": f"s{si}w{wi}", "si": si, "wi": wi, "channel": spk["channels"][wi], "device": None,
                "name": f"{spk['name']} {way['name'].lower()}" if multi else spk["name"],
                "protect": dsp.butterworth4("Highpass", low) if low else [],
                "f1": low / 3 if low else 15.0, "f2": 22000.0, "max_db": cfg["max_level_db"],
            })
    sub = config.sub(cfg)
    if sub:  # last, so that the speakers are measured first
        xo = float(sub["crossover"])
        out.append({
            "key": "sub", "si": None, "wi": 0, "channel": sub["channel"], "device": config.sub_device(cfg),
            "name": "Subwoofer", "protect": [], "f1": 15.0, "f2": min(12 * xo, 2000.0), "sub": True,
            "band": [xo / 2, xo * 2],
            "max_db": cfg["max_level_db"] if sub["max_level_db"] is None else sub["max_level_db"],
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
        self.sub = next((d for d in self.drivers if d.get("sub")), None)
        self.n_out = {}  # channels needed per output device (None = the DAC)
        for d in self.drivers:
            self.n_out[d["device"]] = max(self.n_out.get(d["device"], 0), d["channel"] + 1)
        self.limit = 10 ** (max(d["max_db"] for d in self.drivers) / 20)
        self.ref = [d for d in self.drivers if d["si"] == 0][-1]  # top driver of first speaker
        lo = max(2000.0, 2 * (cfg["ways"][-1].get("min_freq") or 0))
        self.probe = dsp.log_sweep(self.fs, lo, min(4 * lo, 16000.0), 0.1)
        self.ambient = None

    # ---- low level -------------------------------------------------------
    def _play(self, out, extra=1.0, device=None):
        peak = np.max(np.abs(out)) if len(out) else 0.0
        if peak > self.limit * 1.0001:
            raise RuntimeError("Safety stop: test signal above the maximum level.")
        return self.audio.playrec(out, extra, device) if device else self.audio.playrec(out, extra)

    def _driver_signal(self, drv, x, amp):
        """Signal for one driver: band-protected, scaled, never above the driver's limit."""
        y = dsp.apply(drv["protect"], self.fs, x) * amp
        scale = min(1.0, 10 ** (drv["max_db"] / 20) / max(np.max(np.abs(y)), 1e-12))
        return y * scale, amp * scale

    def _band_rms(self, x, band):
        sos = signal.butter(4, band, "bandpass", fs=self.fs, output="sos")
        y = signal.sosfilt(sos, x)
        return np.sqrt(np.mean(y[len(y) // 5:] ** 2))

    def _auto_level(self, drv, band, start_db, target_snr=TARGET_SNR):
        """Raise the level in 6 dB steps until the mic hears the driver well."""
        if self.ambient is None:
            self.ambient = self._play(np.zeros((int(0.4 * self.fs), self.n_out[None])), extra=0.2)
        noise = self._band_rms(self.ambient, band)
        burst = dsp.log_sweep(self.fs, band[0], band[1], 1.0 if drv.get("sub") else 0.4)
        level = max(start_db, START_DB)
        while True:
            level = min(level, drv["max_db"])
            out = np.zeros((len(burst), self.n_out[drv["device"]]))
            out[:, drv["channel"]] = self._driver_signal(drv, burst, 10 ** (level / 20))[0]
            rec = self._play(out, 0.5, drv["device"])
            snr = dsp.db(self._band_rms(rec, band) / noise)
            if np.max(np.abs(rec)) > 0.7:
                return level - 6, snr - 6
            if snr >= target_snr or level >= drv["max_db"]:
                return level, snr
            level += 6 if snr > target_snr - 12 else 12

    def _band(self, drv):
        if drv.get("sub"):
            return drv["band"]
        lo = max(drv["f1"] * 3, 100.0)
        return [lo, min(max(8 * lo, 3000.0), 12000.0)]

    def _silent(self, drv):
        return RuntimeError(f"No sound detected from {drv['name']} (DAC channel {drv['channel'] + 1}). "
                            "Check cables, amplifier volume and microphone.")

    # ---- steps -----------------------------------------------------------
    def identify(self):
        """Wiring check: a hiss that is safe for any driver (a rumble for the subwoofer)."""
        lo = max(2000.0, 2 * max(w.get("min_freq") or 0 for w in self.cfg["ways"]))
        rng = np.random.default_rng(0)
        for i, drv in enumerate(self.drivers):
            self.progress(i / len(self.drivers), f"Now playing: {drv['name']}", drv["key"])
            band = drv["band"] if drv.get("sub") else [lo, min(3 * lo, 16000.0)]
            sos = signal.butter(6, band, "bandpass", fs=self.fs, output="sos")
            noise = signal.sosfilt(sos, rng.standard_normal(int(1.2 * self.fs)))
            noise = dsp.fade(noise / np.max(np.abs(noise)), self.fs, 0.1, 0.1)
            safe = drv if drv.get("sub") else dict(drv, protect=dsp.butterworth4("Highpass", lo))
            level, snr = self._auto_level(safe, band, START_DB, target_snr=25.0)
            if snr < 10:
                raise self._silent(drv)
            out = np.zeros((len(noise), self.n_out[drv["device"]]))
            out[:, drv["channel"]] = noise * 10 ** (min(level, drv["max_db"]) / 20)
            self._play(out, 0.3, drv["device"])

    def find_levels(self):
        levels, start = {}, START_DB
        for i, drv in enumerate(self.drivers):
            self.progress(0.1 * i / len(self.drivers), f"Setting level: {drv['name']}", drv["key"])
            # a subwoofer has its own amplifier: start from the bottom again; room rumble
            # limits what can be heard of it, the long sweep copes with less
            sub = drv.get("sub")
            level, snr = self._auto_level(drv, self._band(drv), START_DB if sub else start,
                                          25.0 if sub else TARGET_SNR)
            if snr < (10 if sub else 15):
                raise self._silent(drv)
            levels[drv["key"]] = level
            if not sub:
                start = min(levels.values()) - 12
        return levels

    def _taper(self, ir):
        ir = ir[:IR_LEN].copy()
        ir[-IR_LEN // 8:] *= np.linspace(1, 0, IR_LEN // 8) ** 2
        return ir

    def _capture(self, sweep, placed, ref_amp):
        """Play reference chirp + sweep signals, return the IR relative to the reference.

        placed: list of (channel, signal) of the same length as `sweep`.
        """
        fs = self.fs
        lead, gap = int(0.05 * fs), int(0.3 * fs)
        start = lead + len(self.probe) + gap
        out = np.zeros((start + len(sweep) + int(0.3 * fs), self.n_out[None]))
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
        return self._taper(np.fft.irfft(h, n)), float(np.max(np.abs(rec)))

    def _peak_ir(self, rec, sweep, start, band=None):
        """IR from a recording without timing reference: its strongest arrival is put at PRE.

        band: where the driver plays; the arrival is searched there only, so that rumble
        or hiss elsewhere cannot be mistaken for it.
        Returns the IR, the delay (samples after the sweep started) of its first sample,
        and the signal-to-noise ratio in dB.
        """
        n = 1 << int(np.ceil(np.log2(len(rec) + IR_LEN)))
        h = np.fft.irfft(dsp.deconvolve(rec, np.concatenate([np.zeros(start), sweep]), n), n)
        seen = h
        if band:
            sos = signal.butter(2, band, "bandpass", fs=self.fs, output="sos")
            seen = signal.sosfiltfilt(sos, h)
        k = int(np.argmax(np.abs(seen[:len(rec) - start])))
        idle = np.roll(seen, -k)[n // 2:n - self.fs // 2]  # long after the arrival, well before it
        snr = float(dsp.db(np.abs(seen[k]) / np.sqrt(np.mean(idle ** 2))))
        s0 = k - int(PRE * self.fs)
        return self._taper(np.roll(h, -s0)), s0, snr

    def _capture_alone(self, drv, sweep, sig):
        """Measure a driver on a second output device: no reference, timing comes from `_sync`."""
        lead = int(0.1 * self.fs)
        out = np.zeros((lead + len(sweep) + int(0.3 * self.fs), self.n_out[drv["device"]]))
        out[lead:lead + len(sweep), drv["channel"]] = sig
        rec = self._play(out, 1.0, drv["device"])
        ir, _, snr = self._peak_ir(rec, sweep, lead, drv.get("band"))
        return ir, float(np.max(np.abs(rec))), snr

    def _sync(self, levels, direct_ir):
        """Delay of the subwoofer's device relative to the speakers' device, through the
        playback engines. Returns where `direct_ir` belongs on the common time axis
        (samples to add to its index)."""
        fs, sub, cfg = self.fs, self.sub, self.cfg
        sweep = dsp.log_sweep(fs, 20.0, 2 * sub["band"][1], 4.0)
        lead = int(0.3 * fs)
        start = lead + len(self.probe) + int(0.3 * fs)
        out = np.zeros((start + len(sweep) + int(0.3 * fs), 2))
        # full-scale into the system audio path; the engines attenuate to the test levels,
        # so that nothing else arriving on that path can be louder than a test signal
        out[lead:lead + len(self.probe), 0] = self.probe * 0.9
        out[start:start + len(sweep), 1] = sweep * 0.9
        configs = engine.sync_configs(cfg, self.ref, sub, levels[self.ref["key"]], levels["sub"],
                                      self.audio.device_channels())
        recs, buffers = self.audio.system_playrec(configs, [out], 1.0)
        t_ref, snr = dsp.find_arrival(recs[0], self.probe)
        if snr < 12:
            raise RuntimeError("Timing reference not detected through the system audio path. Check that "
                               "the system audio source in Setup is the virtual cable, that it is the "
                               "default Windows output and that nothing else is playing.")
        ir, s0, _ = self._peak_ir(recs[0], sweep, start, sub["band"])
        sos = signal.butter(2, sub["band"], "bandpass", fs=fs, output="sos")
        a, b = signal.sosfiltfilt(sos, ir), signal.sosfiltfilt(sos, direct_ir)
        corr = signal.correlate(a, b, "full")
        k = int(np.argmax(corr))
        if corr[k] < 0.5 * np.sqrt(np.sum(a * a) * np.sum(b * b)):
            raise RuntimeError("Could not measure the delay between the two output devices. Reduce "
                               "background noise or allow a higher subwoofer test level.")
        offset = s0 + (k - (len(b) - 1)) - (t_ref - lead) + int(PRE * fs)
        # an engine that has just started has not yet settled its buffer; what it holds
        # too much now is latency that will be gone in normal use
        main, other = buffers.get(cfg["output_device"]), buffers.get(sub["device"])
        if main is None or other is None:
            raise RuntimeError("The playback engine did not report its buffer level.")
        return float(offset - (other - main))

    def measure(self):
        """Measure every driver at every microphone position."""
        fs, n_pos = self.fs, self.cfg["positions"]
        levels = self.find_levels()
        ref_amp = 10 ** (levels[self.ref["key"]] / 20)
        meas = {"fs": fs, "levels": levels, "irs": {d["key"]: [] for d in self.drivers}, "warnings": [],
                "offsets": {}}
        total, step = n_pos * len(self.drivers), 0
        for pos in range(n_pos):
            if pos > 0 and not self.prompt(
                    f"Move the microphone to position {pos + 1} of {n_pos} "
                    "(30-50 cm away from the previous one), then continue."):
                raise Cancelled()
            if hasattr(self.audio, "position"):
                self.audio.position = pos
            for drv in self.drivers:
                where = f" (position {pos + 1}/{n_pos})" if n_pos > 1 else ""
                self.progress(0.1 + 0.9 * step / total, f"Measuring {drv['name']}" + where, drv["key"])
                octaves = np.log2(drv["f2"] / drv["f1"])
                # bass needs time: few cycles per octave otherwise, and the room rumbles
                sweep = dsp.log_sweep(fs, drv["f1"], drv["f2"],
                                      max(1.5, (0.8 if drv.get("sub") else 0.35) * octaves))
                sig, amp = self._driver_signal(drv, sweep, 10 ** (levels[drv["key"]] / 20))
                if drv["device"]:
                    ir, peak, snr = self._capture_alone(drv, sweep, sig)
                else:
                    ir, peak = self._capture(sweep, [(drv["channel"], sig)], ref_amp)
                    # (a subwoofer's slow rise fills the time before its peak: no noise to read there)
                    snr = 99.0 if drv.get("sub") else dsp.db(np.max(np.abs(ir)) / np.sqrt(np.mean(ir[:int(PRE * fs / 2)] ** 2)))
                ir /= amp
                if peak > 0.95:
                    meas["warnings"].append(f"{drv['name']}: microphone clipped.")
                elif snr < 25:
                    meas["warnings"].append(f"{drv['name']}: noisy measurement.")
                meas["irs"][drv["key"]].append(ir.astype(np.float32))
                if drv["device"]:
                    self.progress(0.1 + 0.9 * (step + 0.7) / total,
                                  "Measuring the delay between the output devices" + where, drv["key"])
                    meas["offsets"].setdefault(drv["key"], []).append(self._sync(levels, ir))
                step += 1
        self.progress(1.0, "Measurement finished")
        return meas

    def verify(self, design, levels):
        """Measure each speaker through the complete correction, at position 1."""
        from .design import render, render_sub
        fs, sub = self.fs, design.get("sub")
        ref_amp = 10 ** (levels[self.ref["key"]] / 20)
        sweep = dsp.log_sweep(fs, 15.0, 22000.0, 3.7)
        system = bool(sub and sub["device"])  # two devices: only the engines can play them in sync
        irs, outs, gains = [], [], []
        if hasattr(self.audio, "position"):
            self.audio.position = 0
        for si, spk in enumerate(design["speakers"]):
            for way, cfg_way in zip(spk["ways"], self.cfg["ways"]):
                hp = [f["freq"] for f in way["filters"] if f["type"].startswith("Highpass")]
                if cfg_way.get("min_freq") and way is not spk["ways"][0] and (
                        not hp or min(hp) < cfg_way["min_freq"]):
                    raise RuntimeError("Safety stop: correction lacks the driver protection filter.")
            amp = 10 ** (min(levels[d["key"]] for d in self.drivers if d["si"] == si) / 20)
            rendered = render(spk, sweep * amp, fs)
            scale = min(1.0, 10 ** (self.cfg["max_level_db"] / 20) / max(np.max(np.abs(rendered)), 1e-12))
            placed = [(way["channel"], rendered[:, wi] * scale) for wi, way in enumerate(spk["ways"])]
            if sub:
                bass = render_sub(design, si, sweep * amp, fs)
                scale2 = min(1.0, 10 ** (self.sub["max_db"] / 20) / max(np.max(np.abs(bass)) * scale, 1e-12))
                placed = [(ch, sig * scale2) for ch, sig in placed] + [(sub["channel"], bass * scale * scale2)]
                scale *= scale2
            gains.append(amp * scale)
            if system:
                lead = int(0.3 * fs)
                out = np.zeros((lead + len(sweep) + int(0.3 * fs), max(s["input"] for s in design["speakers"]) + 1))
                out[lead:lead + len(sweep), spk["input"]] = sweep * amp * scale
                outs.append(out)
            else:
                self.progress(si / len(design["speakers"]), f"Verifying {spk['name']}", f"s{si}w0")
                irs.append((self._capture(sweep, placed, ref_amp)[0] / gains[-1]).astype(np.float32))
        if system:
            self.progress(0.0, "Starting the playback engines and letting them settle")
            configs = engine.camilladsp_configs(self.cfg, design, self.audio.device_channels())
            recs, _ = self.audio.system_playrec(configs, outs, 1.0, settle=True)
            irs = [(self._peak_ir(rec, sweep, int(0.3 * fs))[0] / g).astype(np.float32)
                   for rec, g in zip(recs, gains)]
        self.progress(1.0, "Verification finished")
        return irs
