import copy
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request

import numpy as np
import pytest
import yaml

from roomfix import config, design, dsp, engine, measure, sim

FS = 48000


def run(cfg, **sim_args):
    audio = sim.SimAudio(cfg, **sim_args)
    m = measure.Measurer(cfg, audio)
    meas = m.measure()
    d = design.design(cfg, meas)
    d = design.design(cfg, meas, verify_irs=m.verify(d, meas["levels"]))
    return audio, meas, d


def error(d, curve, key):
    f = d["freq"]
    sel = (f >= curve["range"][0] * 2) & (f <= curve["range"][1] / 1.5)
    return np.sqrt(np.mean((curve[key] - d["target"])[sel] ** 2))


@pytest.fixture(scope="module")
def two_way():
    return run(config.demo())


def test_correction_improves_response_and_matches_prediction(two_way):
    _, _, d = two_way
    for c in d["curves"]:
        assert error(d, c, "verified") < 0.75 * error(d, c, "measured")
        sel = (d["freq"] > 30) & (d["freq"] < 18000)
        diff = np.abs(c["verified"] - c["predicted"])[sel]
        assert np.percentile(diff, 95) < 0.5 and diff.max() < 2.0  # max is at deep, noisy nulls


def test_speakers_time_aligned(two_way):
    _, _, d = two_way
    # simulated right speaker is 0.3 m further away: left must be delayed by ~0.875 ms
    assert abs(d["speakers"][0]["delay"] - 42) <= 1 and d["speakers"][1]["delay"] == 0
    a, b = (c["verified_arrival_ms"] for c in d["curves"])
    assert abs(a - b) < 0.03


def test_tweeter_never_receives_low_frequencies_or_excess_level(two_way):
    audio, _, _ = two_way
    cfg = audio.cfg
    limit = 10 ** (cfg["max_level_db"] / 20)
    for spk in cfg["speakers"]:
        played = np.concatenate([h[:, spk["channels"][1]] for h in audio.history if h.shape[1] > spk["channels"][1]])
        assert np.max(np.abs(played)) <= limit * 1.001
        spec = np.abs(np.fft.rfft(played)) ** 2
        f = np.fft.rfftfreq(len(played), 1 / FS)
        low = spec[f < cfg["ways"][1]["min_freq"] / 2].sum() / spec.sum()
        assert 10 * np.log10(low) < -30


@pytest.mark.parametrize("slope", ["LR2", "LR4", "LR8"])
def test_crossover_sums_flat_and_detects_reversed_drivers(slope):
    cfg = config.demo()
    cfg["crossover"]["slope"] = slope
    polarity = lambda d: [[w["invert"] for w in s["ways"]] for s in d["speakers"]]
    _, _, normal = run(cfg)
    left, right = polarity(normal)
    assert left == right  # identical speakers get identical crossovers
    _, _, tweeter = run(cfg, flipped=[(1, 1)])
    assert polarity(tweeter) == [left, [left[0], not left[1]]]
    _, _, speaker = run(cfg, flipped=[(1, 0), (1, 1)])
    assert polarity(speaker) == [left, [not left[0], not left[1]]]
    assert any("reversed polarity" in w for w in speaker["warnings"])
    f = normal["freq"]
    xo = (f > 1500) & (f < 3200)
    for d in (normal, tweeter, speaker):  # no cancellation dip around the crossover
        for c in d["curves"]:
            assert np.min((c["measured"] - np.median(c["measured"][xo]))[xo]) > -3


def test_three_way_and_multiple_positions():
    cfg = config.demo()
    cfg["speakers"] = [{"name": "Left", "input": 0, "channels": [0, 1, 2]},
                       {"name": "Right", "input": 1, "channels": [3, 4, 5]}]
    cfg["ways"] = [{"name": "Woofer", "min_freq": 0}, {"name": "Midrange", "min_freq": 250},
                   {"name": "Tweeter", "min_freq": 2000}]
    cfg["crossover"] = {"freqs": [400, 3000], "slope": "LR4"}
    cfg["positions"] = 3
    _, meas, d = run(cfg)
    assert len(meas["irs"]["s0w0"]) == 3
    for c in d["curves"]:
        assert error(d, c, "predicted") < error(d, c, "measured")


def test_single_way_equalizer_apo_export():
    cfg = copy.deepcopy(config.DEFAULT)
    cfg["backend"] = "equalizerapo"
    _, _, d = run(cfg)
    text = engine.eqapo_config(d)
    assert "Channel: L" in text and "Channel: R" in text and "Filter: ON PK Fc" in text
    assert text.count("Preamp:") == 2


def test_validation_blocks_unsafe_setups():
    cfg = config.demo()
    assert config.validate(cfg) == []
    bad = copy.deepcopy(cfg)
    bad["crossover"]["freqs"] = [1000]  # below the tweeter's safe limit
    assert any("safe limit" in e for e in config.validate(bad))
    with pytest.raises(RuntimeError):
        measure.Measurer(bad, sim.SimAudio(bad))
    bad = copy.deepcopy(cfg)
    bad["backend"] = "equalizerapo"
    assert config.validate(bad)
    bad = copy.deepcopy(cfg)
    bad["speakers"][1]["channels"] = [0, 3]
    assert any("only be used once" in e for e in config.validate(bad))
    assert any("only has" in e for e in config.validate(cfg, n_out=2))


def test_target_changes_shape_of_correction(two_way):
    _, meas, _ = two_way
    cfg = config.demo()
    cfg["target"].update(tilt=0.0, bass_boost=0.0)
    flat = design.design(cfg, meas)
    assert abs(flat["target"]).max() == 0
    assert error(flat, flat["curves"][0], "predicted") < error(flat, flat["curves"][0], "measured")
    cfg["target"]["points"] = [[1000, 0], [10000, -6]]
    assert design.design(cfg, meas)["target"][-1] < -5


def test_calibration_file_is_applied(two_way):
    _, meas, d = two_way
    cal = dsp.load_calibration('"Sens Factor =-1.2dB, SERNO: 700"\n* comment\n20 0.0\n1000 0.0\n2000 3.0 12\n20000 3.0\n')
    cfg = copy.deepcopy(config.DEFAULT)
    _, meas, d = run(cfg)
    d2 = design.design(cfg, meas, cal)
    i = np.argmin(abs(d["freq"] - 8000))
    j = np.argmin(abs(d["freq"] - 500))
    shift = (d2["curves"][0]["measured"][i] - d2["curves"][0]["measured"][j]) - \
            (d["curves"][0]["measured"][i] - d["curves"][0]["measured"][j])
    assert abs(shift + 3.0) < 0.3


def test_no_signal_is_reported():
    cfg = config.demo()
    audio = sim.SimAudio(cfg)
    audio.cfg = dict(cfg, speakers=cfg["speakers"][:1])  # right speaker unplugged
    with pytest.raises(RuntimeError, match="No sound detected from Right"):
        measure.Measurer(cfg, audio).measure()


def test_camilladsp_output_matches_renderer(two_way, tmp_path):
    exe = os.environ.get("CAMILLADSP") or shutil.which("camilladsp")
    if not exe:
        pytest.skip("camilladsp binary not available")
    _, _, d = two_way
    cfg = config.demo()
    c = engine.camilladsp_config(cfg, d, 8)
    assert c["devices"]["playback"]["channels"] == 8
    x = np.random.default_rng(0).standard_normal((FS, 2)) * 0.1
    x.astype("<f8").tofile(tmp_path / "in.raw")
    c["devices"] = {"samplerate": FS, "chunksize": 1024,
                    "capture": {"type": "RawFile", "channels": 2, "filename": str(tmp_path / "in.raw"), "format": "F64_LE"},
                    "playback": {"type": "File", "channels": 8, "filename": str(tmp_path / "out.raw"), "format": "F64_LE"}}
    (tmp_path / "c.yml").write_text(yaml.safe_dump(c, sort_keys=False))
    subprocess.run([exe, str(tmp_path / "c.yml")], check=True, capture_output=True)
    y = np.fromfile(tmp_path / "out.raw", "<f8").reshape(-1, 8)
    n = len(y) - 2048
    assert np.abs(y[:, 4:]).max() == 0  # unused DAC channels stay silent
    for spk in d["speakers"]:
        ref = design.render(spk, x[:, spk["input"]], FS)
        for wi, way in enumerate(spk["ways"]):
            assert np.abs(y[:n, way["channel"]] - ref[:n, wi]).max() < 1e-9


def test_web_api(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    from roomfix.server import App, serve
    app = App(demo=True)
    server = serve(app, 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Roomfix": "1"} if headers is None else headers)
        return json.loads(urllib.request.urlopen(req).read())

    with pytest.raises(urllib.error.HTTPError):  # other websites cannot trigger actions
        call("/api/run", {"action": "measure"}, headers={})
    app.cfg["wiring_confirmed"] = config.wiring_key(app.cfg)
    call("/api/run", {"action": "measure"})
    for _ in range(600):
        state = call("/api/state")
        if not state["job"]["running"]:
            break
        time.sleep(0.1)
    assert state["job"]["error"] is None and "Measured and corrected" in state["job"]["result"]
    d = call("/api/design")
    assert len(d["curves"]) == 2 and "verified" in d["curves"][0]
    assert (tmp_path / "camilladsp.yml").exists()
    before = d["target"][0]
    cfg = state["cfg"]
    cfg["target"]["bass_boost"] = 8
    call("/api/config", cfg)
    assert call("/api/design")["target"][0] > before + 3
    server.shutdown()
