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
        self.hung = False  # the process lives, but answers nothing

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = 1


class World:
    """A fake computer: output devices, CamillaDSP processes and their websocket API."""

    def __init__(self, outputs, inputs=()):
        self.outputs, self.inputs, self.now = list(outputs), list(inputs), 0.0
        self.spawned, self.by_port, self.sent = [], {}, []
        self.state = "Running"
        self.peak = -20.0  # dBFS of what arrives at the engines' inputs
        self.clashes = []  # an engine started on a device that another running engine holds
        self.reloaded, self.reload_ok = [], True  # engines told to read their configuration again
        # until then (and at that time) no question reaches the engines, which play on: a
        # network filter resets the connections, or the computer is too busy to answer in time
        self.deaf = -1.0
        self.taken = set()  # ports another program holds: an engine there has no API

    def spawn(self, path, port, volume, mute):
        proc = Proc()
        proc.device = yaml.safe_load(path.read_text())["devices"]["playback"]["device"]
        proc.hung = port in self.taken
        path.with_suffix(".log").write_text(  # (CamillaDSP starts its log anew)
            "ERROR [camillalib::socketserver] <src\\socketserver.rs:568> Failed to start websocket server: "
            "address in use (os error 10048)\n" if proc.hung else "")
        self.clashes += [proc.device for other in self.by_port.values()
                         if other.code is None and other.device == proc.device]
        self.spawned.append((path.name, volume, mute))
        self.by_port[port] = proc
        return proc

    def ask(self, port, command):
        proc = self.by_port.get(port)
        if proc is None or proc.code is not None or proc.hung or self.now <= self.deaf:
            return None
        if command == "Exit":
            proc.code = 0
        if command == "Reload":
            self.reloaded.append(port)
            return {"result": "Ok"} if self.reload_ok else {"result": {"ConfigValidationError": "no"}}
        if isinstance(command, dict):
            if "GetCaptureSignalPeakSince" in command:
                return {"result": "Ok", "value": [self.peak, self.peak]}
            self.sent.append((port, command))
            return {"result": "Ok"}
        return {"result": "Ok", "value": {"GetState": self.state, "GetStopReason": "PlaybackError"}.get(command)}

    def running(self):
        """Devices that running engines play to."""
        return sorted(p.device for p in self.by_port.values() if p.code is None)

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


SPDIF = "SPDIF In (2–Sound BlasterX G5)"  # as Windows names it: with an en dash


def setup_tv(tmp_path, monkeypatch, spdif=SPDIF):
    """The PC source on a TV and a USB DAC (the subwoofer), and the TV App source: the TV's
    sound arrives at an S/PDIF input, and only the subwoofer's engine runs."""
    world, sup = setup(tmp_path, monkeypatch)
    path = tmp_path / "tvapp.yml"
    path.write_text(yaml.safe_dump({"devices": {"capture": {"type": "Wasapi", "device": spdif},
                                                "playback": {"type": "Wasapi", "device": "USB DAC"}}}))
    world.inputs = ["CABLE Output (VB-Audio Virtual Cable)", spdif]
    sup = supervisor.Supervisor(sup.files, spawn=world.spawn, ask=world.ask, outputs=lambda: world.outputs,
                                inputs=lambda: world.inputs, clock=lambda: world.now, tv_app=path)
    return world, sup


def test_tv_app_source_runs_only_the_subwoofer(tmp_path, monkeypatch):
    world, sup = setup_tv(tmp_path, monkeypatch)
    status = sup.status()
    assert status["source"] == "pc" and status["sources"] == {"pc": "PC", "tv_app": "TV App"}
    world.run(sup, 3)
    assert world.running() == ["LG TV (HDMI)", "USB DAC"]
    assert sup.set_source("tv_app")["source"] == "tv_app"
    world.run(sup, 3)
    assert world.running() == ["USB DAC"] and world.spawned[-1][0] == "tvapp.yml"
    assert statuses(sup) == ["running"] and [e["device"] for e in sup.status()["engines"]] == ["USB DAC"]
    # the volume, mute and on/off are the same for both sources
    sup.set_volume(value=-24)
    assert (sup.engines[0].port, {"SetVolume": -24.0}) in world.sent
    sup.set_source("pc")
    world.run(sup, 3)
    assert world.running() == ["LG TV (HDMI)", "USB DAC"] and statuses(sup) == ["running", "running"]
    assert world.spawned[-2:] == [("camilladsp.yml", -24.0, False), ("camilladsp_2.yml", -24.0, False)]
    # the subwoofer's DAC is opened for one engine alone: the other source's engine has
    # always stopped before the next one starts
    assert world.clashes == []
    sup.set_source("tv_app")
    sup.set_enabled(False)
    world.run(sup, 3)
    assert world.running() == []
    sup.set_enabled(True)
    world.run(sup, 3)
    assert world.running() == ["USB DAC"]


def test_tv_app_source_plays_the_same_speakers_from_the_tv(tmp_path, monkeypatch):
    """Speakers on their own DAC: the TV App source runs an engine on every output device,
    all of them taking the S/PDIF input; one of them says whether sound arrives."""
    world, sup = setup(tmp_path, monkeypatch)
    paths = []
    for name, out in (("tvapp.yml", "LG TV (HDMI)"), ("tvapp_2.yml", "USB DAC")):
        paths.append(tmp_path / name)
        paths[-1].write_text(yaml.safe_dump({"devices": {"capture": {"type": "Wasapi", "device": SPDIF},
                                                         "playback": {"type": "Wasapi", "device": out}}}))
    world.inputs = ["CABLE Output (VB-Audio Virtual Cable)", SPDIF]
    sup = supervisor.Supervisor(sup.files, spawn=world.spawn, ask=world.ask, outputs=lambda: world.outputs,
                                inputs=lambda: world.inputs, clock=lambda: world.now, tv_app=paths)
    world.run(sup, 3)
    assert world.running() == ["LG TV (HDMI)", "USB DAC"]
    sup.set_source("tv_app")
    world.run(sup, 3)
    assert world.running() == ["LG TV (HDMI)", "USB DAC"] and world.clashes == []
    assert [s[0] for s in world.spawned[-2:]] == ["tvapp.yml", "tvapp_2.yml"]
    assert [e.listen for e in sup.engines] == [True, False]
    # a reload with the same files keeps them running; the TV's own speakers instead (one
    # engine) start the group anew
    assert sup.reload(sup.files, paths)["source"] == "tv_app"
    assert sup.reload(sup.files, paths[:1])["source"] == "tv_app" and len(sup.engines) == 1


def test_source_survives_a_restart_and_falls_back_to_pc(tmp_path, monkeypatch):
    world, sup = setup_tv(tmp_path, monkeypatch)
    sup.set_source("tv_app")
    sup.set_source("tv_app")  # idempotent
    again = supervisor.Supervisor(sup.files, spawn=world.spawn, ask=world.ask, outputs=lambda: world.outputs,
                                  inputs=lambda: world.inputs, tv_app=sup.tv_app)
    assert again.status()["source"] == "tv_app"  # e.g. after the next login
    world.run(again, 3)
    assert world.running() == ["USB DAC"]
    # a correction applied without the TV App source: back to the PC
    plain = supervisor.Supervisor(sup.files, spawn=world.spawn, ask=world.ask, outputs=lambda: world.outputs)
    assert plain.status()["source"] == "pc" and plain.status()["sources"] == {"pc": "PC"}
    with pytest.raises(ValueError, match="TV App is not set up"):
        plain.set_source("tv_app")
    with pytest.raises(ValueError, match="no source"):
        sup.set_source("radio")


def test_renamed_spdif_input_is_followed(tmp_path, monkeypatch):
    world, sup = setup_tv(tmp_path, monkeypatch)
    sup.set_source("tv_app")
    world.inputs = ["CABLE Output (VB-Audio Virtual Cable)"]  # the sound card is unplugged
    world.run(sup, 5)
    tv = sup.status()["engines"][0]
    assert tv["status"] == "waiting" and f"{SPDIF} is not connected" in tv["detail"] and world.running() == []
    world.inputs.append("SPDIF In (3- Sound BlasterX G5)")  # back, renumbered by Windows
    world.run(sup, 3)
    assert statuses(sup) == ["running"] and world.running() == ["USB DAC"]
    conf = yaml.safe_load((tmp_path / "tvapp.yml").read_text(encoding="utf-8"))
    assert conf["devices"]["capture"]["device"] == "SPDIF In (3- Sound BlasterX G5)"
    assert conf["devices"]["playback"]["device"] == "USB DAC"


def test_no_sound_from_the_tv_is_shown(tmp_path, monkeypatch):
    from roomfix import tray
    world, sup = setup_tv(tmp_path, monkeypatch)
    world.peak = -1000.0  # nothing arrives: the TV is off, or sends Dolby instead of PCM
    world.run(sup, 5)
    assert all(e["signal"] is None for e in sup.status()["engines"])  # silence from the PC is normal
    assert tray.summary(sup.status())[0] == "on"
    sup.set_source("tv_app")
    world.run(sup, 5)
    assert sup.status()["engines"][0]["signal"] is True  # not yet: the TV may just be quiet
    world.run(sup, supervisor.QUIET_S)
    tv = sup.status()["engines"][0]
    assert tv["status"] == "running" and tv["signal"] is False and "PCM" in tv["detail"]
    state, text = tray.summary(sup.status())
    assert state == "waiting" and text.startswith("roomfix (TV App): ") and "PCM" in text and len(text) < 128
    world.peak = -35.0  # sound
    world.run(sup, 1)
    assert sup.status()["engines"][0]["signal"] is True and sup.status()["engines"][0]["detail"] == ""
    sup.set_volume(value=-20)
    assert tray.summary(sup.status()) == ("on", "roomfix (TV App): -20.0 dB")
    sup.set_source("pc")
    assert tray.summary(sup.status()) == ("on", "roomfix (PC): -20.0 dB")


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


def test_an_unanswered_question_does_not_restart_the_engine(tmp_path, monkeypatch, caplog):
    """Now and then a question to an engine goes unanswered for a second or a few while the
    engine plays on. Each restart for it was a dropout of the sound (24.11.: the subwoofer's
    engine every 30 to 180 minutes)."""
    caplog.set_level("INFO", logger="roomfix.engine")
    world, sup = setup(tmp_path, monkeypatch)
    world.run(sup, 120)
    procs = [e.proc for e in sup.engines]
    for gap in (1, 1, 2, 5, 1):  # seconds without an answer, half an hour apart
        world.deaf = world.now + gap
        world.run(sup, 1800)
    assert [e.proc for e in sup.engines] == procs and len(world.spawned) == 2
    assert statuses(sup) == ["running", "running"]
    assert "answers again, after 5 unanswered question(s) in 6 s" in caplog.text
    assert "did not answer" not in caplog.text


def test_engine_that_stops_answering_is_restarted(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch, devices=("USB DAC",))
    world.run(sup, 120)
    hung = sup.engines[0].proc
    hung.hung = True
    world.run(sup, supervisor.HUNG_S)
    assert sup.engines[0].proc is hung  # (it may still answer)
    world.run(sup, 6)
    assert hung.code is not None and sup.engines[0].proc is not hung  # killed, started again
    assert statuses(sup) == ["running"] and len(world.spawned) == 2
    # a computer that answers nothing at all for a long while: the engines are started
    # again, but not once a second
    world.deaf = world.now + 120
    world.run(sup, 120)
    assert "did not answer for" in sup.status()["engines"][0]["detail"]
    assert 3 <= len(world.spawned) - 2 <= 8


def test_engine_whose_port_is_taken_gets_another(tmp_path, monkeypatch):
    """CamillaDSP plays on without its API when another program holds its websocket port
    (and says so in its log only). Started again on that port, it would be deaf again."""
    world, sup = setup(tmp_path, monkeypatch, devices=("USB DAC",))
    eng = sup.engines[0]
    holder = socket.socket()  # (so that no new port can be this one)
    holder.bind(("127.0.0.1", eng.port))
    try:
        world.taken.add(eng.port)
        world.run(sup, 30)
        assert eng.port not in world.taken and statuses(sup) == ["running"] and len(world.spawned) == 2
    finally:
        holder.close()


def test_an_answer_counts_even_if_the_goodbye_fails():
    """engine._request: the answer is in once its frame has arrived. A connection reset
    while saying goodbye afterwards does not lose it."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def serve():
        conn, _ = server.accept()
        with conn:
            stream = conn.makefile("rb")
            while stream.readline().strip():  # the handshake
                pass
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            head = stream.read(2)
            stream.read(4 + (head[1] & 0x7F))  # mask and question
            answer = json.dumps({"GetState": {"result": "Ok", "value": "Running"}}).encode()
            conn.sendall(bytes([0x81, len(answer)]) + answer)
            stream.read(2)  # the goodbye: answered with a reset instead of a goodbye
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert engine._request(server.getsockname()[1], "GetState") == {"result": "Ok", "value": "Running"}
    finally:
        thread.join(5)
        server.close()


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
    from roomfix import logo
    for size in (192, 512):  # and the phone page has one for the home screen: the logo
        png = logo.png(size, full=True)
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (size, size)
    ico = logo.ico()  # roomfix.exe's icon, from 16 to 256 pixels
    assert ico[:4] == bytes([0, 0, 1, 0]) and struct.unpack("<H", ico[4:6])[0] == len(logo.ICO_SIZES)
    assert ico[6 + 16 * (len(logo.ICO_SIZES) - 1)] == 0  # (256 is written as 0)


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


def _serve(tmp_path, monkeypatch, make=setup):
    """A supervisor with its API, and a way to call it under any Host name."""
    world, sup = make(tmp_path, monkeypatch)
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


def test_source_is_switched_from_the_phone(tmp_path, monkeypatch):
    world, sup, thread, call = _serve(tmp_path, monkeypatch, setup_tv)
    told = []
    sup.listeners.append(lambda: told.append(sup.status()["source"]))
    try:
        code, _, page = call("/")
        assert code == 200 and b"/api/source" in page
        for _ in range(2):  # sent again after a lost reply: the same result
            code, _, status = call("/api/source", {"name": "tv_app"})
            assert code == 200 and status["source"] == "tv_app"
        assert call("/api/status")[2]["sources"] == {"pc": "PC", "tv_app": "TV App"}
        assert call("/api/source", {"name": "pc"})[2]["source"] == "pc"
        assert told == ["tv_app", "tv_app", "pc"]  # the tray shows it
        del told[:]
        for bad in ({}, {"name": 1}, {"name": "radio"}, {"name": None}):
            assert call("/api/source", bad)[0] == 400, bad
        assert call("/api/source", {"name": "tv_app"}, host="evil.example")[0] == 403
        assert call("/api/source", b'{"name": "tv_app"}', kind="text/plain")[0] == 403
        assert call("/api/status")[2]["source"] == "pc" and told == []
    finally:
        supervisor.stop()
        thread.join(10)


@pytest.mark.skipif(os.name != "nt", reason="the notification area is a Windows thing")
def test_tray_menu_switches_the_source(tmp_path, monkeypatch):
    from roomfix import tray
    world, sup = setup_tv(tmp_path, monkeypatch)
    sup.set_keys(False)  # leave the volume keys of this computer alone
    icon = tray.Tray(sup)
    icon.start()
    try:
        assert icon.ready.wait(10) and icon.hwnd
        shown = {}
        icon.u.AppendMenuW = lambda menu, flags, ident, text: shown.update({text: (ident, flags)}) or 1
        icon.u.TrackPopupMenu = lambda *args: shown[tray.SOURCE_ITEMS["tv_app"]][0]  # the user picks TV App
        icon._menu()
        assert sup.status()["source"] == "tv_app" and icon.osd_text == "Source: TV App"
        assert shown[tray.SOURCE_ITEMS["pc"]][1] & tray.MF_CHECKED  # (it was PC when the menu opened)
        icon.u.TrackPopupMenu = lambda *args: 0  # closed without a choice
        icon._menu()
        assert shown[tray.SOURCE_ITEMS["tv_app"]][1] & tray.MF_CHECKED
    finally:
        icon.close()


def test_tray_menu_has_no_source_without_tv_app(tmp_path, monkeypatch):
    from roomfix import tray
    _, sup = setup(tmp_path, monkeypatch)
    assert tray.source_name(sup.status()) == "" and tray.summary(sup.status())[1].startswith("roomfix: ")


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
    assert call("/api/source", {"name": "pc"})["state"]["playback"]["source"] == "pc"
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


def test_real_engine_whose_port_is_taken_gets_another(tmp_path, monkeypatch):
    exe = os.environ.get("CAMILLADSP") or shutil.which("camilladsp")
    if not exe:
        pytest.skip("camilladsp binary not available")
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "_exe", lambda: exe)
    monkeypatch.setattr(supervisor, "HUNG_S", 1.0)
    path = tmp_path / "camilladsp.yml"
    path.write_text(yaml.safe_dump({"devices": {
        "samplerate": 48000, "chunksize": 1024,
        "capture": {"type": "SignalGenerator", "channels": 2, "signal": {"type": "Sine", "freq": 1000, "level": -40}},
        "playback": {"type": "File", "channels": 2, "format": "S16_LE", "filename": os.devnull}}}))
    sup = supervisor.Supervisor([path])
    taken = sup.engines[0].port
    holder = socket.socket()
    holder.bind(("127.0.0.1", taken))
    holder.listen()
    try:
        end = time.monotonic() + 30
        while time.monotonic() < end and not (sup.engines[0].port != taken and statuses(sup)[0] != "waiting"
                                              and engine._request(sup.engines[0].port, "GetState")):
            sup.check()
            time.sleep(0.2)
        assert sup.engines[0].port != taken
        assert engine._request(sup.engines[0].port, "GetState") is not None
    finally:
        sup.stop()
        holder.close()


# ---- saved corrections: switching without stopping ---------------------------
def other_processing(files, gain=-3.0):
    """The same output devices, other filters: as a different correction would be."""
    for path in files:
        conf = yaml.safe_load(path.read_text())
        conf["filters"] = {"g": {"type": "Gain", "parameters": {"gain": gain}}}
        path.write_text(yaml.safe_dump(conf))


def test_new_configuration_is_taken_up_without_stopping(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch)
    sup.set_volume(value=-30)
    world.run(sup, 3)
    procs = [e.proc for e in sup.engines]
    other_processing(sup.files)
    del world.sent[:]
    sup.reload()
    assert sorted(world.reloaded) == sorted(e.port for e in sup.engines)
    world.run(sup, 3)
    assert [e.proc for e in sup.engines] == procs and len(world.spawned) == 2  # nothing stopped or started
    assert {(port, json.dumps(cmd)) for port, cmd in world.sent} >= {(e.port, '{"SetVolume": -30.0}') for e in sup.engines}
    # a saved correction names a device as it was called then: today's name goes in first
    world.outputs[0] = "2 - LG TV (HDMI)"
    sup.reload()
    assert yaml.safe_load(sup.files[0].read_text())["devices"]["playback"]["device"] == "2 - LG TV (HDMI)"
    assert len(world.reloaded) == 4 and len(world.spawned) == 2
    # an engine that refuses the new file is started again, with it
    world.reload_ok = False
    sup.reload()
    world.run(sup, 3)
    assert len(world.spawned) == 4 and statuses(sup) == ["running", "running"]


def test_other_output_devices_start_the_engines_anew(tmp_path, monkeypatch):
    world, sup = setup(tmp_path, monkeypatch)
    world.run(sup, 3)
    (tmp_path / "camilladsp_2.yml").unlink()  # say the subwoofer is now on the DAC's own channel
    sup.reload()
    world.run(sup, 3)
    assert world.running() == ["LG TV (HDMI)"] and len(sup.engines) == 1 and not world.clashes
    with pytest.raises(ValueError):
        sup.reload(files=[])  # nothing applied: nothing to reload


def test_saved_correction_is_switched_from_the_phone(tmp_path, monkeypatch):
    from roomfix import corrections
    world, sup, thread, call = _serve(tmp_path, monkeypatch)
    told = []
    sup.listeners.append(lambda: told.append(sup.status()["correction"]))
    try:
        earlier = corrections.presets()[0]["id"]  # what was applied: kept the first time
        other_processing(sup.files)
        movie = corrections.record(corrections.live_files(), name="Movie")
        corrections.use(earlier)
        code, _, status = call("/api/status")
        assert [(c["name"], c["active"]) for c in status["corrections"]] == [("Movie", False),
                                                                              ("Earlier correction", True)]
        for _ in range(2):  # sent again when the reply was lost: the same
            code, _, status = call("/api/correction", {"id": movie})
            assert code == 200 and status["correction"] == movie
            assert [c["active"] for c in status["corrections"]] == [True, False]
        assert "filters" in yaml.safe_load((tmp_path / "camilladsp.yml").read_text())
        assert len(world.reloaded) == 4 and len(world.spawned) == 2  # (no engine stopped)
        assert told == [movie, movie] and sup.switched[0] == movie  # the tray says which
        for bad in ({"id": "../settings"}, {"id": 5}, {}):
            assert call("/api/correction", bad)[0] == 400
        assert call("/api/correction", {"id": earlier}, host="evil.example")[0] == 403
        assert call("/api/status")[2]["correction"] == movie
    finally:
        supervisor.stop()
        thread.join(10)


@pytest.mark.skipif(os.name != "nt", reason="the notification area is a Windows thing")
def test_tray_menu_switches_the_saved_correction(tmp_path, monkeypatch):
    from roomfix import corrections, tray
    world, sup = setup(tmp_path, monkeypatch)
    sup.set_keys(False)  # leave the volume keys of this computer alone
    corrections.ensure()
    earlier = corrections.active()
    other_processing(sup.files)
    corrections.record(corrections.live_files())  # in use, without a name
    icon = tray.Tray(sup)
    icon.start()
    try:
        assert icon.ready.wait(10) and icon.hwnd
        shown = {}
        icon.u.AppendMenuW = lambda menu, flags, ident, text: shown.update({text: (ident, flags)}) or 1
        icon.u.TrackPopupMenu = lambda *args: shown["Earlier correction"][0]  # the user picks the preset
        icon._menu()
        assert "Unnamed (in use)" in shown and shown["Unnamed (in use)"][1] & tray.MF_CHECKED
        assert sup.status()["correction"] == earlier and icon.osd_text == "Correction: Earlier correction"
        corrections.rename(earlier, "")  # no presets left: no menu for them
        shown.clear()
        icon.u.TrackPopupMenu = lambda *args: 0
        icon._menu()
        assert "Saved corrections" not in shown
    finally:
        icon.close()


def test_apply_reloads_running_engines_and_keeps_the_old_files_when_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOMFIX_HOME", str(tmp_path))
    monkeypatch.setattr(engine, "WINDOWS", True)
    monkeypatch.setattr(engine, "_download", lambda: None)
    monkeypatch.setattr(engine, "_startup_file", lambda: tmp_path / "roomfix.vbs")
    monkeypatch.setattr(engine, "camilla_stop", lambda: pytest.fail("the engines were stopped"))
    running = {"enabled": True, "source": "pc", "engines": [{"device": "DAC", "status": "running", "detail": ""}]}
    monkeypatch.setattr(supervisor, "reload", lambda: running)
    checked = []
    monkeypatch.setattr(engine, "_check", lambda files: checked.extend(p.name for p in files))
    cfg = dict(engine.config.DEFAULT, capture_device="CABLE Output", output_device="DAC")
    files = {tmp_path / "camilladsp.yml": "new: 1\n"}
    monkeypatch.setattr(engine, "engine_files", lambda *args: files)
    (tmp_path / "camilladsp.yml").write_text("old: 1\n")
    (tmp_path / "camilladsp_2.yml").write_text("old: 2\n")
    written = []
    assert engine.apply(cfg, None, written=written.extend) == "Correction is active (CamillaDSP)."
    assert checked == ["camilladsp.yml.new"]  # checked before it replaced anything
    assert written == list(files) and sorted(p.name for p in engine._yamls()) == ["camilladsp.yml"]
    assert (tmp_path / "camilladsp.yml").read_text() == "new: 1\n" and (tmp_path / "roomfix.vbs").exists()

    def refuse(paths):
        raise RuntimeError("CamillaDSP rejected the configuration")
    monkeypatch.setattr(engine, "_check", refuse)
    files = {tmp_path / "camilladsp.yml": "broken\n", tmp_path / "camilladsp_2.yml": "broken\n"}
    with pytest.raises(RuntimeError, match="rejected"):
        engine.apply(cfg, None)
    assert (tmp_path / "camilladsp.yml").read_text() == "new: 1\n"  # what plays is left alone
    assert sorted(p.name for p in tmp_path.iterdir() if ".new" in p.name) == []


def test_page_from_the_tray_has_no_window_and_closes_when_idle():
    from roomfix import __main__
    exe, *args = supervisor.command(page=True)
    assert args[-1] == "--from-tray"
    if os.name == "nt":
        assert os.path.basename(exe).lower() == "pythonw.exe"  # no console window

    class App:
        job, seen = {"running": True}, time.monotonic() - 3600

    class Server:
        closed = False

        def shutdown(self):
            Server.closed = True

    app = App()
    done = threading.Thread(target=__main__._close_when_idle, args=(Server(), app, 0.2, 0.05), daemon=True)
    done.start()
    time.sleep(0.3)
    assert not Server.closed  # a measurement runs
    app.job = {"running": False}
    app.seen = time.monotonic()
    time.sleep(0.1)
    assert not Server.closed  # the page asked a moment ago
    done.join(2)
    assert Server.closed


def test_real_engine_takes_up_a_new_configuration_without_stopping(tmp_path, monkeypatch):
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
    sup.set_volume(value=-27)
    try:
        port = sup.engines[0].port
        for _ in range(100):
            sup.check()
            if engine._request(port, "GetVolume"):
                break
            time.sleep(0.2)
        first = sup.engines[0].proc
        other_processing([path], gain=-7.5)
        sup.reload(files=[path])
        config = yaml.safe_load(engine._ws(port, "GetConfig"))
        assert config["filters"]["g"]["parameters"]["gain"] == -7.5
        assert sup.engines[0].proc is first and first.poll() is None  # the same process, still running
        assert engine._ws(port, "GetVolume") == -27
        # a file CamillaDSP refuses: the engine keeps what it had until it is started again
        path.write_text("devices: nonsense\n")
        sup.reload(files=[path])
        assert sup.engines[0].proc is None
    finally:
        sup.stop()
