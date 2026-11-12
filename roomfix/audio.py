"""Real audio I/O through sounddevice (WASAPI on Windows)."""
import sys
import threading

import numpy as np


def _sd():
    import sounddevice as sd
    return sd


def list_devices():
    """Input and output devices: [{"name", "channels"}], WASAPI only on Windows."""
    try:
        sd = _sd()
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
        self.out = self._find(devs["outputs"], cfg["output_device"], "DAC")
        self.inp = self._find(devs["inputs"], cfg["input_device"], "microphone")
        self.in_ch = cfg["input_channel"]
        if self.in_ch >= self.inp["channels"]:
            raise RuntimeError("The selected microphone input channel does not exist.")
        self.exclusive = sys.platform == "win32"

    @staticmethod
    def _find(devs, name, what):
        for dev in devs:
            if dev["name"] == name:
                return dev
        raise RuntimeError(f"The {what} '{name}' was not found. Is it connected and selected in Setup?")

    def playrec(self, out, extra=1.0):
        """Play `out` [samples, channels] on the DAC, return the mono mic recording."""
        try:
            return self._playrec(out, extra, self.exclusive)
        except _sd().PortAudioError:
            if not self.exclusive:
                raise
            self.exclusive = False  # device refused exclusive mode: use shared mode
            return self._playrec(out, extra, False)

    def _playrec(self, signal, extra, exclusive):
        sd = _sd()
        fs, n_out = self.fs, self.out["channels"]
        if signal.shape[1] > n_out:
            raise RuntimeError(f"The DAC only has {n_out} channels.")
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

        extra_out = sd.WasapiSettings(exclusive=True) if exclusive else None
        with sd.InputStream(device=self.inp["index"], channels=self.in_ch + 1, samplerate=fs,
                            dtype="float32", callback=on_input), \
             sd.OutputStream(device=self.out["index"], channels=n_out, samplerate=fs,
                             dtype="float32", callback=on_output, extra_settings=extra_out):
            if not done.wait(len(rec) / fs + 10):
                raise RuntimeError("Audio device stopped responding.")
        if pos["xrun"]:
            raise RuntimeError("Audio glitch (buffer over/underrun) during measurement. "
                               "Close other audio programs and try again.")
        return rec.astype(float)
