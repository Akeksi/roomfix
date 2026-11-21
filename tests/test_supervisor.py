"""The supervisor keeps the playback engines running and owns the master volume."""
import json
import os
import shutil
import socket
import struct
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
    for size in (192, 512):  # and the phone page has one for the home screen
        png = tray.app_icon(size)
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (size, size)


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
    sup.set_volume(value=-24)
    sup.notify()  # as after a change from the phone: shown like a volume key
    for _ in range(50):
        if icon.osd_text == "-24.0 dB":
            break
        time.sleep(0.05)
    assert icon.osd_text == "-24.0 dB" and icon.osd_bar == pytest.approx(0.6)
    world.run(sup, 2)
    shown = {}
    icon.u.AppendMenuW = lambda menu, flags, ident, text: shown.update({text: ident}) or 1
    icon.u.TrackPopupMenu = lambda *args: shown["Mute"]  # the user picks "Mute"
    icon._menu()
    assert sup.status()["mute"] is True
    assert "Correction on" in shown and "Stop correction and close" in shown and "Volume" in shown
    assert any("LG TV (HDMI): running" in text for text in shown if text)
    icon.close()
    assert not icon.thread.is_alive() and not sup.listeners


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


PHONE_HOST = "living-room-pc.tail1234.ts.net"  # what `tailscale serve` passes on from the phone


def test_only_this_computer_and_its_tailscale_name_are_served():
    for host in ("127.0.0.1", "127.0.0.1:8136", "localhost", "LOCALHOST:8136", PHONE_HOST,
                 PHONE_HOST + ":443", PHONE_HOST.upper(), PHONE_HOST + "."):
        assert supervisor.allowed_host(host), host
    # a website that points its own name at 127.0.0.1 (DNS rebinding) sends its own name
    for host in (None, "", "evil.example", "127.0.0.1.evil.example", "localhost.evil.example",
                 PHONE_HOST + ".evil.example", "evil.example/.ts.net", "ts.net", ".ts.net", "a_b.ts.net",
                 "192.168.1.20:8136", "[::1]:8136", "127.0.0.1:8136:1", PHONE_HOST + ":http", " 127.0.0.1"):
        assert not supervisor.allowed_host(host), host
    assert supervisor.local_host("127.0.0.1:8136") and supervisor.local_host("localhost")
    assert not supervisor.local_host(PHONE_HOST)


def _serve(tmp_path, monkeypatch):
    """A supervisor with its API, and a way to call it under any Host name."""
    world, sup = setup(tmp_path, monkeypatch)
    thread = threading.Thread(target=supervisor.run, args=(sup, 0), daemon=True)
    thread.start()
    for _ in range(50):
        if supervisor.request("/api/status"):
            break
        time.sleep(0.1)
    port = json.loads((tmp_path / "engine.json").read_text())["port"]

    def call(path, body=None, host=PHONE_HOST, kind="application/json"):
        """(HTTP status, content type, reply)"""
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     headers={"Host": host, "Content-Type": kind})
        try:
            with urllib.request.urlopen(req, timeout=5) as reply:
                code, kind, raw = reply.status, reply.headers["Content-Type"], reply.read()
        except urllib.error.HTTPError as exc:
            code, kind, raw = exc.code, exc.headers["Content-Type"], exc.read()
        return code, kind, json.loads(raw) if kind == "application/json" else raw

    return world, sup, thread, call


def test_phone_page_works_through_tailscale_serve(tmp_path, monkeypatch):
    world, sup, thread, call = _serve(tmp_path, monkeypatch)
    try:
        for host in ("127.0.0.1", PHONE_HOST):
            code, kind, page = call("/", host=host)
            assert code == 200 and kind.startswith("text/html") and b"/api/volume" in page
        code, _, manifest = call("/manifest.webmanifest")
        assert code == 200 and json.loads(manifest)["start_url"] == "/"
        for icon in json.loads(manifest)["icons"]:  # installable on the home screen
            code, kind, png = call(icon["src"])
            assert code == 200 and kind == "image/png" and png[:4] == b"\x89PNG"
        code, _, status = call("/api/status")
        assert code == 200 and status["enabled"] is True and len(status["engines"]) == 2
        assert call("/nothing")[0] == 404 and call("/../supervisor.py")[0] == 404
        # nothing at all for a website that points its own name at this computer
        assert call("/", host="evil.example")[0] == 403
        assert call("/api/status", host="evil.example")[0] == 403
    finally:
        supervisor.stop()
        thread.join(10)


def test_phone_api_is_idempotent_and_tells_the_tray(tmp_path, monkeypatch):
    world, sup, thread, call = _serve(tmp_path, monkeypatch)
    told = []
    sup.listeners.append(lambda: told.append(sup.status()["volume"]))
    try:
        # the same request twice (the phone sends it again when the network dropped the
        # reply) gives the same result: absolute volume, mute on or off
        for _ in range(2):
            assert call("/api/volume", {"value": -25})[2]["volume"] == -25
            assert call("/api/mute", {"mute": True})[2]["mute"] is True
        code, _, status = call("/api/volume", {"value": -23, "unmute": True})  # "+" on the phone
        assert code == 200 and status["volume"] == -23 and status["mute"] is False
        assert call("/api/volume", {"value": -25})[2]["mute"] is False  # "−" leaves mute alone
        assert call("/api/power", {"on": False})[2]["enabled"] is False
        assert call("/api/power", {"on": True})[2]["enabled"] is True
        assert told == [-25, -25, -25, -25, -23, -25, -25, -25]  # the tray shows every change
        assert call("/api/restart", {})[0] == 200
        assert sup._restart is True
        del told[:]
        # refused, and nothing changes:
        assert call("/api/volume", {"value": 0}, host="evil.example")[0] == 403  # another website
        assert call("/api/volume", b'{"value": 0}', kind="text/plain")[0] == 403  # a form post
        assert call("/api/volume", b'{"value": NaN}')[0] == 400
        assert call("/api/volume", {"value": True})[0] == 400
        assert call("/api/volume", {"value": "loud"})[0] == 400
        assert call("/api/volume", {})[0] == 400
        assert call("/api/volume", [0])[0] == 400
        assert call("/api/mute", {"mute": "yes"})[0] == 400
        assert call("/api/power", {"on": "off"})[0] == 400
        assert call("/api/quit", {})[0] == 403  # the phone could not start it again
        status = call("/api/status")[2]
        assert status["volume"] == -25 and status["mute"] is False and status["enabled"] is True
        assert told == [] and thread.is_alive()
    finally:
        supervisor.stop()  # from this computer it may quit
        thread.join(10)
    assert not thread.is_alive()


def test_api_keeps_its_port_for_tailscale_serve(tmp_path, monkeypatch):
    handler = supervisor._handler(setup(tmp_path, monkeypatch)[1])
    port = supervisor._free_ports(1)[0]
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))  # the supervisor before this one, still closing
    threading.Timer(0.4, holder.close).start()
    server = supervisor._bind(handler, port, tries=40, pause=0.05)
    assert server.server_address[1] == port  # waited for it
    other = supervisor._bind(handler, port, tries=2, pause=0)  # taken for good: any free port
    assert other.server_address[1] not in (0, port)
    server.server_close()
    other.server_close()


def test_status_is_asked_again_when_the_connection_is_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    (tmp_path / "engine.json").write_text(json.dumps({"port": 8136, "pid": 1, "started": time.time()}))
    calls = []

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"volume": -3}'

    def urlopen(req, timeout=None):
        calls.append(req.data)
        if len(calls) < 3:
            raise urllib.error.URLError(ConnectionResetError(10054, "reset by a network filter"))
        return Reply()

    monkeypatch.setattr(supervisor.urllib.request, "urlopen", urlopen)
    assert supervisor.request("/api/status") == {"volume": -3}
    assert len(calls) == 3
    del calls[:]
    assert supervisor.request("/api/mute", {}) is None  # a toggle is not sent twice
    assert len(calls) == 1 and (tmp_path / "engine.json").exists()


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
