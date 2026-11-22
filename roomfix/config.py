"""Settings: defaults, storage and validation (the safety gate)."""
import copy
import json
import os
import sys
from pathlib import Path

WAY_NAMES = {1: ["Full range"], 2: ["Woofer", "Tweeter"], 3: ["Woofer", "Midrange", "Tweeter"]}

DEFAULT = {
    "sample_rate": 48000,
    "output_device": "",
    "input_device": "",
    "input_channel": 0,
    "mic_cal_name": "",
    "backend": "camilladsp",        # or "equalizerapo"
    "capture_device": "",           # where CamillaDSP picks up system audio
    "capture_loopback": False,
    "autostart": True,
    "max_level_db": -12.0,          # hard ceiling for all test signals (dBFS)
    "positions": 1,
    "speakers": [
        {"name": "Left", "input": 0, "channels": [0]},
        {"name": "Right", "input": 1, "channels": [1]},
    ],
    # one entry per way, lowest first. min_freq = lowest frequency the driver may be crossed at.
    "ways": [{"name": "Full range", "min_freq": 0}],
    "crossover": {"freqs": [], "slope": "LR4"},
    # one subwoofer for the bass of all speakers. device "" = a channel of the DAC above,
    # otherwise another output device (its different latency is measured and compensated).
    "sub": {"enabled": False, "device": "", "channel": 2, "crossover": 80.0,
            "max_level_db": None},  # None = same test level limit as the speakers
    # "TV App" source: the TV plays its own apps on its own speakers and sends the sound to a
    # digital (S/PDIF) input. roomfix then runs only the subwoofer, from that input. device ""
    # = not used. The TV's speakers play at once, so the subwoofer can only be later: the
    # measured delay is not used, and delay_ms, invert (against the measured polarity) and
    # gain_db are set by ear.
    "tv_app": {"device": "", "delay_ms": 0.0, "invert": False, "gain_db": 0.0},
    "target": {
        "tilt": -0.8,               # dB per octave
        "bass_boost": 3.0,          # dB shelf below bass_freq
        "bass_freq": 100.0,
        "points": [],               # optional custom curve: [[freq, dB], ...] added on top
        "max_boost": 6.0,
        "max_cut": 12.0,
        "f_min": None,              # correction range, None = detect from the speaker
        "f_max": None,
        "n_filters": 12,
    },
    "wiring_confirmed": "",
}


def data_dir():
    base = os.environ.get("ROOMFIX_HOME")
    if not base:
        root = os.environ.get("APPDATA") if sys.platform == "win32" else None
        base = Path(root) / "roomfix" if root else Path.home() / ".roomfix"
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def load():
    cfg = copy.deepcopy(DEFAULT)
    path = data_dir() / "settings.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        cfg.update({k: v for k, v in saved.items() if k in DEFAULT})
        cfg["target"] = {**DEFAULT["target"], **cfg["target"]}
        cfg["sub"] = {**DEFAULT["sub"], **cfg["sub"]}
        cfg["tv_app"] = {**DEFAULT["tv_app"], **cfg["tv_app"]}
    return cfg


def save(cfg):
    (data_dir() / "settings.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")


def default_channels(n_speakers, n_ways):
    """DAC channels per speaker: all woofers first, then the next way, tweeters last.

    Windows plays ordinary stereo to channels 1 and 2 of a device. If that ever happens
    to the DAC, it must reach woofers, not tweeters. (The web UI uses the same layout.)
    """
    return [[wi * n_speakers + si for wi in range(n_ways)] for si in range(n_speakers)]


def guess_capture(inputs):
    """The virtual cable among the recording devices, or "" if there is none."""
    for word in ("cable output", "vb-audio", "virtual"):
        for dev in inputs:
            if word in dev["name"].lower():
                return dev["name"]
    return ""


def demo():
    """A stereo two-way setup for the built-in simulator."""
    cfg = copy.deepcopy(DEFAULT)
    left, right = default_channels(2, 2)
    cfg.update(
        output_device="Simulated 8-channel DAC", input_device="Simulated microphone",
        speakers=[{"name": "Left", "input": 0, "channels": left},
                  {"name": "Right", "input": 1, "channels": right}],
        ways=[{"name": "Woofer", "min_freq": 0}, {"name": "Tweeter", "min_freq": 1500}],
        crossover={"freqs": [2200], "slope": "LR4"},
    )
    return cfg


def sub(cfg):
    """The subwoofer settings, or None without a subwoofer."""
    s = cfg.get("sub") or {}
    return s if s.get("enabled") else None


def sub_device(cfg):
    """Name of the separate output device of the subwoofer, or None if it shares the DAC."""
    s = sub(cfg)
    return s["device"] if s and s["device"] and s["device"] != cfg["output_device"] else None


TV_APP_MAX_DELAY_MS = 50.0


def tv_app(cfg):
    """The "TV App" settings, or None when that source is not set up (it needs a subwoofer)."""
    t = cfg.get("tv_app") or {}
    return t if t.get("device") and sub(cfg) else None


def wiring_key(cfg):
    """Changes whenever the channel assignment changes, forcing a new wiring check."""
    s = sub(cfg)
    return json.dumps([cfg["output_device"], [s["channels"] for s in cfg["speakers"]]]
                      + ([[s["device"], s["channel"]]] if s else []))


def validate(cfg, n_out=None, n_sub=None):
    """Return a list of problems; an empty list means the setup is safe to use.

    n_out / n_sub: channel counts of the DAC and of a separate subwoofer device, if known.
    """
    errors = []
    ways, xo = cfg["ways"], cfg["crossover"]
    n = len(ways)
    if not 1 <= n <= 3:
        errors.append("Speakers must have 1, 2 or 3 ways.")
    if not cfg["speakers"]:
        errors.append("Add at least one speaker.")
    used = []
    for spk in cfg["speakers"]:
        if len(spk["channels"]) != n:
            errors.append(f"{spk['name']}: needs exactly {n} DAC channel(s).")
        used += spk["channels"]
    s = sub(cfg)
    if s:
        if sub_device(cfg) is None:
            used.append(s["channel"])
        elif n_sub is not None and s["channel"] >= n_sub:
            errors.append(f"The subwoofer device only has {n_sub} channels.")
        if not 30 <= s["crossover"] <= 300:
            errors.append("Subwoofer crossover must be between 30 and 300 Hz.")
        if s["max_level_db"] is not None and not -60 <= s["max_level_db"] <= 0:
            errors.append("Maximum subwoofer test level must be between -60 and 0 dBFS.")
        if cfg["backend"] != "camilladsp":
            errors.append("A subwoofer requires the CamillaDSP engine.")
        elif sub_device(cfg) and not cfg["capture_device"]:
            errors.append("Select the system audio source: it is needed to measure the delay "
                          "between the two output devices.")
    t = tv_app(cfg)
    if t:
        if t["device"] == cfg["capture_device"]:
            errors.append("The TV App input must be the S/PDIF input the TV is connected to, "
                          "not the system audio source.")
        if not 0 <= t["delay_ms"] <= TV_APP_MAX_DELAY_MS:
            errors.append(f"TV App subwoofer delay must be between 0 and {TV_APP_MAX_DELAY_MS:g} ms.")
        if not -20 <= t["gain_db"] <= 10:
            errors.append("TV App subwoofer level must be between -20 and +10 dB.")
    if len(set(used)) != len(used):
        errors.append("Each DAC channel can only be used once.")
    if n_out is not None and used and max(used) >= n_out:
        errors.append(f"The DAC only has {n_out} channels.")
    if len(xo["freqs"]) != n - 1:
        errors.append(f"A {n}-way speaker needs {n - 1} crossover frequencies.")
    else:
        if list(xo["freqs"]) != sorted(xo["freqs"]) or any(f < 20 or f > 20000 for f in xo["freqs"]):
            errors.append("Crossover frequencies must be ascending and between 20 and 20000 Hz.")
        for i, f in enumerate(xo["freqs"]):
            way = ways[i + 1]
            if not way.get("min_freq") or way["min_freq"] < 20:
                errors.append(f"{way['name']}: enter the lowest safe crossover frequency of the driver.")
            elif f < way["min_freq"]:
                errors.append(f"Crossover at {f:g} Hz is below the {way['name'].lower()}'s "
                              f"safe limit of {way['min_freq']:g} Hz.")
    if xo["slope"] not in ("LR2", "LR4", "LR8"):
        errors.append("Unknown crossover slope.")
    if n > 1 and cfg["backend"] != "camilladsp":
        errors.append("Active crossovers require the CamillaDSP engine: if Equalizer APO ever "
                      "fails to load, drivers would receive an unfiltered signal.")
    if not -60 <= cfg["max_level_db"] <= 0:
        errors.append("Maximum test level must be between -60 and 0 dBFS.")
    if not 1 <= cfg["positions"] <= 9:
        errors.append("Use 1 to 9 microphone positions.")
    return errors
