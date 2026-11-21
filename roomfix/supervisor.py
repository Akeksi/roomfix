"""Keeps the playback engines running and owns the master volume.

A background process (`roomfix --engine`), started by "Apply correction" and at login:

- starts one CamillaDSP engine per configuration file of the applied correction;
- restarts an engine that exits or stops. When its output device has gone (a TV that was
  switched off), it waits until the device is back, also under the new name Windows may
  have given it, and starts the engine again;
- holds all engines at the same volume and mute, and remembers them;
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

from . import config, engine

PORT = 8136  # of the HTTP API, if free. The one in use is in engine.json.
MIN_DB, MAX_DB = -60.0, 0.0
STEP_DB = 2.0  # of a volume key; half of it with Shift
RETRY_MIN, RETRY_MAX = 2.0, 30.0  # seconds before a failed engine is started again
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


def _outputs():
    """Names of the output devices connected right now, or None if they cannot be listed."""
    try:
        from . import audio
        return [dev["name"] for dev in audio.list_devices(fresh=True)["outputs"]] or None
    except Exception:
        return None


class Engine:
    """One CamillaDSP process. Only the supervisor's loop starts and stops it."""

    def __init__(self, sup, path, port):
        self.sup, self.path, self.port = sup, path, port
        self.proc, self.status, self.detail = None, "stopped", ""
        self.next_try, self.retry, self.started = 0.0, RETRY_MIN, 0.0
        self.sent = None  # (volume, mute) the engine has
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
        """The output device, if it is not connected. Follows a device that Windows renamed."""
        conf = self._conf()
        play = self._playback(conf)
        outputs = self.sup.outputs() if play.get("type") == "Wasapi" and play.get("device") else None
        if outputs is None:
            return None
        found = find_device(play["device"], outputs)
        if found is None:
            return play["device"]
        if found != play["device"]:
            log.info("%s is now called %s", play["device"], found)
            play["device"] = self.device = found
            self.path.write_text(yaml.safe_dump(conf, sort_keys=False), encoding="utf-8")
        return None

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
        self.status, self.detail = "stopped", ""

    def restart(self, now):
        self.stop()
        self.next_try, self.retry = now, RETRY_MIN

    def check(self, now):
        if self.proc is not None and self.proc.poll() is not None:
            self._failed(now, "stopped" + self._last_error())
        if self.proc is None:
            if now >= self.next_try:
                self._start(now)
            return
        state = (self.sup.ask(self.port, "GetState") or {}).get("value")
        if state is None:
            if now - self.started > 10:
                self.stop()
                self._failed(now, "did not answer")
            return
        if state == "Inactive":
            reason = (self.sup.ask(self.port, "GetStopReason") or {}).get("value")
            self.stop()
            self._failed(now, f"stopped: {reason}")
            return
        self.status, self.detail = ("starting" if state == "Starting" else "running"), ""
        if now - self.started > 60:
            self.retry = RETRY_MIN
        self.push()

    def push(self):
        """Give the engine the volume in force, unless it already has it."""
        want = (self.sup.volume, self.sup.mute)
        if self.proc is None or self.sent == want:
            return
        replies = [self.sup.ask(self.port, {"SetVolume": want[0]}), self.sup.ask(self.port, {"SetMute": want[1]})]
        if all(r and r.get("result") == "Ok" for r in replies):
            self.sent = want


class Supervisor:
    def __init__(self, files, spawn=_spawn, ask=None, outputs=_outputs, clock=time.monotonic):
        self.files = list(files)
        self.spawn, self.ask, self.outputs, self.clock = spawn, ask or engine._request, outputs, clock
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
        self.engines = [Engine(self, path, port) for path, port in zip(self.files, _free_ports(len(self.files)))]
        self._restart = False
        self.listeners = []  # told when the API changed the volume, mute or power: the tray shows it

    @staticmethod
    def _volume_file():
        return config.data_dir() / "volume.json"

    @staticmethod
    def _clamp(value):
        try:
            return round(max(MIN_DB, min(MAX_DB, float(value))), 1)
        except (TypeError, ValueError):
            return 0.0

    def check(self):
        """One round of the supervisor's loop: look after every engine."""
        now = self.clock()
        restart, self._restart = self._restart, False
        for eng in self.engines:
            try:
                if not self.enabled:
                    if eng.proc is not None or eng.status != "stopped":
                        eng.restart(now)  # stops it; starts at once when switched on again
                    continue
                if restart:
                    eng.restart(now)
                eng.check(now)
            except Exception:
                log.exception("engine check failed")

    def stop(self):
        for eng in self.engines:
            eng.stop()

    def _changed(self):
        self._volume_file().write_text(json.dumps({"volume": self.volume, "mute": self.mute,
                                                   "enabled": self.enabled, "keys": self.keys}), encoding="utf-8")
        for eng in self.engines:
            eng.push()

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
        return {"volume": self.volume, "mute": self.mute, "min_db": MIN_DB, "max_db": MAX_DB,
                "enabled": self.enabled, "keys": self.keys, "step_db": STEP_DB,
                "engines": [{"device": e.device, "status": e.status, "detail": e.detail} for e in self.engines]}


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
    from . import tray
    return tray.app_icon(size)


def _file(path):
    """(data, type) of a file of the phone page, or None."""
    if path in FILES:
        name, kind = FILES[path]
        return (PHONE / name).read_bytes(), kind
    if path in ("/icon-192.png", "/icon-512.png"):
        return _icon(int(path[6:9])), "image/png"
    return None


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
                return self._send(200, sup.status())
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
                elif path == "/api/quit":
                    if not local_host(host):  # from the phone there would be no way to start it again
                        return self._send(403, {"error": "forbidden: quit from this computer"})
                    sup.quit.set()
                    result = {"ok": True}
                else:
                    return self._send(404, {"error": "not found"})
                if path in ("/api/volume", "/api/mute", "/api/power"):
                    sup.notify()
                self._send(200, result)
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": str(exc)})

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
    log.info("looking after %d engine(s), API at http://127.0.0.1:%d", len(sup.engines), me["port"])
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
        sup = Supervisor(files)
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
    """The command line that starts the supervisor, or (page=True) the roomfix page."""
    args = [] if page else ["--engine"]
    if getattr(sys, "frozen", False):  # roomfix.exe
        return [sys.executable, *args]
    exe = os.path.join(os.path.dirname(sys.executable), "python.exe" if page else "pythonw.exe")
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
    end, status = time.monotonic() + timeout, None
    while time.monotonic() < end:
        time.sleep(0.3)
        status = request("/api/status")
        if status and all(e["status"] in ("running", "waiting") for e in status["engines"]):
            break
    return status


def stop(timeout=8.0):
    """Stop the supervisor and, with it, the engines."""
    if not _info():
        return
    request("/api/quit", {})
    end = time.monotonic() + timeout
    while _info() and time.monotonic() < end:
        time.sleep(0.1)
    if _info():  # it does not answer: dismiss it, and give it a moment to notice
        _forget()
        time.sleep(1.5)
