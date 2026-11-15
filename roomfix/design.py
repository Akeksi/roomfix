"""Turn measurements into a correction: crossover, alignment, levels and EQ.

The result ("design") fully describes the processing per speaker:

    input -> eq (peaking filters) -> gain -> per way: crossover -> gain/polarity -> delay -> DAC channel

A subwoofer is one more, shared way below all speakers: every speaker is
high-passed at the subwoofer crossover and the subwoofer plays the low-passed
sum of them, level-matched, delayed and with the polarity that makes it add up
with the speakers. It may sit on another output device with another latency.

`render` runs a signal through exactly this chain, `export` writes it out for
CamillaDSP or Equalizer APO.
"""
import numpy as np
from scipy import signal
from scipy.optimize import least_squares

from . import config, dsp
from .measure import PRE, protect_response

NFFT = 1 << 16
GRID = dsp.log_grid(20.0, 20000.0, 24)


# ---- processing chain ----------------------------------------------------
def crossover_filters(cfg, wi):
    xo, n, out = cfg["crossover"], len(cfg["ways"]), []
    if wi == 0 and config.sub(cfg):
        out += dsp.linkwitz_riley("Highpass", float(config.sub(cfg)["crossover"]), "LR4")
    if wi > 0:
        out += dsp.linkwitz_riley("Highpass", xo["freqs"][wi - 1], xo["slope"])
    if wi < n - 1:
        out += dsp.linkwitz_riley("Lowpass", xo["freqs"][wi], xo["slope"])
    return out


def render(spk, x, fs):
    """Run mono signal x through a speaker's chain -> array [samples, ways]."""
    y = dsp.apply(spk["eq"], fs, x) * 10 ** (spk["gain"] / 20)
    out = np.zeros((len(x), len(spk["ways"])))
    for wi, way in enumerate(spk["ways"]):
        z = dsp.apply(way["filters"], fs, y) * 10 ** (way["gain"] / 20) * (-1 if way["invert"] else 1)
        d = way["delay"]
        out[d:, wi] = z[:len(z) - d]
    return out


def render_sub(design, si, x, fs):
    """What the subwoofer plays of speaker si's input signal x."""
    spk, sub = design["speakers"][si], design["sub"]
    y = dsp.apply(spk["eq"], fs, x) * 10 ** ((spk["gain"] + sub["gains"][si]) / 20) * (-1 if sub["invert"] else 1)
    y = dsp.apply(sub["filters"], fs, y)
    return np.concatenate([np.zeros(sub["delay"]), y[:len(y) - sub["delay"]]])


def way_response(way, fs, f):
    h = dsp.response(way["filters"], fs, f) * 10 ** (way["gain"] / 20) * (-1 if way["invert"] else 1)
    return h * np.exp(-2j * np.pi * f * way["delay"] / fs)


# ---- target --------------------------------------------------------------
def target_curve(f, t):
    """Target shape in dB (0 dB at 1 kHz before custom points)."""
    curve = t["tilt"] * np.log2(f / 1000.0)
    curve = curve + t["bass_boost"] / (1 + (f / max(t["bass_freq"], 1.0)) ** 2)
    pts = sorted(p for p in t.get("points") or [] if p[0] > 0)
    if len(pts) >= 2:
        pf, pg = np.array(pts).T
        curve = curve + np.interp(np.log(f), np.log(pf), pg)
    return curve


# ---- EQ fitting ----------------------------------------------------------
def _peq_db(params, fs, f):
    total = np.zeros(len(f))
    for lf, g, lq in np.reshape(params, (-1, 3)):
        total += dsp.db(dsp.response([{"type": "Peaking", "freq": 2 ** lf, "q": 2 ** lq, "gain": g}], fs, f))
    return total


def fit_peq(f, wanted, fs, n, f_lo, f_hi, max_boost, max_cut):
    """Peaking filters whose combined response approximates `wanted` (dB) on grid f."""
    mask = (f >= f_lo) & (f <= f_hi)
    if not mask.any():
        return []
    weight = np.where(mask, 1.0, 0.5)

    def cost(p):
        return (_peq_db(p, fs, f) - wanted) * weight

    params, lo, hi = np.zeros(0), [], []
    for _ in range(n):
        resid = np.where(mask, wanted - _peq_db(params, fs, f), 0.0)
        k = int(np.argmax(np.abs(resid)))
        if abs(resid[k]) < 1.0:
            break
        a = b = k  # width of the bump at half height
        while a > 0 and resid[a - 1] * resid[k] > 0 and abs(resid[a - 1]) > abs(resid[k]) / 2:
            a -= 1
        while b < len(f) - 1 and resid[b + 1] * resid[k] > 0 and abs(resid[b + 1]) > abs(resid[k]) / 2:
            b += 1
        bw = max(np.log2(f[b] / f[a]), 0.1)
        # narrow filters only in the bass, where the room response is stable across the seat
        q_max = float(np.interp(np.log(f[k]), np.log([150.0, 1000.0]), [10.0, 3.0]))
        lo += [max(np.log2(f[k]) - 0.5, np.log2(f_lo)), -max_cut, np.log2(0.4)]
        hi += [min(np.log2(f[k]) + 0.5, np.log2(f_hi)), max_boost, np.log2(q_max)]
        new = [np.log2(f[k]), resid[k], np.log2(np.sqrt(2 ** bw) / (2 ** bw - 1))]
        params = np.concatenate([params, np.clip(new, np.array(lo[-3:]) + 1e-6, np.array(hi[-3:]) - 1e-6)])
    if not len(params):
        return []
    fit = least_squares(cost, params, bounds=(lo, hi), max_nfev=40)
    filters = [{"type": "Peaking", "freq": round(float(2 ** lf), 1), "q": round(float(2 ** lq), 2),
                "gain": round(float(g), 1)} for lf, g, lq in fit.x.reshape(-1, 3)]
    return sorted((x for x in filters if abs(x["gain"]) >= 0.5), key=lambda x: x["freq"])


# ---- crossover alignment ---------------------------------------------------
def _gate(ir, fs, length):
    """Keep the direct sound: from 2 ms before the envelope peak for `length` seconds."""
    k = int(dsp.envelope_peak(ir))
    a, b, fade = max(k - int(0.002 * fs), 0), k + int(length * fs), int(length * fs / 2)
    win = np.zeros(len(ir))
    win[a:b] = 1.0
    ramp = 0.5 + 0.5 * np.cos(np.pi * np.arange(fade) / fade)
    end = min(b + fade, len(ir))
    win[b:end] = ramp[:end - b]
    return ir * win


def align(lower, upper, f, fs, fc, hint=None):
    """Delay (samples, >0 delays the upper way) and polarity flip that make two
    crossover-filtered drivers add up best around fc.

    Within the crossover band a reversed driver looks almost the same as half a
    period of delay, so the delay is first located from the envelope of the
    cross-correlation (where both drivers arrive together), and the polarity is
    then read from its phase at that point. `hint` is the (delay, flip) found
    for the first speaker: the others search around the same delay, so that
    identical speakers get identical crossovers and only a genuinely reversed
    driver changes polarity.
    """
    band = (f > fc / 2) & (f < fc * 2)
    cross = lower[band] * np.conj(upper[band]) / f[band]
    span = int(max(0.002, 1.5 / fc) * fs)
    lags = np.arange(-span, span + 1)
    corr = np.exp(2j * np.pi * np.outer(lags, f[band]) / fs) @ cross
    if hint is None:
        k = int(np.argmax(np.abs(corr)))
        flip = bool(corr[k].real < 0)
        half = max(int(fs / fc / 2), 1)
        near = slice(max(k - half, 0), k + half + 1)
    else:
        k, half = hint[0] + span, max(int(fs / fc / 4), 1)
        near = slice(max(k - half, 0), k + half + 1)
        flip = bool(-corr.real[near].min() > corr.real[near].max())
    r = -corr.real if flip else corr.real
    return int(lags[near][np.argmax(r[near])]), flip


def _mean_db(curve, lo, hi):
    if hi < lo * 1.2:
        lo, hi = lo / 1.3, hi * 1.3
    sel = (GRID >= lo) & (GRID <= hi)
    return float(np.mean(curve[sel])) if sel.any() else float(np.mean(curve))


def _smooth_db(f, power, octaves):
    return 10 * np.log10(np.maximum(dsp.smooth(f, power, GRID, octaves), 1e-20))


FINE = np.interp(np.log(GRID), np.log([100.0, 1000.0]), [1 / 12, 1 / 3])  # smoothing width, octaves


# ---- main ----------------------------------------------------------------
def design(cfg, meas, cal=None, verify_irs=None):
    fs, ways, n_ways = meas["fs"], cfg["ways"], len(cfg["ways"])
    f = np.fft.rfftfreq(NFFT, 1 / fs)
    f[0] = 1e-3
    mic = dsp.calibration_gain(cal, f)
    tgt = cfg["target"]
    shape = target_curve(GRID, tgt)

    def raw(key, wi, ir):
        p = protect_response(cfg, wi, f)
        return np.fft.rfft(ir, NFFT) / mic * np.conj(p) / (np.abs(p) ** 2 + 1e-4)

    speakers, work, hints, notes = [], [], {}, []
    for si, spk in enumerate(cfg["speakers"]):
        keys = [f"s{si}w{wi}" for wi in range(n_ways)]
        n_pos = len(meas["irs"][keys[0]])
        resp = [[raw(k, wi, ir) for ir in meas["irs"][k]] for wi, k in enumerate(keys)]
        xo = [dsp.response(crossover_filters(cfg, wi), fs, f) for wi in range(n_ways)]
        drv_db = [_smooth_db(f, np.mean([np.abs(h) ** 2 for h in r], axis=0), 1 / 6) for r in resp]

        # driver levels: attenuate the louder drivers to match the quietest
        gains, delays, inverts = [0.0] * n_ways, [0] * n_ways, [False] * n_ways
        if n_ways > 1:
            fx = cfg["crossover"]["freqs"]
            lv = [_mean_db(drv_db[wi], fx[wi - 1] * 1.25 if wi else 100.0,
                           fx[wi] / 1.25 if wi < n_ways - 1 else 10000.0) for wi in range(n_ways)]
            gains = [float(np.clip(round(min(lv) - x, 1), -24, 0)) for x in lv]
            # time alignment and polarity, pairwise from the bottom up (direct sound, position 1)
            for wi in range(1, n_ways):
                fc = fx[wi - 1]
                length = float(np.clip(4 / fc, 0.004, 0.04))
                a = raw(keys[wi - 1], wi - 1, _gate(meas["irs"][keys[wi - 1]][0], fs, length)) * xo[wi - 1]
                b = raw(keys[wi], wi, _gate(meas["irs"][keys[wi]][0], fs, length)) * xo[wi]
                d, flip = align(a * 10 ** (gains[wi - 1] / 20), b * 10 ** (gains[wi] / 20), f, fs, fc,
                                hints[wi] if si else None)
                if not si:
                    hints[wi] = (d, flip)
                delays[wi] = delays[wi - 1] + d
                inverts[wi] = inverts[wi - 1] != flip
            delays = [d - min(delays) for d in delays]

        chain = [xo[wi] * 10 ** (gains[wi] / 20) * (-1 if inverts[wi] else 1)
                 * np.exp(-2j * np.pi * f * delays[wi] / fs) for wi in range(n_ways)]
        total = [sum(resp[wi][p] * chain[wi] for wi in range(n_ways)) for p in range(n_pos)]
        power = np.mean([np.abs(t) ** 2 for t in total], axis=0)

        # arrival time of the whole speaker (position 1, 500 Hz - 8 kHz)
        win = np.clip(np.minimum(np.log2(f / 350) / 0.5, np.log2(11000 / f) / 0.5), 0, 1)
        arrival = dsp.envelope_peak(np.fft.irfft(total[0] * win, NFFT))

        fine, third = _smooth_db(f, power, FINE), _smooth_db(f, power, 1 / 3)
        coarse = _smooth_db(f, power, 1 / 2)
        # usable range of the speaker: where the octave-smoothed level is within 6 dB of the midband
        wide = _smooth_db(f, power, 1.0)
        ok = np.where(wide > np.median(third[(GRID >= 200) & (GRID <= 5000)]) - 6)[0]
        f_lo = tgt["f_min"] or GRID[ok[0]]
        f_hi = tgt["f_max"] or GRID[ok[-1]]
        sel = (GRID >= max(2 * f_lo, 200)) & (GRID <= min(f_hi / 2, 6000))
        if not sel.any():
            sel = (GRID >= f_lo) & (GRID <= f_hi)
        offset = float(np.mean((fine - shape)[sel]))
        # a whole speaker wired in reverse shows up as a negative correlation with the first one
        if si == 0:
            first = total[0]
        else:
            band = (f > 200) & (f < 2000)
            lags = np.arange(-int(0.01 * fs), int(0.01 * fs) + 1)
            corr = np.exp(2j * np.pi * np.outer(lags, f[band]) / fs) @ (first[band] * np.conj(total[0][band]))
            if corr[np.argmax(np.abs(corr))].real < 0:
                inverts = [not x for x in inverts]
                notes.append(f"{spk['name']} is connected with reversed polarity; this is corrected digitally.")
        work.append(dict(fine=fine, coarse=coarse, f_lo=float(f_lo), f_hi=float(f_hi), offset=offset,
                         arrival=arrival, drv_db=drv_db, xo=xo, gains=gains, total=total))
        speakers.append({
            "name": spk["name"], "input": spk["input"], "eq": [], "gain": 0.0,
            "ways": [{"name": ways[wi]["name"], "channel": spk["channels"][wi],
                      "filters": crossover_filters(cfg, wi), "gain": gains[wi],
                      "invert": inverts[wi], "delay": int(delays[wi])} for wi in range(n_ways)],
        })

    # speakers: equal level and equal arrival time at the microphone
    common = min(w["offset"] for w in work)
    latest = max(w["arrival"] for w in work)
    for spk, w in zip(speakers, work):
        spk["delay"] = int(round(latest - w["arrival"]))

    # subwoofer: level, delay and polarity against all speakers playing together
    sub, wait = None, 0
    if config.sub(cfg):
        fc = float(config.sub(cfg)["crossover"])
        lp = dsp.linkwitz_riley("Lowpass", fc, "LR4")
        h_lp = dsp.response(lp, fs, f)
        offsets = meas.get("offsets", {}).get("sub") or [0.0] * len(meas["irs"]["sub"])
        # (on its own device, the subwoofer's level relative to the speakers is the one
        # measured through the playback engines, like its delay)
        gains = meas.get("gains", {}).get("sub") or [0.0] * len(offsets)
        hs = [np.fft.rfft(ir, NFFT) / mic * np.exp(-2j * np.pi * f * off / fs) * 10 ** (gain / 20)
              for ir, off, gain in zip(meas["irs"]["sub"], offsets, gains)]
        sub_db = _smooth_db(f, np.mean([np.abs(h) ** 2 for h in hs], axis=0), 1 / 6)
        sub_offset = _mean_db(sub_db - shape, fc / 2.5, fc)
        upper = sum(w["total"][0] * np.exp(-2j * np.pi * f * spk["delay"] / fs) * 10 ** ((common - w["offset"]) / 20)
                    for spk, w in zip(speakers, work))
        lower = hs[0] * h_lp * 10 ** ((common - sub_offset) / 20) * len(speakers)
        # The devices may be far apart in time: find the bulk delay first, then the exact one.
        # Bass rings on in a room, so the two correlate at many delays, tens of milliseconds
        # apart. The right one is next to where the sound actually arrives: the subwoofer's
        # bass against the direct sound of the speakers.
        band = (f > fc / 2) & (f < fc * 2)
        cross, seen = np.zeros(len(f), complex), np.zeros(len(f), complex)
        cross[band] = lower[band] * np.conj(upper[band])
        seen[band] = lower[band]
        t_sub = dsp.envelope_peak(np.fft.irfft(seen, NFFT))
        first = int(round((t_sub if t_sub < NFFT // 2 else t_sub - NFFT) - latest))
        near = first + np.arange(-int(fs / fc), int(fs / fc) + 1)
        bulk = int(near[np.argmax(np.abs(signal.hilbert(np.fft.irfft(cross, NFFT)))[near % NFFT])])
        d, flip = align(lower * np.exp(2j * np.pi * f * bulk / fs), upper, f, fs, fc)
        late = bulk + d  # > 0: the subwoofer arrives later, so the speakers must wait for it
        wait = max(late, 0)
        sub = {"name": "Subwoofer", "channel": config.sub(cfg)["channel"], "device": config.sub_device(cfg),
               "filters": lp, "gains": [round(w["offset"] - sub_offset, 1) for w in work],
               "invert": bool(flip), "delay": int(max(-late, 0))}
        # room noise (rumble) must not pass for deep bass: the subwoofer is only corrected
        # down to where it is clearly above the noise floor of its measurement
        floor = 0.0
        if meas["irs"].get("noise_sub") is not None and len(meas["irs"]["noise_sub"]):
            noise_db = _smooth_db(f, np.mean([np.abs(np.fft.rfft(n, NFFT) / mic * 10 ** (g / 20)) ** 2
                                              for n, g in zip(meas["irs"]["noise_sub"], gains)], axis=0), 1 / 3)
            hidden = np.where((_smooth_db(f, np.mean([np.abs(h) ** 2 for h in hs], axis=0), 1 / 3) < noise_db + 10)
                              & (GRID < fc))[0]
            if len(hidden) and hidden[-1] + 1 < len(GRID):
                floor = float(GRID[hidden[-1] + 1])
                if floor > 30:
                    notes.append(f"Room noise is louder than the subwoofer below {floor:.0f} Hz, so nothing is "
                                 "corrected there. For deeper correction, measure when it is quieter or allow "
                                 "a higher subwoofer test level.")
        if sub["delay"] > 0.25 * fs or wait > 0.25 * fs:
            notes.append("The delay between the output devices is unusually long; sound will lag behind video.")
        for spk, w, gain in zip(speakers, work, sub["gains"]):
            rel = sub["delay"] - spk["delay"] - wait
            term = [h * h_lp * 10 ** (gain / 20) * (-1 if flip else 1) * np.exp(-2j * np.pi * f * rel / fs) for h in hs]
            power = np.mean([np.abs(t + b) ** 2 for t, b in zip(w["total"], term)], axis=0)
            w["bass"] = {"speaker": w["fine"], "sub": _smooth_db(f, np.mean([np.abs(b) ** 2 for b in term], axis=0), FINE)}
            w["fine"], w["coarse"] = _smooth_db(f, power, FINE), _smooth_db(f, power, 1 / 2)
            third, wide = _smooth_db(f, power, 1 / 3), _smooth_db(f, power, 1.0)
            ok = np.where(wide > np.median(third[(GRID >= 200) & (GRID <= 5000)]) - 6)[0]
            w["f_lo"] = float(tgt["f_min"] or max(GRID[ok[0]], floor))

    curves, eq_peak = [], 0.0
    for spk, w in zip(speakers, work):
        for way in spk["ways"]:
            way["delay"] += spk["delay"] + wait
        want_fine = shape + w["offset"] - w["fine"]
        want_coarse = shape + w["offset"] - w["coarse"]
        # cut peaks precisely, but fill only broad dips (narrow dips are cancellations)
        wanted = np.where(want_fine < 0, want_fine, np.clip(np.minimum(want_fine, want_coarse), 0, None))
        wanted = np.clip(wanted, -tgt["max_cut"], tgt["max_boost"])
        # fade the correction out towards the limits of the speaker; boosts fade over a
        # full octave at the bottom so the EQ never pushes a woofer below its range
        up, down = np.log2(GRID / w["f_lo"]), np.log2(w["f_hi"] / GRID)
        edge = np.clip(np.minimum(np.where(wanted > 0, up, up * 2), down * 2) + 0.01, 0, 1)
        spk["eq"] = fit_peq(GRID, wanted * edge, fs, int(tgt["n_filters"]), w["f_lo"], w["f_hi"],
                            tgt["max_boost"], tgt["max_cut"])
        w["eq_db"] = dsp.db(dsp.response(spk["eq"], fs, GRID))
        w["trim"] = common - w["offset"]
        eq_peak = max(eq_peak, float(np.max(w["eq_db"])) + w["trim"])
    headroom = max(eq_peak, 0.0)  # keep the digital signal from clipping
    if sub:  # the subwoofer adds up all speakers' bass
        summed = float(dsp.db(sum(10 ** ((np.max(w["eq_db"]) + w["trim"] + g) / 20)
                                  for w, g in zip(work, sub["gains"]))))
        if summed > headroom + 3:
            notes.append(f"The subwoofer needs a high digital level, which costs {summed - headroom:.0f} dB of "
                         "overall volume. Turn up the subwoofer's own volume and measure again.")
        headroom = max(headroom, summed)
    for si, (spk, w) in enumerate(zip(speakers, work)):
        spk["gain"] = round(w["trim"] - headroom, 1)
        c = {
            "name": spk["name"],
            "measured": w["fine"] - common,
            "predicted": w["fine"] + w["eq_db"] + w["trim"] - common,
            "range": [round(w["f_lo"]), round(w["f_hi"])],
            "drivers": [],
        }
        if n_ways > 1:
            for wi, way in enumerate(spk["ways"]):
                low = (ways[wi].get("min_freq") or 0) / 2.5 if wi else 0
                rawc = np.where(GRID >= low, w["drv_db"][wi] - common, np.nan)
                c["drivers"].append({"name": way["name"], "raw": rawc,
                                     "filtered": rawc + dsp.db(dsp.response(way["filters"], fs, GRID)) + way["gain"]})
        if sub:
            c["bass"] = {k: v - common for k, v in w["bass"].items()}
        if verify_irs is not None and si < len(verify_irs):
            h = np.fft.rfft(verify_irs[si], NFFT) / mic
            c["verified"] = _smooth_db(f, np.abs(h) ** 2, FINE) - common + headroom
            win = np.clip(np.minimum(np.log2(f / 350) / 0.5, np.log2(11000 / f) / 0.5), 0, 1)
            c["verified_arrival_ms"] = round((dsp.envelope_peak(np.fft.irfft(h * win, NFFT)) / fs - PRE) * 1000, 3)
            if sub and sub["device"]:
                # measured through the system audio path: neither its volume nor its latency is known
                mid = (GRID >= 200) & (GRID <= 5000)
                c["verified"] += float(np.mean((c["predicted"] - c["verified"])[mid]))
                c["verified_arrival_ms"] = None
                fc = config.sub(cfg)["crossover"]
                around = (GRID >= fc / 1.4) & (GRID <= fc * 1.4)
                missing = float(np.mean((c["predicted"] - c["verified"])[around]))
                if missing > 2.5 and not any("add up" in n for n in notes):
                    notes.append(f"Subwoofer and speakers do not add up as predicted around {fc:g} Hz "
                                 f"({missing:.0f} dB is missing). The delay between the two output devices "
                                 "was different when the result was measured than when the delay was measured. "
                                 "Press \"Measure result again\"; if it stays, measure again.")
        curves.append(c)
    return {"fs": fs, "speakers": speakers, "sub": sub, "curves": curves, "freq": GRID, "target": shape,
            "headroom": round(headroom, 1), "warnings": meas.get("warnings", []) + notes}


def to_json(obj):
    """Design -> plain JSON-serialisable structure (curves rounded, NaN -> None)."""
    if isinstance(obj, dict):
        return {k: to_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return [None if np.isnan(v) else round(float(v), 2) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    return obj
