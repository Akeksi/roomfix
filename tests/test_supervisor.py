"""The supervisor keeps the playback engines running and owns the master volume."""
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request

import pytest
import yaml

from roomfix import engine, supervisor


class Proc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = 1


class World:
    """A fake computer: output devices, CamillaDSP processes and their websocket API."""

    def __init__(self, outputs):
        self.outputs, self.now = list(outputs), 0.0
        self.spawned, self.by_port, self.sent = [], {}, []
        self.state = "Running"

    def spawn(self, path, port, volume, mute):
        proc = Proc()
        self.spawned.append((path.name, volume, mute))
        self.by_port[port] = proc
        return proc

    def ask(self, port, command):
        proc = self.by_port.get(port)
        if proc is None or proc.code is not None:
            return None
        if command == "Exit":
            proc.code = 0
        if isinstance(command, dict):
            self.sent.append((port, command))
            return {"result": "Ok"}
        return {"result": "Ok", "value": {"GetState": self.state, "GetStopReason": "PlaybackError"}.get(command)}

    def run(self, sup, seconds):
        for _ in range(int(seconds)):
            self.now += 1.0
            sup.check()


def setup(tmp_path, monkeypatch, devices=("LG TV (HDMI)", "USB DAC")):
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    files = []
    for i, device in enumerate(devices):
        files.append(tmp_path / ("camilladsp.yml" if i == 0 else f"camilladsp_{i + 1}.yml"))
        files[-1].write_text(yaml.safe_dump({"devices": {"playback": {"type": "Wasapi", "device": device}}}))
    world = World(devices)
    sup = supervisor.Supervisor(files, spawn=world.spawn, ask=world.ask, outputs=lambda: world.outputs,
                                clock=lambda: world.now)
    return world, sup


def statuses(sup):
    return [e["status"] for e in sup.status()["engines"]]


def test_engine_restarts_when_its_device_comes_back(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch)
    world.run(sup, 3)
    assert statuses(sup) == ["running", "running"] and len(world.spawned) == 2
    # the TV is switched off: its device disappears and the engine exits
    world.outputs.remove("LG TV (HDMI)")
    world.by_port[sup.engines[0].port].code = 0
    world.run(sup, 600)
    tv = sup.status()["engines"][0]
    assert tv["status"] == "waiting" and "LG TV (HDMI)" in tv["detail"]
    assert statuses(sup)[1] == "running"
    assert len(world.spawned) == 2  # nothing is launched while the device is away
    # switched on again, hours later: sound is back within a few seconds
    world.outputs.append("LG TV (HDMI)")
    world.run(sup, 4)
    assert statuses(sup) == ["running", "running"] and len(world.spawned) == 3


def test_device_renamed_by_windows_is_followed(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch, devices=("1 - LG TV (HDMI)",))
    world.outputs[:] = ["Speakers (Other)", "2 - LG TV (HDMI)"]  # replugged: Windows renumbers it
    world.run(sup, 3)
    assert statuses(sup) == ["running"]
    conf = yaml.safe_load((tmp_path / "camilladsp.yml").read_text())
    assert conf["devices"]["playback"]["device"] == "2 - LG TV (HDMI)"
    assert sup.status()["engines"][0]["device"] == "2 - LG TV (HDMI)"


def test_stopped_or_hung_engine_is_restarted_with_growing_pauses(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch, devices=("USB DAC",))
    world.state = "Inactive"  # the process lives, but CamillaDSP gave up on the device
    world.run(sup, 120)
    assert "PlaybackError" in sup.status()["engines"][0]["detail"]
    assert 4 <= len(world.spawned) <= 9  # retried, but not once a second for ever
    world.state = "Running"
    world.run(sup, 40)
    assert statuses(sup) == ["running"]


def test_volume_goes_to_all_engines_and_survives_restarts(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch)
    world.run(sup, 2)
    assert sup.set_volume(value=-20)["volume"] == -20
    ports = [e.port for e in sup.engines]
    assert [(p, c) for p, c in world.sent if "SetVolume" in c] == [(p, {"SetVolume": -20.0}) for p in ports]
    assert sup.set_volume(delta=-2.5)["volume"] == -22.5
    assert sup.set_volume(value=12)["volume"] == 0  # never amplifies: the measured levels are the limit
    assert sup.set_volume(value=-500)["volume"] == supervisor.MIN_DB
    sup.set_volume(value=-30)
    assert sup.set_mute(True)["mute"] is True and (ports[1], {"SetMute": True}) in world.sent
    assert sup.set_mute()["mute"] is False  # toggles
    sup.set_mute(True)
    assert sup.set_volume(delta=2)["mute"] is False  # turning it up unmutes, like a volume key
    # an engine that restarts starts at the volume in force, not at full level
    world.by_port[ports[0]].code = 0
    world.run(sup, 5)
    assert world.spawned[-1] == ("camilladsp.yml", -28.0, False)
    # and so does a new supervisor, e.g. after the next login
    again = supervisor.Supervisor(sup.files, spawn=world.spawn, ask=world.ask, outputs=lambda: world.outputs)
    assert again.status()["volume"] == -28 and again.status()["mute"] is False


def test_volume_set_while_an_engine_was_down_reaches_it_later(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch, devices=("USB DAC",))
    world.run(sup, 2)
    real, world.ask = world.ask, lambda port, command: None  # the engine does not answer for a moment
    sup.ask = world.ask
    sup.set_volume(value=-12)
    sup.ask = real
    world.run(sup, 2)
    assert (sup.engines[0].port, {"SetVolume": -12.0}) in world.sent


def test_correction_can_be_switched_off_and_on(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch)
    world.run(sup, 3)
    assert sup.set_enabled(False)["enabled"] is False
    world.run(sup, 30)
    assert statuses(sup) == ["stopped", "stopped"] and len(world.spawned) == 2
    assert all(p.code is not None for p in world.by_port.values())  # no sound passes: engines are down
    # it stays off over a restart of the supervisor (next login)...
    again = supervisor.Supervisor(sup.files, spawn=world.spawn, ask=world.ask, outputs=lambda: world.outputs)
    assert again.status()["enabled"] is False
    # ...until switched on, from the tray icon or by applying a correction
    supervisor.switch_on()
    again = supervisor.Supervisor(sup.files, spawn=world.spawn, ask=world.ask, outputs=lambda: world.outputs)
    assert again.status()["enabled"] is True
    sup.set_enabled(True)
    world.run(sup, 3)
    assert statuses(sup) == ["running", "running"] and len(world.spawned) == 4


def test_tray_icon_tells_the_state(tmp_path, monkeypatch):
    from roomfix import tray
    world, sup = setup(tmp_path, monkeypatch)
    world.run(sup, 3)
    sup.set_volume(value=-20)
    assert tray.summary(sup.status()) == ("on", "roomfix: -20.0 dB")
    sup.set_mute(True)
    assert tray.summary(sup.status())[0] == "muted"
    world.outputs.remove("LG TV (HDMI)")
    world.by_port[sup.engines[0].port].code = 0
    world.run(sup, 5)
    state, text = tray.summary(sup.status())
    assert state == "waiting" and "LG TV (HDMI) is not connected" in text
    sup.set_enabled(False)
    assert tray.summary(sup.status())[0] == "off"
    for state in tray.COLORS:  # every state has a valid Windows icon with several sizes
        ico = tray.icon_file(state)
        assert ico[:4] == bytes([0, 0, 1, 0]) and ico[4] >= 3 and b"PNG" in ico
    assert tray.icon_file("on") != tray.icon_file("muted")


@pytest.mark.skipif(os.name != "nt", reason="the notification area is a Windows thing")
def test_tray_icon_appears_and_goes_away(tmp_path, monkeypatch):
    from roomfix import tray
    world, sup = setup(tmp_path, monkeypatch)
    sup.set_keys(False)  # leave the volume keys of this computer alone
    icon = tray.Tray(sup)
    icon.start()
    assert icon.ready.wait(10) and icon.hwnd and icon.shown == tray.summary(sup.status())
    assert all(icon.icons.values())  # Windows could load the generated icon
    icon._show("-20.0 dB", 0.66)  # the volume display can be drawn
    time.sleep(0.3)
    world.run(sup, 2)
    shown = {}
    icon.u.AppendMenuW = lambda menu, flags, ident, text: shown.update({text: ident}) or 1
    icon.u.TrackPopupMenu = lambda *args: shown["Mute"]  # the user picks "Mute"
    icon._menu()
    assert sup.status()["mute"] is True
    assert "Correction on" in shown and "Stop correction and close" in shown and "Volume" in shown
    assert any("LG TV (HDMI): running" in text for text in shown if text)
    icon.close()
    assert not icon.thread.is_alive()


def test_http_api_and_shutdown(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch)
    thread = threading.Thread(target=supervisor.run, args=(sup, 0), daemon=True)
    thread.start()
    for _ in range(50):
        if supervisor.request("/api/status"):
            break
        time.sleep(0.1)
    port = json.loads((tmp_path / "engine.json").read_text())["port"]
    assert supervisor.request("/api/volume", {"value": -15})["volume"] == -15
    assert supervisor.request("/api/mute", {"mute": True})["mute"] is True
    assert supervisor.request("/api/mute", {"mute": "toggle"})["mute"] is False
    assert supervisor.request("/api/mute", {})["mute"] is True
    assert supervisor.request("/api/power", {"on": False})["enabled"] is False
    assert supervisor.request("/api/power", {"on": "toggle"})["enabled"] is True
    status = supervisor.request("/api/status")
    assert status["volume"] == -15 and [e["device"] for e in status["engines"]] == ["LG TV (HDMI)", "USB DAC"]
    # another website cannot turn the volume up: a form post is refused
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/volume", data=b'{"value": 0}',
                                 headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(req)
    assert supervisor.request("/api/status")["volume"] == -15
    supervisor.stop()
    thread.join(10)
    assert not thread.is_alive() and not (tmp_path / "engine.json").exists()
    assert all(p.code is not None for p in world.by_port.values())  # the engines were stopped too
    assert supervisor.request("/api/status") is None


def test_roomfix_page_shows_and_sets_the_volume(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch)
    threading.Thread(target=supervisor.run, args=(sup, 0), daemon=True).start()
    from roomfix.server import App, serve
    app = App(demo=True)
    monkeypatch.setattr(engine, "WINDOWS", True)  # as on a real computer with engines running
    monkeypatch.setattr(engine, "camilla_running", lambda: True)
    server = serve(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def call(path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Roomfix": "1"})
        return json.loads(urllib.request.urlopen(req).read())

    for _ in range(50):
        if call("/api/state")["playback"]:
            break
        time.sleep(0.1)
    state = call("/api/volume", {"value": -18})["state"]
    assert state["playback"]["volume"] == -18 and len(state["playback"]["engines"]) == 2
    assert call("/api/mute", {"mute": True})["state"]["playback"]["mute"] is True
    supervisor.stop()
    assert call("/api/state")["playback"] is None
    with pytest.raises(urllib.error.HTTPError):
        call("/api/volume", {"value": -18})
    server.shutdown()


def test_real_engine_is_restarted_and_obeys_the_volume(tmp_path, monkeypatch):
    exe = os.environ.get("CAMILLADSP") or shutil.which("camilladsp")
    if not exe:
        pytest.skip("camilladsp binary not available")
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "_exe", lambda: exe)
    path = tmp_path / "camilladsp.yml"
    path.write_text(yaml.safe_dump({"devices": {
        "samplerate": 48000, "chunksize": 1024,
        "capture": {"type": "SignalGenerator", "channels": 2, "signal": {"type": "Sine", "freq": 1000, "level": -40}},
        "playback": {"type": "File", "channels": 2, "format": "S16_LE", "filename": os.devnull}}}))
    sup = supervisor.Supervisor([path])
    sup.set_volume(value=-33)
    try:
        def wait(done):
            for _ in range(100):
                sup.check()
                if done():
                    return True
                time.sleep(0.2)
        port = sup.engines[0].port
        volume = lambda: (engine._request(port, "GetVolume") or {}).get("value")
        # (without a sound device CamillaDSP stays "starting", but it lives and answers)
        alive = lambda: statuses(sup)[0] in ("starting", "running") and volume() is not None
        assert wait(alive)
        assert volume() == -33  # started at the stored volume
        sup.set_volume(value=-21)
        assert volume() == -21
        first = sup.engines[0].proc
        first.kill()
        first.wait(5)
        assert wait(lambda: alive() and sup.engines[0].proc is not first)
        assert volume() == -21
        log = path.with_suffix(".log").read_text(errors="replace")
        assert "Lost connection" not in log  # asking the engine does not fill its log
    finally:
        sup.stop()
    assert sup.engines[0].proc is None
