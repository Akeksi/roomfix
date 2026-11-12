"""Signal-processing building blocks: biquads, crossovers, sweeps, smoothing.

Filters are plain dicts using CamillaDSP's parameter names, e.g.
{"type": "Peaking", "freq": 100, "q": 2, "gain": -3}. The same dicts are used
for simulation, for rendering test signals and for export, so what is measured
is what gets applied.
"""
import numpy as np
from scipy import signal

BW4_Q = (0.5411961, 1.3065630)  # Q of the two sections of a 4th-order Butterworth


def coeffs(flt, fs):
    """(b0, b1, b2, a1, a2) for one filter dict (RBJ cookbook / bilinear)."""
    t, f = flt["type"], flt["freq"]
    if t in ("LowpassFO", "HighpassFO"):
        k = np.tan(np.pi * f / fs)
        a1 = (k - 1) / (k + 1)
        if t == "LowpassFO":
            return k / (k + 1), k / (k + 1), 0.0, a1, 0.0
        return 1 / (k + 1), -1 / (k + 1), 0.0, a1, 0.0
    w = 2 * np.pi * f / fs
    cs, alpha = np.cos(w), np.sin(w) / (2 * flt["q"])
    if t == "Peaking":
        a = 10 ** (flt["gain"] / 40)
        b = (1 + alpha * a, -2 * cs, 1 - alpha * a)
        den = (1 + alpha / a, -2 * cs, 1 - alpha / a)
    elif t == "Lowpass":
        b = ((1 - cs) / 2, 1 - cs, (1 - cs) / 2)
        den = (1 + alpha, -2 * cs, 1 - alpha)
    elif t == "Highpass":
        b = ((1 + cs) / 2, -(1 + cs), (1 + cs) / 2)
        den = (1 + alpha, -2 * cs, 1 - alpha)
    else:
        raise ValueError(f"unknown filter type {t}")
    return b[0] / den[0], b[1] / den[0], b[2] / den[0], den[1] / den[0], den[2] / den[0]


def response(filters, fs, f):
    """Complex frequency response of a filter chain at frequencies f."""
    z = np.exp(-2j * np.pi * np.asarray(f) / fs)
    h = np.ones(len(z), complex)
    for flt in filters:
        b0, b1, b2, a1, a2 = coeffs(flt, fs)
        h *= (b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z)
    return h


def apply(filters, fs, x):
    """Run signal x through a filter chain."""
    if not filters:
        return x
    sos = np.array([[b0, b1, b2, 1.0, a1, a2] for b0, b1, b2, a1, a2 in (coeffs(f, fs) for f in filters)])
    return signal.sosfilt(sos, x, axis=0)


def butterworth4(kind, freq):
    """4th-order Butterworth high/lowpass as two biquads."""
    return [{"type": kind, "freq": freq, "q": q} for q in BW4_Q]


def linkwitz_riley(kind, freq, slope):
    """Linkwitz-Riley filter. kind: Highpass/Lowpass, slope: LR2, LR4 or LR8."""
    if slope == "LR2":
        return [{"type": kind + "FO", "freq": freq}] * 2
    if slope == "LR4":
        return [{"type": kind, "freq": freq, "q": 0.7071068}] * 2
    if slope == "LR8":
        return butterworth4(kind, freq) * 2
    raise ValueError(f"unknown crossover slope {slope}")


def db(x):
    return 20 * np.log10(np.maximum(np.abs(x), 1e-12))


def fade(x, fs, t_in, t_out):
    n_in, n_out = int(t_in * fs), int(t_out * fs)
    x = x.copy()
    x[:n_in] *= 0.5 - 0.5 * np.cos(np.pi * np.arange(n_in) / n_in)
    x[len(x) - n_out:] *= 0.5 + 0.5 * np.cos(np.pi * np.arange(n_out) / n_out)
    return x


def log_sweep(fs, f1, f2, dur):
    """Exponential sine sweep with unit amplitude."""
    t = np.arange(int(dur * fs)) / fs
    rate = dur / np.log(f2 / f1)
    x = np.sin(2 * np.pi * f1 * rate * (np.exp(t / rate) - 1))
    return fade(x, fs, min(0.05, dur / 10), min(0.01, dur / 20))


def deconvolve(rec, sweep, n):
    """Transfer function (rfft bins, length n//2+1) of rec relative to sweep."""
    x, y = np.fft.rfft(sweep, n), np.fft.rfft(rec, n)
    p = np.abs(x) ** 2
    return y * np.conj(x) / (p + 1e-6 * p.max())


def find_arrival(rec, probe):
    """Sample position (fractional) where `probe` starts in `rec`, and its SNR in dB."""
    env = np.abs(signal.hilbert(signal.fftconvolve(rec, probe[::-1], mode="valid")))
    k = int(np.argmax(env))
    snr = db(env[k] / (np.median(env) + 1e-12))
    return k + _parabola(env, k), snr


def _parabola(y, k):
    if k <= 0 or k >= len(y) - 1:
        return 0.0
    d = y[k - 1] - 2 * y[k] + y[k + 1]
    return 0.0 if d == 0 else 0.5 * (y[k - 1] - y[k + 1]) / d


def envelope_peak(ir):
    """Fractional sample index of the peak of the IR envelope."""
    env = np.abs(signal.hilbert(ir))
    k = int(np.argmax(env))
    return k + _parabola(env, k)


def smooth(f_lin, power, f_out, octaves):
    """Fractional-octave smoothing of a power spectrum on a linear grid.

    Returns mean power around each f_out within +-octaves/2 (octaves may be an
    array for frequency-dependent smoothing).
    """
    df = f_lin[1] - f_lin[0]
    cs = np.concatenate([[0.0], np.cumsum(power)])
    half = 2 ** (np.asarray(octaves) / 2)
    lo = np.clip(np.floor(f_out / half / df).astype(int), 0, len(power) - 1)
    hi = np.clip(np.ceil(f_out * half / df).astype(int) + 1, lo + 1, len(power))
    return (cs[hi] - cs[lo]) / (hi - lo)


def log_grid(f1=20.0, f2=20000.0, per_octave=24):
    n = int(round(np.log2(f2 / f1) * per_octave))
    return f1 * 2 ** (np.arange(n + 1) / per_octave)


def load_calibration(text):
    """Parse a microphone calibration file -> (freq, dB) arrays.

    Accepts the common 'freq dB [phase]' text formats (miniDSP UMIK, Dayton,
    REW, ...); header and comment lines are ignored.
    """
    rows = []
    for line in text.splitlines():
        parts = line.replace(",", " ").replace(";", " ").split()
        try:
            f, g = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            continue
        if f > 0:
            rows.append((f, g))
    if len(rows) < 2:
        raise ValueError("no calibration data found in file")
    rows.sort()
    f, g = np.array(rows).T
    return f, g


def calibration_gain(cal, f):
    """Linear mic gain at frequencies f (1.0 everywhere without calibration)."""
    if cal is None:
        return np.ones(len(f))
    cf, cg = cal
    return 10 ** (np.interp(np.log(np.maximum(f, 1e-3)), np.log(cf), cg) / 20)
