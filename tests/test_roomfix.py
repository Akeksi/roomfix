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


# ---- subwoofer, also on a second output device with different latency -----------
SUB_LIMIT_DB = -30.0


def sub_cfg(device=""):
    cfg = copy.deepcopy(config.DEFAULT)
    cfg["capture_device"] = "Simulated cable"
    cfg["sub"] = dict(config.DEFAULT["sub"], enabled=True, device=device, channel=0 if device else 2,
                      crossover=90.0, max_level_db=SUB_LIMIT_DB)
    return cfg


@pytest.fixture(scope="module", params=["", "Second DAC"], ids=["same-dac", "other-dac"])
def with_sub(request):
    return run(sub_cfg(request.param))


def test_subwoofer_extends_bass_and_blends_with_speakers(with_sub):
    _, _, d = with_sub
    f = d["freq"]
    for c in d["curves"]:
        assert c["range"][0] < 36  # simulated speakers reach 48 Hz, the subwoofer 25 Hz
        assert error(d, c, "verified") < 0.75 * error(d, c, "measured")
        diff = np.abs(c["verified"] - c["predicted"])
        assert np.percentile(diff[(f > 30) & (f < 18000)], 95) < 1.0
        # the crossover region only adds up as predicted if delay and polarity are right
        assert diff[(f > 45) & (f < 180)].max() < 2.0
        # no cancellation: the crossover band is as loud as the bands on either side of it
        level = lambda lo, hi: np.mean(c["measured"][(f > lo) & (f < hi)])
        assert level(60, 135) > min(level(35, 55), level(150, 250)) - 3


def test_subwoofer_bass_management_export(with_sub):
    audio, _, d = with_sub
    cfg = audio.cfg
    other = cfg["sub"]["device"]
    configs = engine.camilladsp_configs(cfg, d, {cfg["output_device"]: 4, other: 2})
    assert [dev for dev, _ in configs] == ([cfg["output_device"], other] if other else [cfg["output_device"]])
    x = np.random.default_rng(0).standard_normal((FS // 2, 2)) * 0.1
    out = {dev: engine.simulate(conf, x, FS) for dev, conf in configs}
    main = out[cfg["output_device"]]
    for spk in d["speakers"]:  # each speaker is high-passed and plays what the renderer says
        ref = design.render(spk, x[:, spk["input"]], FS)
        assert np.abs(main[:, spk["ways"][0]["channel"]] - ref[:, 0]).max() < 1e-9
        assert any(flt["type"] == "Highpass" and flt["freq"] == 90.0 for flt in spk["ways"][0]["filters"])
    sub = sum(design.render_sub(d, si, x[:, spk["input"]], FS) for si, spk in enumerate(d["speakers"]))
    played = out[other or cfg["output_device"]][:, d["sub"]["channel"]]
    assert np.abs(played - sub).max() < 1e-9  # the subwoofer gets the bass of all speakers
    spec = np.abs(np.fft.rfft(played)) ** 2
    fr = np.fft.rfftfreq(len(played), 1 / FS)
    assert 10 * np.log10(spec[fr > 400].sum() / spec.sum()) < -30


def test_camilladsp_runs_subwoofer_configs_like_the_simulation(with_sub, tmp_path):
    exe = os.environ.get("CAMILLADSP") or shutil.which("camilladsp")
    if not exe:
        pytest.skip("camilladsp binary not available")
    audio, _, d = with_sub
    x = np.random.default_rng(0).standard_normal((FS, 2)) * 0.1
    x.astype("<f8").tofile(tmp_path / "in.raw")
    for i, (_, c) in enumerate(engine.camilladsp_configs(audio.cfg, d)):
        n_out = c["mixers"]["drivers"]["channels"]["out"]
        expected = engine.simulate(c, x, FS)
        c["devices"] = {"samplerate": FS, "chunksize": 1024,
                        "capture": {"type": "RawFile", "channels": 2, "filename": str(tmp_path / "in.raw"), "format": "F64_LE"},
                        "playback": {"type": "File", "channels": n_out, "filename": str(tmp_path / f"out{i}.raw"), "format": "F64_LE"}}
        (tmp_path / "c.yml").write_text(yaml.safe_dump(c, sort_keys=False))
        subprocess.run([exe, str(tmp_path / "c.yml")], check=True, capture_output=True)
        y = np.fromfile(tmp_path / f"out{i}.raw", "<f8").reshape(-1, n_out)
        n = len(y) - 2048
        assert np.abs(y[:n] - expected[:n]).max() < 1e-9


def test_subwoofer_never_played_above_its_own_limit(with_sub):
    audio, _, _ = with_sub
    cfg = audio.cfg
    history = audio.history_other if cfg["sub"]["device"] else audio.history
    ch = cfg["sub"]["channel"]
    played = np.concatenate([h[:, ch] for h in history if h.shape[1] > ch])
    assert 0 < np.max(np.abs(played)) <= 10 ** (SUB_LIMIT_DB / 20) * 1.001
    mains = np.concatenate([h[:, :2].ravel() for h in audio.history])
    assert np.max(np.abs(mains)) <= 10 ** (cfg["max_level_db"] / 20) * 1.001


def test_latency_between_output_devices_is_compensated():
    audio, meas, d = run(sub_cfg("Second DAC"))
    main_ms, sub_ms = audio.engine_latency_ms  # simulated: the speakers' device is much slower
    assert main_ms - sub_ms > 50
    assert abs(d["sub"]["delay"] / FS * 1000 - (main_ms - sub_ms)) < 6  # acoustic paths differ a little
    assert all(w["delay"] < 0.005 * FS for s in d["speakers"] for w in s["ways"])
    # the other way round: a slow subwoofer device means the speakers must wait
    audio, meas, d = run(sub_cfg("Second DAC"), engine_latency_ms=(40.0, 160.0))
    assert d["sub"]["delay"] < 0.005 * FS
    assert all(abs(w["delay"] / FS * 1000 - 120) < 8 for s in d["speakers"] for w in s["ways"])


def test_subwoofer_validation():
    cfg = sub_cfg()
    assert config.validate(cfg, n_out=4) == []
    assert any("only has" in e for e in config.validate(cfg, n_out=2))
    bad = copy.deepcopy(cfg)
    bad["sub"]["channel"] = 1
    assert any("only be used once" in e for e in config.validate(bad))
    bad = copy.deepcopy(cfg)
    bad["sub"]["crossover"] = 500
    assert any("Subwoofer crossover" in e for e in config.validate(bad))
    bad = copy.deepcopy(cfg)
    bad["backend"] = "equalizerapo"
    assert any("subwoofer" in e.lower() for e in config.validate(bad))
    other = sub_cfg("Second DAC")
    assert config.validate(other, n_out=2) == []  # channel 1 of the second device is free
    other["capture_device"] = ""
    assert any("system audio source" in e for e in config.validate(other))


def test_silent_subwoofer_is_reported():
    cfg = sub_cfg("Second DAC")
    audio = sim.SimAudio(cfg)
    audio.cfg = dict(cfg, sub=dict(cfg["sub"], enabled=False))  # subwoofer unplugged
    with pytest.raises(RuntimeError, match="No sound detected from Subwoofer"):
        measure.Measurer(cfg, audio).measure()


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


def test_second_instance_cannot_take_the_same_port(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    from roomfix.server import App, serve
    app = App(demo=True)
    first = serve(app, 0)
    with pytest.raises(OSError):  # otherwise both would answer, at random
        serve(app, first.server_address[1])
    first.server_close()

def test_demo_and_normal_instance_do_not_share_a_port(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    from types import SimpleNamespace
    from roomfix.server import App, open_server, serve
    first = serve(App(demo=False), 0)  # a normal roomfix is running on the default port
    port = first.server_address[1]
    threading.Thread(target=first.serve_forever, daemon=True).start()
    # the demo must not send the user to the normal instance: it takes the next free port
    demo, url = open_server(App(demo=True), default=port)
    assert demo is not None and demo.server_address[1] != port and str(demo.server_address[1]) in url
    demo.server_close()
    # starting the same kind again just points to the one that is running
    again, url = open_server(SimpleNamespace(demo=False), default=port)
    assert again is None and url == f"http://127.0.0.1:{port}/"
    # a port the user asked for is never silently replaced
    with pytest.raises(OSError):
        open_server(App(demo=True), port=port)
    first.shutdown()
    first.server_close()


def test_system_audio_source_is_preselected():
    inputs = [{"name": "Mikrofoni (Umik-1  Gain: 18dB  )"}, {"name": "CABLE Output (VB-Audio Virtual Cable)"}]
    assert config.guess_capture(inputs) == "CABLE Output (VB-Audio Virtual Cable)"
    assert config.guess_capture(inputs[:1]) == ""


def test_default_channels_keep_tweeters_off_the_first_two_channels():
    # Windows plays stereo to channels 1 and 2 of a device: only woofers may sit there
    assert config.default_channels(2, 3) == [[0, 2, 4], [1, 3, 5]]
    assert config.default_channels(3, 1) == [[0], [1], [2]]
    assert [s["channels"] for s in config.demo()["speakers"]] == [[0, 2], [1, 3]]


def test_test_signals_are_played_in_shared_mode(monkeypatch):
    # Exclusive mode plays with gaps on some USB DACs and on VB-CABLE (heard as a distorted,
    # robotic sweep, several dB too weak). Shared mode also accepts any device format.
    from roomfix import audio
    opened = []

    class Settings:
        def __init__(self, exclusive=False):
            opened.append(exclusive)
            self._streaminfo = type("Info", (), {})()

    class Stream:
        def __init__(self, **kw): pass
        def __enter__(self): raise RuntimeError("opened")
        def __exit__(self, *exc): return False
    fake = type("sd", (), {"WasapiSettings": Settings, "InputStream": Stream, "OutputStream": Stream})
    monkeypatch.setattr(audio, "_sd", lambda: fake)
    monkeypatch.setattr(audio.sys, "platform", "win32")
    a = audio.Audio.__new__(audio.Audio)
    a.fs, a.in_ch, a.inp = FS, 0, {"index": 0}
    with pytest.raises(RuntimeError, match="opened"):
        a._playrec(np.zeros((10, 2)), 0.1, {"name": "DAC", "channels": 2, "index": 1})
    assert opened == [False, False]

def test_windows_volume_of_an_output_device_does_not_upset_the_balance():
    # Test signals are played in shared mode, where the Windows volume of a device applies.
    # The playback engine bypasses it. With the speakers' device at -9 dB in Windows, the
    # speakers look 9 dB quieter than music will be: the balance must come from the engines.
    _, _, base = run(sub_cfg("Second DAC"))
    _, meas, d = run(sub_cfg("Second DAC"), windows_volume_db=(-9.0, 0.0))
    assert abs(meas["gains"]["sub"][0] + 9) < 1
    assert np.abs(np.array(d["sub"]["gains"]) - np.array(base["sub"]["gains"])).max() < 1.0
    f = d["freq"]
    for c in d["curves"]:
        assert np.abs(c["verified"] - c["predicted"])[(f > 45) & (f < 180)].max() < 2.0


def test_room_rumble_is_not_mistaken_for_subwoofer_output():
    # A noisy room rumbles below the subwoofer's range. The measurement must not take that
    # for deep bass and equalize it: the correction starts where the subwoofer rises above the noise.
    _, meas, quiet = run(sub_cfg("Second DAC"))
    assert not any("noise" in n for n in quiet["warnings"])
    _, meas, d = run(sub_cfg("Second DAC"), rumble_db=-30.0)
    assert any("noise" in n for n in d["warnings"])
    for c, q, spk in zip(d["curves"], quiet["curves"], d["speakers"]):
        assert q["range"][0] < 30 < c["range"][0] < 60
        assert all(flt["freq"] > 30 for flt in spk["eq"])
    # the delay is still found: it comes from where the sound arrives, not from the noise
    assert abs(d["sub"]["delay"] - quiet["sub"]["delay"]) < 0.004 * FS


def test_changed_delay_between_devices_is_reported():
    # The latency of an output device can differ from one engine start to the next. If the
    # verification shows that subwoofer and speakers no longer add up, the user must be told.
    cfg = sub_cfg("Second DAC")
    audio = sim.SimAudio(cfg)
    m = measure.Measurer(cfg, audio)
    meas = m.measure()
    d = design.design(cfg, meas)
    good = design.design(cfg, meas, verify_irs=m.verify(d, meas["levels"]))
    assert not any("add up" in n for n in good["warnings"])
    audio.engine_latency_ms = (150.0, 64.5)  # the subwoofer's device restarts 5.5 ms faster
    bad = design.design(cfg, meas, verify_irs=m.verify(d, meas["levels"]))
    assert any("add up" in n for n in bad["warnings"])
