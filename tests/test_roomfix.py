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


# ---- TV App: the TV's own speakers, the subwoofer from an S/PDIF input -------------
SPDIF = "SPDIF In (2–Sound BlasterX G5)"


def tv_cfg(cfg, **tv):
    return dict(cfg, tv_app=dict(config.DEFAULT["tv_app"], device=SPDIF, **tv))


def test_tv_app_config_plays_only_the_corrected_subwoofer(with_sub):
    audio, _, d = with_sub
    cfg = tv_cfg(audio.cfg)
    sub_device = cfg["sub"]["device"] or cfg["output_device"]
    channels = {cfg["output_device"]: 4, **({sub_device: 2} if cfg["sub"]["device"] else {})}
    assert engine.camilladsp_configs(cfg, d, channels) == engine.camilladsp_configs(audio.cfg, d, channels)  # PC unchanged
    device, conf = engine.tv_app_config(cfg, d, channels)
    assert device == sub_device
    dev = conf["devices"]
    assert dev["capture"] == {"type": "Wasapi", "channels": 2, "device": SPDIF}  # shared mode: the TV's format
    assert dev["playback"]["device"] == (sub_device or None)  # (the simulated DAC has no name)
    assert dev["playback"]["channels"] == (2 if cfg["sub"]["device"] else 4) and dev["playback"]["exclusive"]
    assert dev["chunksize"] == 256 and dev["target_level"] <= 512  # little latency: the TV's speakers do not wait
    assert dev["enable_rate_adjust"] and dev["resampler"]["type"].startswith("Async")  # two clocks
    assert "adjust_period" not in dev  # (held at one level only to line several engines up)
    # only the subwoofer is fed; the speakers' drivers are the TV's, roomfix plays none of them
    assert [m["dest"] for m in conf["mixers"]["drivers"]["mapping"]] == [d["sub"]["channel"]]
    # the measured correction of the bass, without the delay that lined it up with the PC's path
    assert d["sub"]["delay"] > 0 or not cfg["sub"]["device"]
    x = np.random.default_rng(1).standard_normal((FS // 2, 2)) * 0.1
    want = sum(design.render_sub(dict(d, sub=dict(d["sub"], delay=0)), si, x[:, spk["input"]], FS)
               for si, spk in enumerate(d["speakers"]))
    played = engine.simulate(conf, x, FS)
    assert np.abs(played[:, d["sub"]["channel"]] - want).max() < 1e-9
    assert np.abs(np.delete(played, d["sub"]["channel"], axis=1)).max() == 0
    # set by ear: later, the other polarity, louder or quieter
    tuned = engine.tv_app_config(tv_cfg(audio.cfg, delay_ms=5.0, invert=True, gain_db=-4.0), d)[1]
    later = sum(design.render_sub(dict(d, sub=dict(d["sub"], delay=240, invert=not d["sub"]["invert"],
                                                   gains=[g - 4.0 for g in d["sub"]["gains"]])),
                                  si, x[:, spk["input"]], FS) for si, spk in enumerate(d["speakers"]))
    assert np.abs(engine.simulate(tuned, x, FS)[:, d["sub"]["channel"]] - later).max() < 1e-9


def test_tv_app_needs_a_subwoofer_and_an_input(with_sub):
    audio, _, d = with_sub
    assert engine.tv_app_config(audio.cfg, d) is None  # no input chosen
    assert config.tv_app(audio.cfg) is None
    no_sub = tv_cfg(dict(audio.cfg, sub=dict(audio.cfg["sub"], enabled=False)))
    assert config.tv_app(no_sub) is None and engine.tv_app_config(no_sub, dict(d, sub=None)) is None


def test_camilladsp_runs_the_tv_app_config_like_the_simulation(with_sub, tmp_path):
    exe = os.environ.get("CAMILLADSP") or shutil.which("camilladsp")
    if not exe:
        pytest.skip("camilladsp binary not available")
    audio, _, d = with_sub
    _, c = engine.tv_app_config(tv_cfg(audio.cfg, delay_ms=1.5, invert=True), d)
    (tmp_path / "check.yml").write_text(yaml.safe_dump(c, sort_keys=False), encoding="utf-8")
    check = subprocess.run([exe, "--check", str(tmp_path / "check.yml")], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr  # as generated, devices and all
    n_out = c["mixers"]["drivers"]["channels"]["out"]
    x = np.random.default_rng(0).standard_normal((FS, 2)) * 0.1
    x.astype("<f8").tofile(tmp_path / "in.raw")
    expected = engine.simulate(c, x, FS)
    c["devices"] = {"samplerate": FS, "chunksize": 256,
                    "capture": {"type": "RawFile", "channels": 2, "filename": str(tmp_path / "in.raw"), "format": "F64_LE"},
                    "playback": {"type": "File", "channels": n_out, "filename": str(tmp_path / "out.raw"), "format": "F64_LE"}}
    (tmp_path / "c.yml").write_text(yaml.safe_dump(c, sort_keys=False))
    subprocess.run([exe, str(tmp_path / "c.yml")], check=True, capture_output=True)
    y = np.fromfile(tmp_path / "out.raw", "<f8").reshape(-1, n_out)
    n = len(y) - 512
    assert np.abs(y[:n] - expected[:n]).max() < 1e-9


def test_apply_writes_the_tv_app_engine_beside_the_pc_engines(with_sub, tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "WINDOWS", False)  # write the files, start nothing
    audio, _, d = with_sub
    engine.apply(tv_cfg(audio.cfg), d)
    pc = [p.name for p in engine._yamls()]
    assert pc == (["camilladsp.yml", "camilladsp_2.yml"] if audio.cfg["sub"]["device"] else ["camilladsp.yml"])
    conf = yaml.safe_load((tmp_path / "tvapp.yml").read_text(encoding="utf-8"))
    assert conf["devices"]["capture"]["device"] == SPDIF and engine._tv_app_files() == [tmp_path / "tvapp.yml"]
    engine.apply(audio.cfg, d)  # TV App no longer used: its engine goes
    assert not (tmp_path / "tvapp.yml").exists() and [p.name for p in engine._yamls()] == pc


def test_tv_app_settings_are_checked_and_kept(tmp_path, monkeypatch):
    cfg = tv_cfg(sub_cfg("Second DAC"))
    assert config.validate(cfg, n_out=2) == []
    for key, value, words in (("delay_ms", 80, "delay"), ("delay_ms", -1, "delay"), ("gain_db", 15, "level"),
                              ("device", cfg["capture_device"], "not the system audio source")):
        bad = copy.deepcopy(cfg)
        bad["tv_app"][key] = value
        assert any(words in e for e in config.validate(bad, n_out=2)), (key, value)
    # settings saved before TV App existed get its defaults; saved ones are kept
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    old = {k: v for k, v in config.DEFAULT.items() if k != "tv_app"}
    (tmp_path / "settings.json").write_text(json.dumps(old))
    assert config.load()["tv_app"] == config.DEFAULT["tv_app"]
    config.save(dict(config.load(), tv_app={"device": SPDIF, "delay_ms": 3.5}))
    assert config.load()["tv_app"] == dict(config.DEFAULT["tv_app"], device=SPDIF, delay_ms=3.5)


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
    app.cfg["adjust"] = {"speakers": [{"gain": -3}], "sub": {}}  # made for an earlier measurement
    version = call("/api/state")["settings_version"]
    call("/api/run", {"action": "measure"})
    for _ in range(600):
        state = call("/api/state")
        if not state["job"]["running"]:
            break
        time.sleep(0.1)
    assert state["job"]["error"] is None and "Measured and corrected" in state["job"]["result"]
    # a new measurement starts without the changes by hand; applied, it is kept
    assert state["cfg"]["adjust"] == config.DEFAULT["adjust"] and state["settings_version"] > version
    assert [(c["active"], c["editable"]) for c in state["corrections"]] == [(True, True)]
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


# ---- changes by hand, and saved corrections ----------------------------------
def flat_ways(d):
    return [w for s in d["speakers"] for w in s["ways"]]


def test_changes_by_hand_come_on_top_of_the_measured_values(two_way):
    _, meas, _ = two_way
    measured = design.design(config.demo(), meas)
    cfg = config.demo()
    cfg["adjust"] = {"speakers": [{"ways": [{}, {"gain": -2, "invert": True}]},  # left tweeter
                                  {"delay_ms": 1.0, "invert": True}], "sub": {}}  # right speaker
    d = design.design(cfg, meas)
    assert d["changes"] == 4
    # the EQ is fitted without them: it does not undo them
    assert [s["eq"] for s in d["speakers"]] == [s["eq"] for s in measured["speakers"]]
    tweeter, before = d["speakers"][0]["ways"][1], measured["speakers"][0]["ways"][1]
    assert tweeter["gain"] == pytest.approx(before["gain"] - 2) and tweeter["invert"] != before["invert"]
    right, right_before = d["speakers"][1]["ways"], measured["speakers"][1]["ways"]
    assert [w["invert"] for w in right] == [not w["invert"] for w in right_before]
    assert [w["delay"] - b["delay"] for w, b in zip(right, right_before)] == [48, 48]  # 1 ms
    assert d["speakers"][0]["gain"] == measured["speakers"][0]["gain"]  # (nothing louder: nothing quieter)
    # the page shows the measured values beside the changes
    assert d["auto"]["speakers"][0]["ways"][1] == before and d["auto"]["speakers"][1]["ways"] == right_before
    # predicted: the tweeter 2 dB down, and reversed it no longer adds up with the woofer
    f, c, c0 = d["freq"], d["curves"][0], measured["curves"][0]
    treble, crossover = (f > 5000) & (f < 10000), (f > 1800) & (f < 2700)
    assert np.mean((c["predicted"] - c0["predicted"])[treble]) == pytest.approx(-2, abs=0.5)
    assert np.min((c["predicted"] - c0["predicted"])[crossover]) < -6
    assert np.allclose(c["measured"], c0["measured"])  # "before" stays as measured
    # a whole speaker reversed or later sounds the same on its own
    assert np.allclose(d["curves"][1]["predicted"], measured["curves"][1]["predicted"], atol=0.01)
    # and what is exported is what the chain renders (CamillaDSP is checked against render)
    conf = engine.camilladsp_config(cfg, d, 8)
    x = np.random.default_rng(2).standard_normal((FS // 2, 2)) * 0.1
    y = engine.simulate(conf, x, FS)
    for spk in d["speakers"]:
        ref = design.render(spk, x[:, spk["input"]], FS)
        for wi, way in enumerate(spk["ways"]):
            assert np.abs(y[:, way["channel"]] - ref[:, wi]).max() < 1e-9


def test_louder_or_earlier_by_hand_never_clips_or_plays_too_soon(two_way):
    _, meas, _ = two_way
    measured = design.design(config.demo(), meas)
    cfg = config.demo()
    # the right woofer, which needs no delay as measured, 3 dB louder and 0.5 ms earlier
    assert flat_ways(measured)[2]["delay"] == 0
    cfg["adjust"] = {"speakers": [{}, {"ways": [{"gain": 3, "delay_ms": -0.5}]}], "sub": {}}
    d = design.design(cfg, meas)
    assert d["shift"] == 3 and any("3.0 dB quieter" in w for w in d["warnings"])
    for s, s0 in zip(d["speakers"], measured["speakers"]):
        assert s["gain"] == pytest.approx(s0["gain"] - 3)  # everything quieter, so nothing clips...
    assert flat_ways(d)[2]["gain"] == pytest.approx(flat_ways(measured)[2]["gain"] + 3)  # ...but this one
    # it cannot play before its sound arrives: all the others wait 0.5 ms longer instead
    delays, before = [w["delay"] for w in flat_ways(d)], [w["delay"] for w in flat_ways(measured)]
    assert delays[2] == 0 and [a - b for a, b in zip(delays, before)] == [24, 24, 0, 24]


def test_subwoofer_changed_by_hand_but_the_tv_app_keeps_its_own(with_sub):
    audio, meas, _ = with_sub
    measured = design.design(audio.cfg, meas)
    cfg = tv_cfg(copy.deepcopy(audio.cfg), delay_ms=2.0)
    cfg["adjust"] = {"speakers": [], "sub": {"gain": -3, "delay_ms": 2.0, "invert": True}}
    d = design.design(cfg, meas)
    sub, before = d["sub"], measured["sub"]
    assert sub["gains"] == pytest.approx([g - 3 for g in before["gains"]]) and sub["invert"] != before["invert"]
    assert sub["delay"] - before["delay"] == 96  # 2 ms
    # predicted: 3 dB less subwoofer, and reversed it no longer adds up with the speakers
    f, c, c0 = d["freq"], d["curves"][0], measured["curves"][0]
    deep = (f > 35) & (f < 50)
    assert np.mean((c["bass"]["sub"] - c0["bass"]["sub"])[deep]) == pytest.approx(-3, abs=0.3)
    fc = cfg["sub"]["crossover"]
    assert np.min((c["predicted"] - c0["predicted"])[(f > fc / 1.3) & (f < fc * 1.3)]) < -3
    # the TV App source is set by ear on its own: as measured, not as changed for the PC
    assert engine.tv_app_config(cfg, d) == engine.tv_app_config(tv_cfg(audio.cfg, delay_ms=2.0), measured)


def test_changes_by_hand_are_checked_and_kept(tmp_path, monkeypatch):
    assert config.adjust_errors({"speakers": [{"gain": 1, "ways": [{"delay_ms": -3}]}], "sub": {"invert": True}}) == []
    for bad, words in (({"sub": {"gain": 12}}, "level"), ({"speakers": [{"delay_ms": 25}]}, "delay"),
                       ({"speakers": [{"ways": [{}, {"gain": "loud"}]}]}, "numbers"), ([], "object")):
        assert any(words in e for e in config.adjust_errors(bad)), bad
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    old = {k: v for k, v in config.DEFAULT.items() if k != "adjust"}  # saved before they existed
    (tmp_path / "settings.json").write_text(json.dumps(old))
    assert config.load()["adjust"] == config.DEFAULT["adjust"]
    adjust = {"speakers": [{"invert": True}], "sub": {"gain": -2.5}}
    config.save(dict(config.load(), adjust=adjust))
    assert config.load()["adjust"] == adjust


def test_saved_corrections(tmp_path, monkeypatch):
    from roomfix import corrections
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    live = lambda: sorted(p.name for p in corrections.live_files())
    write = lambda name, text: (tmp_path / name).write_text(text, encoding="utf-8")
    # what was applied before there was a history is kept as a preset
    write("camilladsp.yml", "old: 1\n")
    write("camilladsp_2.yml", "old: 2\n")
    write("equalizerapo.txt", "left over from trying Equalizer APO\n")
    first = corrections.entries()
    assert [(e["name"], e["active"], e["editable"]) for e in first] == [("Earlier correction", True, False)]
    assert live() == ["camilladsp.yml", "camilladsp_2.yml"]
    # each Apply adds one; the same files again add nothing
    write("measurement.npz", "measured")
    write("camilladsp.yml", "new: 1\n")
    (tmp_path / "camilladsp_2.yml").unlink()
    settings = {"target": {"tilt": -1}, "crossover": None, "adjust": None}
    new = corrections.record(corrections.live_files(), settings=settings, layout="L",
                             measurement=tmp_path / "measurement.npz", summary="Warm")
    assert corrections.record(corrections.live_files()) == new
    assert [(e["id"] == new, e["active"], e["editable"]) for e in corrections.entries()] == [(True, True, True),
                                                                                         (False, False, False)]
    meta = corrections.get(new)
    assert meta["settings"] == settings and meta["summary"] == "Warm" and meta["layout"] == "L"
    assert corrections.measurement_file(meta["measurement"]).read_text() == "measured"
    # back to the earlier one: its files in place, the others gone
    old = first[0]["id"]
    corrections.use(old, backend="camilladsp")
    assert corrections.active() == old and live() == ["camilladsp.yml", "camilladsp_2.yml"]
    assert (tmp_path / "camilladsp_2.yml").read_text() == "old: 2\n"
    assert (tmp_path / "equalizerapo.txt").exists()  # (another engine's file is left alone)
    with pytest.raises(ValueError, match="made for CamillaDSP"):  # with Equalizer APO chosen in Setup
        corrections.use(old, backend="equalizerapo")
    # presets: those with a name, and the one in use
    assert [e["id"] for e in corrections.presets()] == [old]
    corrections.rename(new, "  Movie   night ")
    assert [(e["name"], e["active"]) for e in corrections.presets()] == [("Movie night", False),
                                                                          ("Earlier correction", True)]
    with pytest.raises(ValueError):
        corrections.rename(new, "x" * 61)
    with pytest.raises(ValueError, match="in use"):
        corrections.delete(old)
    for bad in ("../settings", "..\\x", "", None, 5, new + "/../.."):
        with pytest.raises(ValueError):
            corrections.get(bad)
    # history: the newest unnamed ones are kept, presets always
    for i in range(corrections.HISTORY + 3):
        write("camilladsp.yml", f"history: {i}\n")
        last = corrections.record(corrections.live_files(), created=2e9 + i)
    names = [e["name"] for e in corrections.entries()]
    assert len(names) == corrections.HISTORY + 2 and "Movie night" in names and "Earlier correction" in names
    assert corrections.active() == last
    corrections.delete(new)
    assert "Movie night" not in [e["name"] for e in corrections.entries()]
    assert not list((tmp_path / "corrections" / "measurements").glob("*.npz"))  # no longer needed


def test_first_apply_keeps_the_correction_applied_before(two_way, tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    from roomfix import corrections
    from roomfix.server import App
    (tmp_path / "camilladsp.yml").write_text("applied: before the history\n")
    app = App(demo=True)
    app.meas = dict(two_way[1], layout=app.layout())
    app.redesign()
    app.apply()
    entries = corrections.entries()
    assert [(e["name"], e["active"]) for e in entries] == [("", True), ("Earlier correction", False)]
    corrections.use(entries[1]["id"])
    assert (tmp_path / "camilladsp.yml").read_text() == "applied: before the history\n"


def test_page_keeps_applied_corrections_and_follows_a_switch(two_way, tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    from roomfix import corrections
    from roomfix.server import App
    _, meas, _ = two_way
    app = App(demo=True)
    app.meas = dict(meas, layout=app.layout())
    app._save_measurement()
    app.redesign()
    app.apply()
    balanced = corrections.active()
    state = app.state()
    assert [(c["active"], c["editable"]) for c in state["corrections"]] == [(True, True)]
    assert not state["unapplied"] and "Balanced" in state["corrections"][0]["summary"]
    cfg = copy.deepcopy(app.cfg)
    cfg["target"]["bass_boost"] = 6.0
    cfg["adjust"] = {"speakers": [{"ways": [{}, {"invert": True}]}], "sub": {}}
    app.set_config(cfg)
    assert app.state()["unapplied"]
    with pytest.raises(RuntimeError, match="level change"):
        app.set_config(dict(cfg, adjust={"sub": {"gain": 50}}))
    app.apply()
    flipped = corrections.active()
    assert flipped != balanced and "1 change by hand" in app.state()["corrections"][0]["summary"]
    # switched elsewhere (the phone): the page takes up that correction's settings
    version = app.settings_version
    corrections.use(balanced)
    state = app.state()
    assert state["settings_version"] == version + 1 and not state["unapplied"]
    assert app.cfg["target"]["bass_boost"] == 3.0 and app.cfg["adjust"] == config.DEFAULT["adjust"]
    assert app.design["changes"] == 0 and app.cfg["correction"] == balanced
    # from the page, also with another measurement
    other = dict(meas, layout=app.layout(), levels={k: v - 1 for k, v in meas["levels"].items()})
    app.meas = other
    app._save_measurement()
    app.redesign()
    app.apply()
    assert app.meas_id != corrections.get(balanced)["measurement"]
    app.use_correction(flipped)
    assert app.cfg["adjust"]["speakers"][0]["ways"][1]["invert"] is True
    assert app.meas_id == corrections.get(flipped)["measurement"] and app.meas["levels"] == meas["levels"]
    assert app.design["changes"] == 1 and not app.state()["unapplied"]
    # one made with another setup plays, but the page says that it cannot show it
    app.cfg["speakers"][0]["channels"] = [4, 5]
    app.use_correction(balanced)
    assert "another setup" in app.state()["correction_note"]
    assert app.cfg["target"]["bass_boost"] == 6.0  # (the settings were not taken)
