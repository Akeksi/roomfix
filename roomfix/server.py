"""Local web UI: a small JSON API plus static files, bound to 127.0.0.1 only."""
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

from . import audio, config, design, dsp, engine, measure, sim

WEB = Path(__file__).parent / "web"
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}


class App:
    def __init__(self, demo=False):
        self.demo = demo
        if demo:
            engine.WINDOWS = False  # demo never touches the real audio engine
        self.cfg = config.demo() if demo else config.load()
        self.meas = self.verify_irs = self.design = None
        self.design_version = 0
        self.job = {"running": False, "action": "", "progress": 0, "message": "", "active": None,
                    "prompt": None, "error": None, "result": None}
        self._answer = threading.Event()
        self._answer_ok = False
        self._cancel = False
        self._span = (0, 1)
        self.lock = threading.Lock()
        self._load_measurement()

    # ---- helpers ---------------------------------------------------------
    def devices(self):
        if self.demo:
            return {"outputs": [{"name": self.cfg["output_device"], "channels": 8}],
                    "inputs": [{"name": self.cfg["input_device"], "channels": 1}]}
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
        np.savez_compressed(config.data_dir() / "measurement.npz", meta=json.dumps(meta), **arrays)

    def _load_measurement(self):
        path = config.data_dir() / "measurement.npz"
        if self.demo or not path.exists():
            return
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
        return {
            "cfg": self.cfg, "demo": self.demo, "devices": self.devices(), "errors": self.errors(),
            "job": self.job, "engine": engine.status(self.cfg), "design_version": self.design_version,
            "has_design": self.design is not None, "way_names": config.WAY_NAMES,
            "wiring_ok": len(self.cfg["ways"]) == 1 or self.cfg["wiring_confirmed"] == config.wiring_key(self.cfg),
        }

    # ---- actions ---------------------------------------------------------
    def set_config(self, new):
        with self.lock:
            if self.job["running"]:
                raise RuntimeError("Wait until the measurement has finished.")
            for key in config.DEFAULT:
                if key in new and key not in ("wiring_confirmed", "mic_cal_name"):
                    self.cfg[key] = new[key]
            if not self.demo:
                config.save(self.cfg)
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
        return engine.apply(self.cfg, self.design, self.channels())

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
                m = measure.Measurer(self.cfg, backend, self._progress, self._prompt)
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
                    if not self.demo:
                        self._save_measurement()
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
            if not self._local():
                return self._send(403, {"error": "forbidden"})
            path = self.path.split("?")[0]
            if path == "/api/state":
                return self._send(200, app.state())
            if path == "/api/design":
                return self._send(200, design.to_json(app.design) if app.design else None)
            file = WEB / ("index.html" if path == "/" else path.lstrip("/"))
            if file.suffix in TYPES and file.is_file() and file.parent == WEB:
                return self._send(200, file.read_bytes(), TYPES[file.suffix])
            self._send(404, {"error": "not found"})

        def do_POST(self):
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
