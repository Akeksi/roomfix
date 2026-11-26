"""Keeps the playback engines running and owns the master volume.

A background process (`roomfix --engine`), started by "Apply correction" and at login:

- starts one CamillaDSP engine per configuration file of the applied correction;
- restarts an engine that exits or stops. When its output device has gone (a TV that was
  switched off), it waits until the device is back, also under the new name Windows may
  have given it, and starts the engine again;
- holds all engines at the same volume and mute, and remembers them;
- switches between the sources: "PC" (what Windows plays, through the virtual cable, to
  every output device) and, if set up, "TV App" (what the TV's own apps play, which the TV
  sends over S/PDIF: the same speakers play it with the correction, or, when they are the
  TV's own, only the subwoofer's engine runs). Only the engines of the chosen source run;
- switches to a saved correction (from the phone, the tray icon or the roomfix page): its
  engine files are put in place and every running engine reloads its configuration,
  which takes effect without a pause while the output devices stay the same;
- serves a small HTTP API on 127.0.0.1 for the roomfix page and for hotkey scripts, and
  a page for the phone, which `tailscale serve` can make reachable from the tailnet.
"""
import functools
import json
import logging
import logging.handlers
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from . import config, corrections, engine

PORT = 8136  # of the HTTP API, if free. The one in use is in engine.json.
MIN_DB, MAX_DB = -60.0, 0.0
STEP_DB = 2.0  # of a volume key; half of it with Shift
RETRY_MIN, RETRY_MAX = 2.0, 30.0  # seconds before a failed engine is started again
# An engine counts as hung once it has not answered for this many seconds and this many
# questions in a row. One missed answer is no sign of trouble: a question can be lost on
# the way (AdGuard resets connections to 127.0.0.1) or come back late when the computer is
# busy, and starting the engine again for it cuts the sound for a moment.
HUNG_S, HUNG_MISSES = 10.0, 3
SOURCES = {"pc": "PC", "tv_app": "TV App"}
SIGNAL_DB = -80.0  # a peak above this on the S/PDIF input is sound
QUIET_S = 10.0  # seconds without sound before "no signal" is shown
NO_SIGNAL = "no sound from the TV: is it playing, with its optical output set to PCM?"
log = logging.getLogger("roomfix.engine")


def _plain(name):
    """A device name without the numbering Windows adds after a replug: "2 - TV", "(3- DAC)"."""
    return re.sub(r"\(\d+\s*[–-]\s*", "(", re.sub(r"^\d+\s*-\s*", "", name))


def find_device(name, available):
    """The current name of a device, or None if it is not connected."""
    if name in available:
        return name
    return next((dev for dev in available if _plain(dev) == _plain(name)), None)


def _spawn(path, port, volume, mute):
    args = [str(engine._exe()), "-p", str(port), f"--gain={volume:.1f}", *(["--mute"] if mute else []),
            "-o", str(path.with_suffix(".log")), str(path)]
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=0x08000000 if sys.platform == "win32" else 0)  # no window


_listed = {"at": -1e9, "devices": None}


def _connected(kind):
    """Names of the devices of a kind ("outputs", "inputs") connected right now, or None if
    they cannot be listed. One listing serves both kinds for a second: it is slow."""
    if time.monotonic() - _listed["at"] > 1.0:
        try:
            from . import audio
            _listed["devices"] = audio.list_devices(fresh=True)
        except Exception:
            _listed["devices"] = None
        _listed["at"] = time.monotonic()
    return [dev["name"] for dev in (_listed["devices"] or {}).get(kind, [])] or None


def _outputs():
    return _connected("outputs")


def _inputs():
    return _connected("inputs")


class Engine:
    """One CamillaDSP process. Only the supervisor's loop starts and stops it."""

    def __init__(self, sup, path, port, listen=False):
        self.sup, self.path, self.port = sup, path, port
        self.proc, self.status, self.detail = None, "stopped", ""
        self.next_try, self.retry, self.started = 0.0, RETRY_MIN, 0.0
        self.answered, self.misses = 0.0, 0  # when it last answered; questions unanswered since
        self.sent = None  # (volume, mute) the engine has
        # listen: tell when no sound arrives (the TV's S/PDIF input; silence on the virtual
        # cable just means that nothing plays). signal: True, False, or None = not known.
        self.listen, self.signal, self.heard = listen, None, 0.0
        try:
            self.device = self._playback(self._conf()).get("device") or ""
        except Exception:
            self.device = ""

    def _conf(self):
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def _playback(conf):
        return conf["devices"]["playback"]

    def _missing(self):
        """The output or input device, if it is not connected. Follows a device that Windows
        renamed ("SPDIF In (2- G5)" to "SPDIF In (3- G5)")."""
        conf = self._conf()
        missing, renamed = None, False
        for key in ("playback", "capture"):
            dev = conf["devices"].get(key) or {}
            if dev.get("type") != "Wasapi" or not dev.get("device"):
                continue
            # a loopback capture records an output device
            available = self.sup.outputs() if key == "playback" or dev.get("loopback") else self.sup.inputs()
            if available is None:
                continue
            found = find_device(dev["device"], available)
            if found is None:
                missing = missing or dev["device"]
            elif found != dev["device"]:
                log.info("%s is now called %s", dev["device"], found)
                dev["device"], renamed = found, True
        if renamed:
            self.device = self._playback(conf).get("device") or ""
            self.path.write_text(yaml.safe_dump(conf, sort_keys=False), encoding="utf-8")
        return missing

    def _start(self, now):
        missing = self._missing()
        if missing:
            if self.status != "waiting" or missing not in self.detail:
                log.info("waiting for %s", missing)
            self.status, self.detail = "waiting", f"{missing} is not connected"
            self.next_try, self.retry = now + RETRY_MIN, RETRY_MIN  # start at once when it returns
            return
        log.info("starting the engine for %s", self.device or self.path.name)
        volume, mute = self.sup.volume, self.sup.mute
        self.proc = self.sup.spawn(self.path, self.port, volume, mute)
        self.sent, self.started, self.status, self.detail = (volume, mute), now, "starting", ""
        self.answered, self.misses = now, 0  # (it has this long to open its API)
        self.signal, self.heard = None, now

    def _failed(self, now, why):
        log.warning("engine for %s %s; next try in %.0f s", self.device or self.path.name, why, self.retry)
        self.proc, self.status, self.detail = None, "waiting", why
        self.next_try, self.retry = now + self.retry, min(self.retry * 2, RETRY_MAX)

    def _last_error(self):
        try:
            lines = self.path.with_suffix(".log").read_text(errors="replace").splitlines()
            text = next(line for line in reversed(lines) if "ERROR" in line)
            return ": " + re.sub(r"\x1b\[[0-9;]*m", "", text).split("> ", 1)[-1].strip()[:200]
        except (OSError, StopIteration):
            return ""

    def stop(self):
        """Ask CamillaDSP to exit, which closes the audio device properly; kill it if it does not."""
        proc, self.proc, self.sent = self.proc, None, None
        if proc is not None and proc.poll() is None:
            self.sup.ask(self.port, "Exit")
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                pass
            if proc.poll() is None:
                proc.kill()
                proc.wait(3)
        self.status, self.detail, self.signal = "stopped", "", None

    def restart(self, now):
        self.stop()
        self.next_try, self.retry = now, RETRY_MIN

    def reload(self, now):
        """Take up the new contents of the configuration file. A running engine swaps its
        processing without stopping; one that refuses the file is started again with it."""
        try:
            self.device = self._playback(self._conf()).get("device") or ""
        except Exception:
            pass
        if self.proc is None or self.proc.poll() is not None:
            self.next_try, self.retry = now, RETRY_MIN  # (starts with the new file)
            return
        # a saved correction names the devices as they were called then: their names of
        # today go into the file first ("1 - TV" is now "2 - TV"), or the engine would let go
        # of its device to look for the old name
        try:
            missing = self._missing()
        except Exception:
            missing = None
        if missing:
            log.info("the new configuration needs %s, which is not connected", missing)
            return self.restart(now)
        reply = self.sup.ask(self.port, "Reload")
        if reply and reply.get("result") == "Ok":
            log.info("engine for %s reloaded its configuration", self.device or self.path.name)
            self.sent = None  # give it the volume again, to be sure
            self.push()
        else:
            log.warning("engine for %s did not reload (%s): starting it again", self.device or self.path.name,
                        (reply or {}).get("result"))
            self.restart(now)

    def check(self, now):
        if self.proc is not None and self.proc.poll() is not None:
            self._failed(now, "stopped" + self._last_error())
        if self.proc is None:
            if now >= self.next_try:
                self._start(now)
            return
        state = (self.sup.ask(self.port, "GetState") or {}).get("value")
        if state is None:
            self.misses += 1
            if self.misses >= HUNG_MISSES and now - self.answered > HUNG_S:
                silent = now - self.answered
                why = self._last_error() if self.answered == self.started else ""  # (this run's log)
                self.stop()
                if "websocket server" in why.lower():
                    # another program holds its port: CamillaDSP plays on, but without its
                    # API, and would do so again on the same port
                    self.port = _free_ports(1)[0]
                self._failed(now, f"did not answer for {silent:.0f} s{why}")
            return
        if self.misses and self.answered > self.started:  # (not while it was starting)
            log.info("engine for %s answers again, after %d unanswered question(s) in %.0f s",
                     self.device or self.path.name, self.misses, now - self.answered)
        self.answered, self.misses = now, 0
        if state == "Inactive":
            reason = (self.sup.ask(self.port, "GetStopReason") or {}).get("value")
            self.stop()
            self._failed(now, f"stopped: {reason}")
            return
        self.status, self.detail = ("starting" if state == "Starting" else "running"), ""
        if now - self.started > 60:
            self.retry = RETRY_MIN
        if self.listen:
            self._hear(now)
        self.push()

    def _hear(self, now):
        """Whether sound arrives at the input. An S/PDIF input is silent when the TV is off or
        paused, but also when it sends Dolby or DTS instead of PCM, which is easy to miss."""
        peaks = (self.sup.ask(self.port, {"GetCaptureSignalPeakSince": 2.0}) or {}).get("value")
        if isinstance(peaks, list) and peaks and max(peaks) > SIGNAL_DB:
            self.heard = now  # (no answer: as before. A reset connection is not silence.)
        self.signal = now - self.heard < QUIET_S
        if not self.signal:
            self.detail = NO_SIGNAL

    def push(self):
        """Give the engine the volume in force, unless it already has it."""
        want = (self.sup.volume, self.sup.mute)
        if self.proc is None or self.sent == want:
            return
        replies = [self.sup.ask(self.port, {"SetVolume": want[0]}), self.sup.ask(self.port, {"SetMute": want[1]})]
        if all(r and r.get("result") == "Ok" for r in replies):
            self.sent = want


def _paths(files):
    """A list of paths from a list, one path, or None."""
    if not files:
        return []
    return [Path(files)] if isinstance(files, (str, os.PathLike)) else [Path(p) for p in files]


class Supervisor:
    def __init__(self, files, spawn=_spawn, ask=None, outputs=_outputs, clock=time.monotonic, tv_app=None,
                 inputs=_inputs):
        """files: one configuration per output device of the PC source; tv_app: those of the
        TV App source, if it is set up (a list, or one path)."""
        self.files = list(files)
        self.tv_app = _paths(tv_app)
        self.spawn, self.ask, self.outputs, self.inputs, self.clock = spawn, ask or engine._request, outputs, inputs, clock
        self.lock = threading.Lock()  # volume and mute
        self.quit = threading.Event()
        try:
            saved = json.loads(self._volume_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        self.volume = self._clamp(saved.get("volume", 0.0))
        self.mute = bool(saved.get("mute", False))
        self.enabled = bool(saved.get("enabled", True))  # off: engines stopped, the tray icon stays
        self.keys = bool(saved.get("keys", True))  # the volume keys of the keyboard set this volume
        self.engine_lock = threading.RLock()  # the loop and a reload both change the engines
        self._build(self.files, tv_app)
        # the source chosen last time; PC if the TV App source is no longer set up
        self.source = saved.get("source") if saved.get("source") in self.groups else "pc"
        self._restart = False
        self.switched = None  # (id, when) of the last switch to a saved correction
        self.listeners = []  # told when the API changed the volume, mute or power: the tray shows it

    def _build(self, files, tv_app):
        self.files, self.tv_app = list(files), _paths(tv_app)
        ports = _free_ports(len(self.files) + len(self.tv_app))
        self.groups = {"pc": [Engine(self, path, port) for path, port in zip(self.files, ports)]}
        if self.tv_app:  # (they all hear the same input: one says whether sound arrives)
            self.groups["tv_app"] = [Engine(self, path, port, listen=i == 0) for i, (path, port)
                                     in enumerate(zip(self.tv_app, ports[len(self.files):]))]

    @property
    def engines(self):
        """The engines of the chosen source: the ones that run."""
        return self.groups[self.source]

    @staticmethod
    def _volume_file():
        return config.data_dir() / "volume.json"

    @staticmethod
    def _clamp(value):
        try:
            return round(max(MIN_DB, min(MAX_DB, float(value))), 1)
        except (TypeError, ValueError):
            return 0.0

    def _all(self):
        return [eng for group in self.groups.values() for eng in group]

    def check(self):
        """One round of the supervisor's loop: look after every engine."""
        with self.engine_lock:
            self._check()

    def _check(self):
        now = self.clock()
        restart, self._restart = self._restart, False
        source, freed = self.source, False  # (the API may switch the source meanwhile)
        # First stop what must not run: the engines of the other source hold the same
        # output devices, which an engine opens for itself alone.
        for name, group in self.groups.items():
            if self.enabled and name == source:
                continue
            for eng in group:
                try:
                    if eng.proc is not None or eng.status != "stopped":
                        eng.restart(now)  # stops it; starts at once when chosen or switched on again
                        freed = True
                except Exception:
                    log.exception("engine stop failed")
        if not self.enabled or freed:  # give Windows a moment to release the devices
            return
        for eng in self.groups[source]:
            try:
                if restart:
                    eng.restart(now)
                eng.check(now)
            except Exception:
                log.exception("engine check failed")

    def stop(self):
        with self.engine_lock:
            for eng in self._all():
                eng.stop()

    def reload(self, files=None, tv_app=None):
        """The applied correction's files have changed: every engine takes up its new
        configuration. Without a pause, unless the output devices are no longer the same
        set of files (say a subwoofer on its own device was added): then they start anew."""
        if files is None:
            files, tv_app = engine._yamls(), engine._tv_app_files()
        if not files:
            raise ValueError("no correction has been applied")
        with self.engine_lock:
            now = self.clock()
            if list(map(Path, files)) == list(map(Path, self.files)) and _paths(tv_app) == self.tv_app:
                for eng in self._all():
                    eng.reload(now)
            else:
                log.info("the correction has other output devices: starting the engines anew")
                for eng in self._all():
                    eng.stop()
                self._build(files, tv_app)
                if self.source not in self.groups:
                    self.source = "pc"
                    with self.lock:
                        self._changed()
        return self.status()

    def use_correction(self, cid):
        """Switch to a saved correction: its engine files in place, and the engines reload."""
        if not isinstance(cid, str):
            raise ValueError('send {"id": "<id of a saved correction>"}')
        with self.engine_lock:
            meta = corrections.use(cid, backend="camilladsp")
            log.info("using the saved correction %s", meta.get("name") or cid)
            self.switched = (cid, time.monotonic())  # the tray icon says which
            return self.reload()

    def _changed(self):
        self._volume_file().write_text(json.dumps({"volume": self.volume, "mute": self.mute, "enabled": self.enabled,
                                                   "keys": self.keys, "source": self.source}), encoding="utf-8")
        for eng in self._all():
            eng.push()

    def set_source(self, name):
        """Play from the PC ("pc") or from the TV's apps ("tv_app"). The engines of the other
        source stop, those of this one start, at the volume in force."""
        if name not in self.groups:
            if name in SOURCES:
                raise ValueError(f"{SOURCES[name]} is not set up: choose its input in roomfix's Setup "
                                 "and apply the correction")
            raise ValueError(f"no source {name!r}: use one of {', '.join(map(repr, self.groups))}")
        with self.lock:
            if name != self.source:
                log.info("source: %s", SOURCES[name])
            self.source = name
            self._changed()
        return self.status()

    def set_enabled(self, on=None):
        """Switch the correction off (engines stopped: no sound through it) or on again."""
        with self.lock:
            self.enabled = (not self.enabled) if on is None else bool(on)
            log.info("switched %s", "on" if self.enabled else "off")
            self._changed()
        return self.status()

    def set_keys(self, on=None):
        with self.lock:
            self.keys = (not self.keys) if on is None else bool(on)
            self._changed()
        return self.status()

    def set_volume(self, value=None, delta=None, unmute=False):
        with self.lock:
            self.volume = self._clamp(self.volume + float(delta) if delta is not None else value)
            if unmute or (delta is not None and float(delta) > 0):
                self.mute = False  # turning it up unmutes, like a volume key
            self._changed()
        return self.status()

    def set_mute(self, mute=None):
        with self.lock:
            self.mute = (not self.mute) if mute is None else bool(mute)
            self._changed()
        return self.status()

    def restart(self):
        self._restart = True
        return self.status()

    def notify(self):
        """Tell the listeners that the API changed something (from the phone page, a hotkey
        script), so the tray can show the new volume like it does for a volume key."""
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                log.exception("listener failed")

    def status(self):
        """engines: those of the chosen source. signal: False while no sound arrives at an
        input that should have it (TV App), else True or None (not known)."""
        return {"volume": self.volume, "mute": self.mute, "min_db": MIN_DB, "max_db": MAX_DB,
                "enabled": self.enabled, "keys": self.keys, "step_db": STEP_DB,
                "source": self.source, "sources": {name: SOURCES[name] for name in self.groups},
                "correction": corrections.active(),
                "engines": [{"device": e.device, "status": e.status, "detail": e.detail, "signal": e.signal}
                            for e in self.engines]}


def _free_ports(n):
    import socket
    socks = [socket.socket() for _ in range(n)]
    try:
        for sock in socks:
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in socks]
    finally:
        for sock in socks:
            sock.close()


# ---- the background process ----------------------------------------------
def _info_file():
    return config.data_dir() / "engine.json"


def _info():
    try:
        return json.loads(_info_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _forget():
    for _ in range(20):
        try:
            return _info_file().unlink(missing_ok=True)
        except OSError:  # Windows: someone is reading it right now
            time.sleep(0.05)


# ---- the HTTP API and the phone page --------------------------------------
PHONE = Path(__file__).parent / "web" / "phone"
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json")}
_LOCAL = r"(?:127\.0\.0\.1|localhost)"
_TAILSCALE = r"(?:[a-z0-9-]+\.)+ts\.net\.?"  # the name `tailscale serve` passes on: pc.tailnet.ts.net


def local_host(host):
    """Whether a Host header names this computer."""
    return re.fullmatch(_LOCAL + r"(?::\d{1,5})?", host or "", re.I) is not None


def allowed_host(host):
    """Whether a Host header names this computer or its Tailscale name. A website that points
    its own name at 127.0.0.1 (DNS rebinding) sends its own name, and is refused."""
    return re.fullmatch(f"(?:{_LOCAL}|{_TAILSCALE})" + r"(?::\d{1,5})?", host or "", re.I) is not None


def _number(value):
    if isinstance(value, bool) or not math.isfinite(number := float(value)):
        raise ValueError(f"not a number of dB: {value!r}")
    return number


def _switch(value):
    """true or false; None for "toggle", or when not given."""
    if value is None or value == "toggle":
        return None
    if isinstance(value, bool) or value in (0, 1):
        return bool(value)
    raise ValueError(f'expected true, false or "toggle", not {value!r}')


@functools.lru_cache(maxsize=None)
def _icon(size):
    from . import logo
    return logo.png(size, full=True)  # the phone rounds the corners itself


def _file(path):
    """(data, type) of a file of the phone page, or None."""
    if path in FILES:
        name, kind = FILES[path]
        return (PHONE / name).read_bytes(), kind
    if path in ("/icon-192.png", "/icon-512.png"):
        return _icon(int(path[6:9])), "image/png"
    return None


def _with_presets(status):
    """The status, with the saved corrections that can be switched to: the presets (those
    with a name) and the one in use."""
    try:
        status["corrections"] = [{"id": e["id"], "name": e["name"], "active": e["active"]}
                                 for e in corrections.presets() if e["backend"] == "camilladsp"]
    except OSError:
        log.exception("saved corrections could not be listed")
        status["corrections"] = []
    return status


def _handler(sup):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")  # no other site frames it
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not allowed_host(self.headers.get("Host")):
                return self._send(403, {"error": "forbidden"})
            path = self.path.split("?")[0]
            if path == "/api/status":
                return self._send(200, _with_presets(sup.status()))
            file = _file(path)
            if file:
                return self._send(200, *file)
            self._send(404, {"error": "not found"})

        def do_POST(self):
            # A web page cannot send JSON to another site without asking first (CORS), and
            # nothing here answers that question: other websites cannot touch the volume.
            host = self.headers.get("Host")
            if not allowed_host(host) or not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(403, {"error": "forbidden: send application/json"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("send a JSON object")
                path = self.path.split("?")[0]
                if path == "/api/volume":
                    unmute = body.get("unmute") is True
                    if "delta" in body:
                        result = sup.set_volume(delta=_number(body["delta"]), unmute=unmute)
                    else:
                        result = sup.set_volume(value=_number(body["value"]), unmute=unmute)
                elif path == "/api/mute":
                    result = sup.set_mute(_switch(body.get("mute")))
                elif path == "/api/restart":
                    result = sup.restart()
                elif path == "/api/power":
                    result = sup.set_enabled(_switch(body.get("on")))
                elif path == "/api/source":
                    if not isinstance(body.get("name"), str):
                        raise ValueError('send {"name": "pc"} or {"name": "tv_app"}')
                    result = sup.set_source(body["name"])
                elif path == "/api/correction":
                    result = sup.use_correction(body.get("id"))
                elif path == "/api/reload":
                    result = sup.reload()
                elif path == "/api/quit":
                    if not local_host(host):  # from the phone there would be no way to start it again
                        return self._send(403, {"error": "forbidden: quit from this computer"})
                    sup.quit.set()
                    result = {"ok": True}
                else:
                    return self._send(404, {"error": "not found"})
                if path in ("/api/volume", "/api/mute", "/api/power", "/api/source", "/api/correction"):
                    sup.notify()
                self._send(200, _with_presets(result) if "engines" in result else result)
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
            except OSError as exc:  # a file of the correction could not be written
                log.exception("request %s failed", self.path)
                self._send(500, {"error": str(exc)})

    return Handler


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows the default would let two processes share the port
    daemon_threads = True

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _bind(handler, port, tries=10, pause=0.5):
    """The API's server, on the usual port if at all possible: `tailscale serve` sends the
    phone there. A supervisor that was just dismissed can hold it a moment longer."""
    for attempt in range(tries):
        try:
            return _Server(("127.0.0.1", port), handler)
        except OSError:
            if attempt + 1 < tries:
                time.sleep(pause)
    log.warning("port %d is taken by another program: the API is on another port, and the phone "
                "page cannot be reached through tailscale serve", port)
    return _Server(("127.0.0.1", 0), handler)


def run(sup, port=PORT):
    """Serve the API and look after the engines until told to quit."""
    server = _bind(_handler(sup), port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    me = {"port": server.server_address[1], "pid": os.getpid(), "started": time.time()}
    _info_file().write_text(json.dumps(me), encoding="utf-8")
    log.info("looking after %d engine(s), source %s, API at http://127.0.0.1:%d", len(sup._all()),
             SOURCES[sup.source], me["port"])
    try:
        # engine.json names the supervisor in charge. If it is removed or replaced, this one
        # has been dismissed, even if the request to quit never arrived.
        while _info() == me:
            sup.check()
            if sup.quit.wait(1.0):
                break
    finally:
        sup.stop()
        if _info() == me:
            _forget()
        server.shutdown()
        server.server_close()
        log.info("stopped")


def main():
    """Entry point of `roomfix --engine`."""
    handler = logging.handlers.RotatingFileHandler(config.data_dir() / "engine.log", maxBytes=500_000,
                                                   backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    files = engine._yamls()
    if request("/api/status") is not None or not files:
        return  # already running, or no correction has been applied
    try:
        corrections.ensure()  # the correction applied before there was a history: a preset
        sup = Supervisor(files, tv_app=engine._tv_app_files())
        icon = None
        if sys.platform == "win32":
            from . import tray
            icon = tray.Tray(sup)  # the icon in the notification area, and the volume keys
            icon.start()
        try:
            run(sup)
        finally:
            if icon:
                icon.close()
    except Exception:
        log.exception("supervisor failed")
        raise


# ---- talking to the background process ------------------------------------
def request(path, body=None, timeout=1.5):
    """Call the running supervisor's API. Returns the reply, or None if there is none."""
    port = _info().get("port")
    if not port:
        return None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    # Network filters (AdGuard) sometimes reset a connection to 127.0.0.1. Asking again is
    # safe for a question, not for a change such as "toggle mute"; and a supervisor that
    # seems to be missing would be started a second time.
    for attempt in range(3 if body is None else 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as reply:
                return json.loads(reply.read())
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ConnectionRefusedError):
                if time.time() - _info().get("started", 0) > 30:  # left behind by one that died
                    _forget()
                return None
            if not isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
                return None
    return None


def command(page=False):
    """The command line that starts the supervisor, or (page=True) the roomfix page from the
    tray icon: without a window, closing by itself once the page has been closed a while."""
    args = ["--from-tray"] if page else ["--engine"]
    if getattr(sys, "frozen", False):  # roomfix.exe
        return [sys.executable, *args]
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return [exe if os.path.exists(exe) else sys.executable, "-m", "roomfix", *args]


def switch_on():
    """Before a start: a correction that was switched off from the tray icon is on again."""
    try:
        saved = json.loads(Supervisor._volume_file().read_text(encoding="utf-8"))
        if saved.get("enabled") is False:
            Supervisor._volume_file().write_text(json.dumps(dict(saved, enabled=True)), encoding="utf-8")
    except (OSError, ValueError):
        pass


def start(timeout=25.0):
    """Start the supervisor in the background. Returns its status once the engines are up
    or waiting for their devices."""
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # detached, own process group
    subprocess.Popen(command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    return wait_ready(timeout)


def _ready(status):
    return bool(status) and all(e["status"] in ("running", "waiting") or e.get("signal") is False
                                for e in status["engines"])  # (a TV that is off sends nothing)


def wait_ready(timeout=25.0, status=None):
    """The supervisor's status once its engines are up or waiting for their devices (or
    the last one seen, when that takes longer than timeout)."""
    end = time.monotonic() + timeout
    while not _ready(status) and time.monotonic() < end:
        time.sleep(0.3)
        status = request("/api/status") or status
    return status


def reload():
    """Tell a running supervisor that the applied correction's files have changed, and switch
    the correction on. Returns its status once the engines are ready, or None if no
    supervisor answers."""
    status = request("/api/reload", {}, timeout=15)
    if status is None:
        return None
    if not status.get("enabled"):
        status = request("/api/power", {"on": True}) or status
    return wait_ready(status=status)


def stop(timeout=8.0, closing=5.0):
    """Stop the supervisor and, with it, the engines. Returns once its process has ended
    (or after `closing` more seconds): it removes engine.json first, then closes its API and
    its tray icon. Whoever stops it may end every roomfix.exe next (the installer does), and
    one ended halfway would leave its icon behind in the notification area."""
    info = _info()
    if not info:
        return
    answered = request("/api/quit", {}) is not None
    end = time.monotonic() + timeout
    while _info() and time.monotonic() < end:
        time.sleep(0.1)
    if _info():  # it does not answer: dismiss it, and give it a moment to notice
        _forget()
        time.sleep(1.5)
    elif answered and info.get("pid") and info["pid"] != os.getpid():  # (in tests it is a thread)
        engine.wait_exit(info["pid"], closing)
