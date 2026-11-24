"""Local web UI: a small JSON API plus static files, bound to 127.0.0.1 only."""
import copy
import json
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from . import audio, config, corrections, design, dsp, engine, measure, sim

WEB = Path(__file__).parent / "web"
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
TARGETS = {(0.0, 0.0): "Flat", (-0.5, 0.0): "Slight downward tilt", (-0.8, 3.0): "Balanced", (-1.0, 6.0): "Warm"}


class App:
    def __init__(self, demo=False):
        self.demo = demo
        if demo:
            engine.WINDOWS = False  # demo never touches the real audio engine
        self.cfg = config.demo() if demo else config.load()
        if not demo and not self.cfg["capture_device"]:  # first run: find the virtual cable
            self.cfg["capture_device"] = config.guess_capture(self.devices()["inputs"])
        self.meas = self.verify_irs = self.design = self.meas_id = None
        self.design_version = 0
        self.job = {"running": False, "action": "", "progress": 0, "message": "", "active": None,
                    "prompt": None, "error": None, "result": None}
        self._answer = threading.Event()
        self._answer_ok = False
        self._cancel = False
        self._span = (0, 1)
        self.lock = threading.RLock()
        self.seen = time.monotonic()  # when the page last asked something
        self.settings_version = 0  # changes when the settings change here, not on the page
        self.note = ""  # about the saved correction in use, when the page cannot show it
        corrections.ensure()
        if not demo:
            self._load_measurement()

    # ---- helpers ---------------------------------------------------------
    def devices(self):
        if self.demo:
            return {"outputs": [{"name": self.cfg["output_device"], "channels": 8}],
                    "inputs": [{"name": self.cfg["input_device"], "channels": 1},
                               {"name": "Simulated S/PDIF In", "channels": 2}]}
        return audio.list_devices()

    def channels(self):
        return {dev["name"]: dev["channels"] for dev in self.devices()["outputs"]}

    def errors(self):
        channels, other = self.channels(), config.sub_device(self.cfg)
        errs = config.validate(self.cfg, channels.get(self.cfg["output_device"]), channels.get(other))
        if other and other not in channels:
            errs.insert(0, "The subwoofer's output device is not connected.")
        if not self.cfg["output_device"]:
            errs.insert(0, "Select the DAC.")
        if not self.cfg["input_device"]:
            errs.insert(0, "Select the measurement microphone.")
        return errs

    def calibration(self):
        path = config.data_dir() / "mic_cal.txt"
        if self.cfg["mic_cal_name"] and path.exists():
            return dsp.load_calibration(path.read_text(encoding="utf-8", errors="replace"))
        return None

    def layout(self):
        """Everything a measurement depends on; if it changes, measure again."""
        c = self.cfg
        return json.dumps([c["output_device"], c["input_device"], c["input_channel"], c["sample_rate"],
                           [s["channels"] for s in c["speakers"]], [w.get("min_freq") for w in c["ways"]],
                           [c["sub"]["device"], c["sub"]["channel"]] if config.sub(c) else None])

    def _save_measurement(self):
        arrays = {k: np.array(v) for k, v in self.meas["irs"].items()}
        meta = {k: v for k, v in self.meas.items() if k != "irs"}
        path = config.data_dir() / "measurement.npz"
        np.savez_compressed(path, meta=json.dumps(meta), **arrays)
        self.meas_id = corrections.measurement_id(path)

    def _load_measurement(self):
        path = config.data_dir() / "measurement.npz"
        if not path.exists():
            return
        self.meas_id = corrections.measurement_id(path)
        try:
            data = np.load(path)
            meas = json.loads(str(data["meta"]))
            meas["irs"] = {k: list(data[k]) for k in data.files if k != "meta"}
            if meas.get("layout") == self.layout():
                self.meas = meas
                self.redesign()
        except Exception:
            traceback.print_exc()

    def redesign(self, keep_verify=False):
        if not keep_verify:
            self.verify_irs = None
        if self.meas is None or self.meas.get("layout") != self.layout() or config.validate(self.cfg):
            self.design = None
        else:
            self.design = design.design(self.cfg, self.meas, self.calibration(), self.verify_irs)
        self.design_version += 1

    def state(self):
        self._follow()
        layout = self.layout()
        saved = [dict(e, editable=e["editable"] and e.pop("layout") == layout) for e in corrections.entries()]
        return {
            "cfg": self.cfg, "demo": self.demo, "devices": self.devices(), "errors": self.errors(),
            "job": self.job, "engine": engine.status(self.cfg), "design_version": self.design_version,
            "playback": engine.playback() if self.cfg["backend"] == "camilladsp" else None,
            "has_design": self.design is not None, "way_names": config.WAY_NAMES,
            "wiring_ok": len(self.cfg["ways"]) == 1 or self.cfg["wiring_confirmed"] == config.wiring_key(self.cfg),
            "corrections": saved, "settings_version": self.settings_version, "correction_note": self.note,
            "unapplied": self.unapplied(),
        }

    def unapplied(self):
        """Whether the result on the page is not the correction in use: changed settings, or
        a new measurement, that have not been applied."""
        if self.design is None or self.note:
            return False
        try:
            meta = corrections.get(corrections.active())
        except ValueError:
            return True
        return (meta.get("settings") != corrections.settings_of(self.cfg)
                or meta.get("measurement") != self.meas_id or meta.get("layout") != self.layout())

    # ---- saved corrections -------------------------------------------------
    def summary(self):
        """A line about what shapes the correction, for the list of saved corrections."""
        c, t = self.cfg, self.cfg["target"]
        name = TARGETS.get((float(t["tilt"]), float(t["bass_boost"])))
        parts = [(name + ": " if name else "") + f"tilt {t['tilt']:+g} dB/oct, bass {t['bass_boost']:+g} dB"]
        if t.get("points"):
            parts.append("own curve")
        if t.get("f_min") or t.get("f_max"):
            parts.append(f"corrects {t.get('f_min') or 'auto'} to {t.get('f_max') or 'auto'} Hz")
        if len(c["ways"]) > 1:
            parts.append("crossover " + " / ".join(f"{f:g}" for f in c["crossover"]["freqs"]) + " Hz")
        if config.sub(c):
            parts.append(f"subwoofer from {c['sub']['crossover']:g} Hz")
        n = design.count_changes(c)
        if n:
            parts.append(f"{n} change{'s' if n > 1 else ''} by hand")
        return "; ".join(parts)

    def _record(self, paths):
        """Keep the correction that was just applied (Apply), with what made it."""
        meas = config.data_dir() / "measurement.npz"
        cid = corrections.record(paths, settings=corrections.settings_of(self.cfg), layout=self.layout(),
                                 measurement=meas if meas.exists() else None, summary=self.summary())
        self.cfg["correction"], self.note = cid, ""
        self._save()

    def _save(self):
        if not self.demo:
            config.save(self.cfg)

    def _follow(self):
        """Another saved correction is in use (switched from the phone, the tray icon, or
        this page): show it, with its settings and measurement, so that it can be changed."""
        cid = corrections.active()
        if not cid or cid == self.cfg.get("correction") or self.job["running"]:
            return
        with self.lock:
            try:
                meta = corrections.get(cid)
            except ValueError:
                return
            self.cfg["correction"], self.note = cid, ""
            if not (meta.get("settings") and meta.get("measurement")):
                self.note = ("This saved correction plays as it was saved. It was applied before roomfix kept "
                             "its settings, so the result below is not the one playing.")
            elif meta.get("layout") != self.layout():
                self.note = ("This saved correction was made with another setup (devices or channels). It plays "
                             "as saved; the result below is not the one playing.")
            else:
                corrections.restore_settings(self.cfg, meta["settings"])
                path, mine = corrections.measurement_file(meta["measurement"]), config.data_dir() / "measurement.npz"
                if path and self.meas_id != meta["measurement"]:
                    tmp = mine.with_name("measurement.npz.new")
                    shutil.copyfile(path, tmp)
                    config.replace_file(tmp, mine)
                    self.meas = None
                    self._load_measurement()  # (designs it)
                else:
                    self.redesign()
            self._save()
            self.settings_version += 1

    def use_correction(self, cid):
        """Switch to a saved correction. Returns a message for the user."""
        with self.lock:
            if self.job["running"]:
                raise RuntimeError("Wait until the measurement has finished.")
            corrections.use(cid, backend=self.cfg["backend"])
        message = engine.switch_to_files(self.cfg)
        self._follow()
        return message

    def rename_correction(self, cid, name):
        corrections.rename(cid, name)

    def delete_correction(self, cid):
        corrections.delete(cid)

    # ---- actions ---------------------------------------------------------
    def set_config(self, new):
        with self.lock:
            if self.job["running"]:
                raise RuntimeError("Wait until the measurement has finished.")
            errs = config.adjust_errors(new.get("adjust", self.cfg["adjust"]))
            if errs:
                raise RuntimeError(errs[0])
            for key in config.DEFAULT:
                if key in new and key not in ("wiring_confirmed", "mic_cal_name", "correction"):
                    self.cfg[key] = new[key]
            self._save()
            self.redesign()

    def set_calibration(self, name, text):
        if text:
            dsp.load_calibration(text)  # validate
            (config.data_dir() / "mic_cal.txt").write_text(text, encoding="utf-8")
        self.cfg["mic_cal_name"] = name if text else ""
        if not self.demo:
            config.save(self.cfg)
        self.redesign()

    def apply(self):
        if self.design is None:
            raise RuntimeError("Measure first.")
        corrections.ensure()  # (keeps the correction applied before there was a history)
        return engine.apply(self.cfg, self.design, self.channels(), written=self._record)

    def start(self, action, auto_apply=True):
        with self.lock:
            if self.job["running"]:
                raise RuntimeError("Already running.")
            errs = self.errors()
            if errs:
                raise RuntimeError(errs[0])
            self._cancel = False
            self.job.update(running=True, action=action, progress=0, message="Starting", active=None,
                            prompt=None, error=None, result=None)
        threading.Thread(target=self._run, args=(action, auto_apply), daemon=True).start()

    def answer(self, ok):
        self._answer_ok = ok
        self._answer.set()

    def cancel(self):
        self._cancel = True
        self.answer(False)

    def _progress(self, frac, msg, active=None):
        if self._cancel:
            raise measure.Cancelled()
        lo, hi = self._span
        self.job.update(progress=lo + (hi - lo) * frac, message=msg, active=active)

    def _prompt(self, msg):
        self._answer.clear()
        self.job["prompt"] = msg
        self._answer.wait()
        self.job["prompt"] = None
        return self._answer_ok

    def _run(self, action, auto_apply):
        try:
            backend = sim.SimAudio(self.cfg, realtime=True) if self.demo else audio.Audio(self.cfg)
            with engine.paused(self.cfg):
                # the levels the last measurement found with this setup: the search starts near them
                previous = self.meas.get("levels") if self.meas and self.meas.get("layout") == self.layout() else None
                m = measure.Measurer(self.cfg, backend, self._progress, self._prompt, previous=previous)
                multi = len(self.cfg["ways"]) > 1
                if action == "identify" or (multi and not self.state()["wiring_ok"]):
                    self._span = (0, 0.15 if action != "identify" else 1)
                    m.identify()
                    if multi:
                        if not self._prompt("Did every sound come from the driver named on screen? "
                                            "If a tweeter stayed silent or the wrong driver played, answer No "
                                            "and fix the channel assignment."):
                            raise measure.Cancelled()
                        self.cfg["wiring_confirmed"] = config.wiring_key(self.cfg)
                        if not self.demo:
                            config.save(self.cfg)
                if action == "measure":
                    self._span = (0.15, 0.8)
                    meas = m.measure()
                    meas["layout"] = self.layout()
                    self.meas = meas
                    self._save_measurement()  # (the demo's: in its own temporary folder)
                    # changes by hand were made for the previous measurement; it is kept,
                    # with them, in the saved corrections
                    self.cfg["adjust"] = copy.deepcopy(config.DEFAULT["adjust"])
                    self._save()
                    self.settings_version += 1
                    self.redesign()
                if action in ("measure", "verify"):
                    if self.design is None:
                        raise RuntimeError("Measure first.")
                    self._span = (0.8, 1.0) if action == "measure" else (0, 1)
                    self.verify_irs = m.verify(self.design, self.meas["levels"])
                    self.redesign(keep_verify=True)
            result = "Wiring confirmed." if action == "identify" else "Done."
            if action == "measure":
                result = "Measured and corrected. " + (self.apply() if auto_apply else "Press Apply to use it.")
            self.job.update(result=result, progress=1)
        except measure.Cancelled:
            self.job["error"] = "Cancelled."
        except Exception as exc:
            traceback.print_exc()
            self.job["error"] = str(exc)
        finally:
            self.job.update(running=False, prompt=None, active=None)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _local(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def do_GET(self):
            app.seen = time.monotonic()
            if not self._local():
                return self._send(403, {"error": "forbidden"})
            path = self.path.split("?")[0]
            if path == "/api/state":
                return self._send(200, app.state())
            if path == "/api/design":
                return self._send(200, design.to_json(app.design) if app.design else None)
            if path == "/logo.png":  # the browser tab's icon
                from . import logo
                return self._send(200, logo.png(64), "image/png")
            file = WEB / ("index.html" if path == "/" else path.lstrip("/"))
            if file.suffix in TYPES and file.is_file() and file.parent == WEB:
                return self._send(200, file.read_bytes(), TYPES[file.suffix])
            self._send(404, {"error": "not found"})

        def do_POST(self):
            app.seen = time.monotonic()
            # the custom header keeps other websites from triggering actions
            if not self._local() or self.headers.get("X-Roomfix") != "1":
                return self._send(403, {"error": "forbidden"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            try:
                message = None
                if self.path == "/api/config":
                    app.set_config(body)
                elif self.path == "/api/calibration":
                    app.set_calibration(body.get("name", ""), body.get("text", ""))
                elif self.path == "/api/run":
                    app.start(body["action"], body.get("auto_apply", True))
                elif self.path == "/api/answer":
                    app.answer(bool(body.get("ok")))
                elif self.path == "/api/cancel":
                    app.cancel()
                elif self.path == "/api/apply":
                    message = app.apply()
                elif self.path == "/api/bypass":
                    message = engine.bypass(app.cfg)
                elif self.path in ("/api/volume", "/api/mute", "/api/source"):
                    engine.set_playback(self.path, body)
                elif self.path == "/api/correction/use":
                    message = app.use_correction(body.get("id"))
                elif self.path == "/api/correction/rename":
                    app.rename_correction(body.get("id"), body.get("name", ""))
                elif self.path == "/api/correction/delete":
                    app.delete_correction(body.get("id"))
                else:
                    return self._send(404, {"error": "not found"})
                self._send(200, {"ok": True, "message": message, "state": app.state()})
            except Exception as exc:
                self._send(400, {"error": str(exc)})

    return Handler


class Server(ThreadingHTTPServer):
    # Python's default lets a second roomfix bind the same port on Windows, after which
    # either instance answers at random. One instance only.
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(app, port=8137):
    return Server(("127.0.0.1", port), make_handler(app))


def _running(port):
    """What answers on the port: True/False = a roomfix (demo or not), None = something else."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return bool(json.loads(r.read())["demo"])
    except Exception:
        return None


def open_server(app, port=None, default=8137):
    """Start the server. Returns (server, url), or (None, url) if this kind of roomfix
    (normal or demo) is already running at url.

    Without an explicit port, a busy port is skipped unless the same kind of roomfix has
    it: a demo next to a normal roomfix gets its own port instead of opening the other.
    """
    if port is not None:
        return serve(app, port), f"http://127.0.0.1:{port}/"
    for p in range(default, default + 20):
        url = f"http://127.0.0.1:{p}/"
        try:
            return serve(app, p), url
        except OSError:
            if _running(p) == app.demo:
                return None, url
    raise OSError("no free port")
