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
    return cfg


def save(cfg):
    (data_dir() / "settings.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")


def demo():
    """A stereo two-way setup for the built-in simulator."""
    cfg = copy.deepcopy(DEFAULT)
    cfg.update(
        output_device="Simulated 8-channel DAC", input_device="Simulated microphone",
        speakers=[{"name": "Left", "input": 0, "channels": [0, 1]},
                  {"name": "Right", "input": 1, "channels": [2, 3]}],
        ways=[{"name": "Woofer", "min_freq": 0}, {"name": "Tweeter", "min_freq": 1500}],
        crossover={"freqs": [2200], "slope": "LR4"},
    )
    return cfg


def wiring_key(cfg):
    """Changes whenever the channel assignment changes, forcing a new wiring check."""
    return json.dumps([cfg["output_device"], [s["channels"] for s in cfg["speakers"]]])


def validate(cfg, n_out=None):
    """Return a list of problems; an empty list means the setup is safe to use."""
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
