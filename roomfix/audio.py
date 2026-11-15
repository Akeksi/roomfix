"""Real audio I/O through sounddevice (WASAPI on Windows)."""
import sys
import threading
import time

import numpy as np


def _sd():
    import sounddevice as sd
    return sd


def list_devices(fresh=False):
    """Input and output devices: [{"name", "channels"}], WASAPI only on Windows.

    fresh: ask Windows again. Otherwise devices connected since the first call are not seen.
    """
    try:
        sd = _sd()
        if fresh:
            sd._terminate()
            sd._initialize()
        apis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception:  # no audio system (e.g. a server)
        return {"inputs": [], "outputs": []}
    result = {"inputs": [], "outputs": []}
    for dev in devices:
        api = apis[dev["hostapi"]]["name"]
        if sys.platform == "win32" and "WASAPI" not in api:
            continue
        for kind, key in (("inputs", "max_input_channels"), ("outputs", "max_output_channels")):
            if dev[key] > 0 and dev["name"] not in [d["name"] for d in result[kind]]:
                result[kind].append({"name": dev["name"], "channels": dev[key], "index": dev["index"]})
    return result


class Audio:
    simulated = False

    def __init__(self, cfg):
        self.fs = cfg["sample_rate"]
        devs = list_devices()
        self.cfg, self.outputs = cfg, devs["outputs"]
        self.out = self._find(devs["outputs"], cfg["output_device"], "DAC")
        self.inp = self._find(devs["inputs"], cfg["input_device"], "microphone")
        self.in_ch = cfg["input_channel"]
        if self.in_ch >= self.inp["channels"]:
            raise RuntimeError("The selected microphone input channel does not exist.")
        self._last_play = -1e9

    @staticmethod
    def _find(devs, name, what):
        for dev in devs:
            if dev["name"] == name:
                return dev
        raise RuntimeError(f"The {what} '{name}' was not found. Is it connected and selected in Setup?")

    def device_channels(self):
        return {d["name"]: d["channels"] for d in self.outputs}

    def system_output(self):
        """The output device that feeds the system audio source: where Windows plays music."""
        name = self.cfg["capture_device"]
        if not self.cfg["capture_loopback"]:
            twin = name.replace("Output", "Input", 1)  # VB-CABLE: "CABLE Output" records "CABLE Input"
            if twin == name or twin not in [d["name"] for d in self.outputs]:
                sd = _sd()
                api = next(a for a in sd.query_hostapis() if "WASAPI" in a["name"])
                twin = sd.query_devices(api["default_output_device"])["name"]
            name = twin
        used = {self.cfg["output_device"], (self.cfg.get("sub") or {}).get("device")}
        if name in used:
            raise RuntimeError("The system audio source must be a virtual cable, not one of the output devices.")
        return name

    def system_playrec(self, configs, outs, extra=1.0, settle=False):
        """Play each of `outs` as system audio through engines running `configs`.

        Returns the recordings and each engine's playback buffer level during the first one.
        """
        from . import engine
        name = self.system_output()
        with engine.session(configs) as engines:
            if settle:
                engines.settle()
            recs, levels = [], None
            for out in outs:
                before = engines.buffers()
                recs.append(self.playrec(out, extra, name))
                after = engines.buffers()
                if levels is None:
                    levels = {dev: None if None in (before[dev], after[dev]) else (before[dev] + after[dev]) / 2
                              for dev in before}
        return recs, levels

    def playrec(self, out, extra=1.0, device=None):
        """Play `out` [samples, channels] on the DAC (or another output device), return the
        mono mic recording."""
        dev = self._find(self.outputs, device, "output device") if device else self.out
        com = None
        if sys.platform == "win32":
            # WASAPI streams only start on a thread that has COM initialised; the
            # measurement runs on a worker thread, where nobody has done that yet.
            import ctypes
            com = ctypes.windll.ole32
            if com.CoInitializeEx(None, 0) < 0:  # e.g. thread already uses another COM mode
                com = None
        try:
            # A DAC or HDMI link that was idle, or was just released by the playback
            # engine, can swallow the first half second. Wake it with silence first.
            if time.monotonic() - self._last_play > 5:
                self._playrec(np.zeros((int(0.6 * self.fs), out.shape[1])), 0.0, dev)
            rec = self._playrec(out, extra, dev)
            self._last_play = time.monotonic()
            return rec
        finally:
            if com:
                com.CoUninitialize()

    def _playrec(self, signal, extra, dev):
        sd = _sd()
        fs, n_out = self.fs, dev["channels"]
        if signal.shape[1] > n_out:
            raise RuntimeError(f"{dev['name']} only has {n_out} channels.")
        play = np.zeros((len(signal), n_out), np.float32)
        play[:, :signal.shape[1]] = signal
        rec = np.zeros(len(signal) + int(extra * fs), np.float32)
        pos = {"in": 0, "out": 0, "xrun": False}
        done = threading.Event()

        def on_input(indata, frames, _time, status):
            if status.input_overflow:
                pos["xrun"] = True
            n = min(frames, len(rec) - pos["in"])
            rec[pos["in"]:pos["in"] + n] = indata[:n, self.in_ch]
            pos["in"] += n
            if pos["in"] >= len(rec):
                done.set()

        def on_output(outdata, frames, _time, status):
            if status.output_underflow and 0 < pos["out"] < len(play):
                pos["xrun"] = True
            n = min(frames, len(play) - pos["out"])
            outdata[:n] = play[pos["out"]:pos["out"] + n]
            outdata[n:] = 0
            pos["out"] += n

        extra_in = extra_out = None
        if sys.platform == "win32":
            # Shared mode: Windows converts to whatever sample rate and bit depth the device
            # is set to. (Exclusive mode is not dependable: on some USB DACs and on virtual
            # cables it plays with gaps, heard as a distorted, robotic sweep.)
            # Raw mode bypasses the Windows audio effects. Without it, microphone noise
            # suppression gates the test signals and output enhancements colour them.
            extra_in, extra_out = sd.WasapiSettings(), sd.WasapiSettings()
            extra_in._streaminfo.streamOption = extra_out._streaminfo.streamOption = 1  # eStreamOptionRaw
        with sd.InputStream(device=self.inp["index"], channels=self.in_ch + 1, samplerate=fs,
                            dtype="float32", callback=on_input, extra_settings=extra_in), \
             sd.OutputStream(device=dev["index"], channels=n_out, samplerate=fs,
                             dtype="float32", callback=on_output, extra_settings=extra_out):
            if not done.wait(len(rec) / fs + 10):
                raise RuntimeError("Audio device stopped responding.")
        if pos["xrun"]:
            raise RuntimeError("Audio glitch (buffer over/underrun) during measurement. "
                               "Close other audio programs and try again.")
        return rec.astype(float)
