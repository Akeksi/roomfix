"""Apply a design: write the config for CamillaDSP or Equalizer APO and run it."""
import io
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
import yaml

from . import config, dsp

CAMILLA_URL = "https://github.com/HEnquist/camilladsp/releases/download/v4.1.3/camilladsp-windows-amd64.zip"
APO_CHANNELS = ["L", "R", "C", "SUB", "RL", "RR", "SL", "SR"]
WINDOWS = sys.platform == "win32"
TARGET_LEVEL = 1024  # playback buffer level (samples) every engine is held at


# ---- config text ---------------------------------------------------------
def _devices(cfg, fs, device, channels, n_in, synced):
    """The devices section. synced: several engines must keep a fixed delay between them."""
    dev = {"samplerate": fs, "chunksize": 1024, "enable_rate_adjust": True,
           "resampler": {"type": "AsyncSinc", "profile": "Balanced"},
           "capture": {"type": "Wasapi", "channels": n_in, "device": cfg["capture_device"] or None,
                       "loopback": bool(cfg["capture_loopback"])},
           "playback": {"type": "Wasapi", "channels": channels, "device": device or None, "exclusive": True}}
    if synced:
        # the latency of an engine is chunk + buffer level. Holding every engine at the same
        # level, and getting there quickly after a start, keeps the devices aligned.
        dev.update(target_level=TARGET_LEVEL, adjust_period=2)
    return dev


def _processing(design, ways, sub, n_in, min_out=0):
    """(number of output channels, mixers, filters, pipeline) of one engine: the correction
    of every speaker, then the drivers this engine plays. ways: all ways of every speaker;
    sub: the subwoofer (the design's, or one adjusted for another source), or None."""
    speakers = design["speakers"]
    filters, pipeline, outs = {}, [], []

    def add(name, flt):
        if flt["type"] in ("Gain", "Delay"):
            filters[name] = {"type": flt["type"], "parameters": {k: v for k, v in flt.items() if k != "type"}}
        else:
            filters[name] = {"type": "Biquad", "parameters": dict(flt)}
        return name

    # 1. one channel per speaker, 2. speaker EQ, 3. split to drivers, 4. crossover + delay
    pipeline.append({"type": "Mixer", "name": "speakers"})
    for si, spk in enumerate(speakers):
        names = [add(f"s{si}_eq{i}", flt) for i, flt in enumerate(spk["eq"])]
        names.append(add(f"s{si}_gain", {"type": "Gain", "gain": spk["gain"]}))
        pipeline.append({"type": "Filter", "description": f"{spk['name']}: room correction",
                         "channels": [si], "names": names})
        if ways:
            for wi, way in enumerate(spk["ways"]):
                outs.append((f"s{si}w{wi}", f"{spk['name']} {way['name']}", way,
                             [{"channel": si, "gain": way["gain"], "inverted": way["invert"]}]))
    if sub:  # the bass of all speakers
        outs.append(("sub", sub["name"], sub, [{"channel": si, "gain": g, "inverted": sub["invert"]}
                                               for si, g in enumerate(sub["gains"])]))
    pipeline.append({"type": "Mixer", "name": "drivers"})
    for key, description, way, _ in outs:
        names = [add(f"{key}_xo{i}", flt) for i, flt in enumerate(way["filters"])]
        if way["delay"]:
            names.append(add(f"{key}_delay", {"type": "Delay", "delay": way["delay"], "unit": "samples"}))
        if names:
            pipeline.append({"type": "Filter", "description": description,
                             "channels": [way["channel"]], "names": names})
    n_out = max(max(way["channel"] for _, _, way, _ in outs) + 1, min_out or 0)
    mapping = [{"dest": way["channel"], "sources": sources} for _, _, way, sources in outs]
    mixers = {
        "speakers": {"channels": {"in": n_in, "out": len(speakers)}, "mapping": [
            {"dest": si, "sources": [{"channel": s["input"], "gain": 0}]} for si, s in enumerate(speakers)]},
        "drivers": {"channels": {"in": len(speakers), "out": n_out},
                    "mapping": sorted(mapping, key=lambda m: m["dest"])},
    }
    return n_out, mixers, filters, pipeline


def camilladsp_configs(cfg, design, channels=None):
    """CamillaDSP (v4) configurations as [(output device, dict)], one engine per device.

    channels: {device name: channel count of that device}, where known.
    """
    channels = channels or {}
    speakers, sub = design["speakers"], design.get("sub")
    n_in = max(s["input"] for s in speakers) + 1
    devices = [cfg["output_device"]] + ([sub["device"]] if sub and sub["device"] else [])
    result = []
    for device in devices:
        has_sub = sub and (sub["device"] or cfg["output_device"]) == device
        n_out, mixers, filters, pipeline = _processing(design, device == cfg["output_device"],
                                                       sub if has_sub else None, n_in, channels.get(device))
        result.append((device, {
            "title": "roomfix",
            "devices": _devices(cfg, design["fs"], device, n_out, n_in, len(devices) > 1),
            "mixers": mixers,
            "filters": filters,
            "pipeline": pipeline,
        }))
    return result


TV_APP_CHUNK = 256  # small: the TV's own speakers do not wait for the subwoofer
TV_APP_LEVEL = 512  # playback buffer. Shared-mode capture arrives in 10 ms packets, which it must bridge.


def tv_app_configs(cfg, design, channels=None):
    """The engines of the "TV App" source as [(output device, dict)]; [] if it is not set up.

    The TV sends what its own apps play to an S/PDIF input of the PC.

    speakers "roomfix": the engines of the PC source, the whole correction included, take
    that input instead of the system audio. The same speakers and subwoofer play, lined up
    as measured.

    speakers "tv": the speakers are the TV's own, which play the apps by themselves. Only the
    subwoofer plays, with the measured correction (speaker EQ in the bass, level, crossover,
    polarity) from left and right of that input. Not the measured delay: it lines the
    subwoofer up with the PC's path to the TV. The TV's speakers do not wait for this engine,
    so the subwoofer is as early as it can be, plus the delay set by ear.
    """
    t = config.tv_app(cfg)
    if not t:
        return []
    if t.get("speakers", "roomfix") == "roomfix":
        result = []
        for device, conf in camilladsp_configs(cfg, design, channels):
            n_in = max(2, conf["devices"]["capture"]["channels"])  # S/PDIF is stereo
            conf["mixers"]["speakers"]["channels"]["in"] = n_in
            conf["devices"]["capture"] = {"type": "Wasapi", "channels": n_in, "device": t["device"]}
            result.append((device, dict(conf, title="roomfix TV App")))
        return result
    tv = _tv_sub_config(cfg, design, t, channels)
    return [tv] if tv else []


def _tv_sub_config(cfg, design, t, channels):
    """The subwoofer's engine for the TV's own speakers, or None without a subwoofer."""
    # as measured: the changes by hand are for the PC's path, this source has its own by ear
    design = dict(design, **(design.get("auto") or {}))
    sub = design.get("sub")
    if not sub:
        return None
    channels, fs = channels or {}, design["fs"]
    device = sub["device"] or cfg["output_device"]
    tuned = dict(sub, delay=int(round(float(t["delay_ms"]) * fs / 1000)),
                 invert=bool(sub["invert"]) != bool(t["invert"]),
                 gains=[round(g + float(t["gain_db"]), 2) for g in sub["gains"]])
    n_in = max(2, max(s["input"] for s in design["speakers"]) + 1)  # S/PDIF is stereo
    n_out, mixers, filters, pipeline = _processing(design, False, tuned, n_in, channels.get(device))
    devices = {
        "samplerate": fs, "chunksize": TV_APP_CHUNK, "target_level": TV_APP_LEVEL,
        # the TV's clock runs the input, the DAC's the output: follow the difference
        "enable_rate_adjust": True, "resampler": {"type": "AsyncPoly", "interpolation": "Cubic"},
        "capture": {"type": "Wasapi", "channels": n_in, "device": t["device"]},
        "playback": {"type": "Wasapi", "channels": n_out, "device": device or None, "exclusive": True},
    }
    return device, {"title": "roomfix TV App", "devices": devices, "mixers": mixers, "filters": filters,
                    "pipeline": pipeline}


def camilladsp_config(cfg, design, device_channels=0):
    """The configuration of the DAC's engine (the only one unless a second device is used)."""
    return camilladsp_configs(cfg, design, {cfg["output_device"]: device_channels})[0][1]


def sync_configs(cfg, ref, sub, ref_db, sub_db, channels=None):
    """Engine configurations for measuring the delay between two output devices.

    System audio channel 1 goes to the reference driver, channel 2 to the subwoofer,
    each attenuated to its test level and the reference behind its protection filter.
    """
    channels = channels or {}
    result = []
    for device, drv, source, gain in ((cfg["output_device"], ref, 0, ref_db), (sub["device"], sub, 1, sub_db)):
        n_out = max(drv["channel"] + 1, channels.get(device) or 0)
        filters = {f"protect{i}": {"type": "Biquad", "parameters": dict(flt)} for i, flt in enumerate(drv["protect"])}
        pipeline = [{"type": "Mixer", "name": "sync"}]
        if filters:
            pipeline.append({"type": "Filter", "channels": [drv["channel"]], "names": list(filters)})
        result.append((device, {
            "title": "roomfix delay measurement",
            "devices": _devices(cfg, cfg["sample_rate"], device, n_out, 2, True),
            "mixers": {"sync": {"channels": {"in": 2, "out": n_out}, "mapping": [
                {"dest": drv["channel"], "sources": [{"channel": source, "gain": gain}]}]}},
            "filters": filters,
            "pipeline": pipeline,
        }))
    return result


def simulate(conf, x, fs):
    """Run x [samples, inputs] through a configuration the way CamillaDSP does (simulator, tests)."""
    y = np.asarray(x, float)
    for step in conf["pipeline"]:
        if step["type"] == "Mixer":
            mixer = conf["mixers"][step["name"]]
            z = np.zeros((len(y), mixer["channels"]["out"]))
            for m in mixer["mapping"]:
                for src in m["sources"]:
                    z[:, m["dest"]] += y[:, src["channel"]] * 10 ** (src.get("gain", 0) / 20) * (
                        -1 if src.get("inverted") else 1)
            y = z
            continue
        for ch in step["channels"]:
            for name in step["names"]:
                kind, par = conf["filters"][name]["type"], conf["filters"][name]["parameters"]
                if kind == "Gain":
                    y[:, ch] *= 10 ** (par["gain"] / 20)
                elif kind == "Delay":
                    y[:, ch] = np.concatenate([np.zeros(par["delay"]), y[:len(y) - par["delay"], ch]])
                else:
                    y[:, ch] = dsp.apply([par], fs, y[:, ch])
    return y


def eqapo_config(design):
    """Equalizer APO configuration text (single-way speakers only)."""
    lines = ["# Generated by roomfix. Changes will be overwritten."]
    for spk in design["speakers"]:
        if len(spk["ways"]) != 1:
            raise RuntimeError("Equalizer APO cannot be used for active crossovers.")
        way = spk["ways"][0]
        ch = lambda i: APO_CHANNELS[i] if i < len(APO_CHANNELS) else str(i + 1)
        if way["channel"] != spk["input"]:
            lines.append(f"Copy: {ch(way['channel'])}={ch(spk['input'])}")
        lines.append(f"Channel: {ch(way['channel'])}")
        lines.append(f"Preamp: {spk['gain'] + way['gain']:.1f} dB")
        if way["delay"]:
            lines.append(f"Delay: {way['delay']} samples")
        for flt in spk["eq"]:
            lines.append(f"Filter: ON PK Fc {flt['freq']:.1f} Hz Gain {flt['gain']:.1f} dB Q {flt['q']:.2f}")
    return "\n".join(lines + ["Channel: all", ""])


# ---- Equalizer APO -------------------------------------------------------
def eqapo_dir():
    if WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EqualizerAPO") as key:
                return Path(winreg.QueryValueEx(key, "ConfigPath")[0])
        except OSError:
            pass
    path = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "EqualizerAPO" / "config"
    return path if path.exists() else None


def _eqapo_write(text):
    folder = eqapo_dir()
    if folder is None:
        raise RuntimeError("Equalizer APO is not installed. Install it and enable it for the DAC, "
                           "or switch the engine to CamillaDSP.")
    (folder / "roomfix.txt").write_text(text, encoding="utf-8")
    main = folder / "config.txt"
    current = main.read_text(encoding="utf-8", errors="replace") if main.exists() else ""
    if "roomfix.txt" not in current:
        main.write_text(current.rstrip("\n") + "\nInclude: roomfix.txt\n", encoding="utf-8")


# ---- CamillaDSP ----------------------------------------------------------
def _exe():
    return config.data_dir() / "camilladsp.exe"


def _yaml():
    return config.data_dir() / "camilladsp.yml"


def _startup_file():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/roomfix.vbs"


def _download():
    if not _exe().exists():
        with urllib.request.urlopen(CAMILLA_URL, timeout=60) as resp:
            archive = zipfile.ZipFile(io.BytesIO(resp.read()))
        _exe().write_bytes(archive.read("camilladsp.exe"))


def _own_processes(access=0x1000, program=None):
    """Open handles of the CamillaDSP processes started from roomfix's own copy (or, with
    program, of the processes of that program, this one excepted).

    Other CamillaDSP installations on the computer are never touched. The caller
    closes the handles. access: PROCESS_QUERY_LIMITED_INFORMATION by default.
    """
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    pids = (wintypes.DWORD * 16384)()
    used = wintypes.DWORD()
    if not k32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(used)):
        return k32, []
    own = os.path.normcase(os.path.realpath(program or _exe()))
    handles = []
    for pid in pids[:used.value // ctypes.sizeof(wintypes.DWORD)]:
        if pid == os.getpid():
            continue
        handle = k32.OpenProcess(access | 0x1000, False, pid)
        if not handle:
            continue
        buf, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if (k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
                and os.path.normcase(os.path.realpath(buf.value)) == own):
            handles.append(handle)
        else:
            k32.CloseHandle(handle)
    return k32, handles


def camilla_running():
    if not WINDOWS:
        return False
    k32, handles = _own_processes()
    for handle in handles:
        k32.CloseHandle(handle)
    return bool(handles)


def camilla_stop():
    """Stop the supervisor and every engine started from roomfix's own copy of CamillaDSP."""
    if not WINDOWS:
        return
    from . import supervisor
    supervisor.stop()  # first: it would start the engines again
    k32, handles = _own_processes(0x0001 | 0x00100000)  # PROCESS_TERMINATE | SYNCHRONIZE
    for handle in handles:
        k32.TerminateProcess(handle, 1)
        k32.WaitForSingleObject(handle, 5000)
        k32.CloseHandle(handle)
    if handles:
        time.sleep(0.5)  # let Windows release the audio device


def quit_all():
    """Close every roomfix: the background process and its engines and, for roomfix.exe, the
    pages that run from it too. The installer does this before it replaces the program.
    Returns whether the correction was running (to start it again afterwards)."""
    from . import supervisor
    if not (WINDOWS and getattr(sys, "frozen", False)):
        running = supervisor.request("/api/status") is not None
        camilla_stop()
        return running
    # roomfix.exe: only what runs from this very program. A roomfix that runs from elsewhere
    # (another copy, or from Python) is not this one's to stop, nor its engines.
    me = os.path.normcase(os.path.realpath(sys.executable))
    ours = _program_of(supervisor._info().get("pid")) == me
    if ours:
        camilla_stop()
    k32, handles = _own_processes(0x0001 | 0x00100000, program=sys.executable)
    for handle in handles:  # the pages
        k32.TerminateProcess(handle, 0)
        k32.WaitForSingleObject(handle, 5000)
        k32.CloseHandle(handle)
    return ours


def _program_of(pid):
    """The program file a process runs (normalised), or None."""
    import ctypes
    from ctypes import wintypes
    if not pid:
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        buf, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.normcase(os.path.realpath(buf.value))
        return None
    finally:
        k32.CloseHandle(handle)


def _startup_text():
    from . import supervisor
    line = " ".join(f'""{arg}""' for arg in supervisor.command())
    return f'CreateObject("WScript.Shell").Run "{line}", 0, False\n'


def startup_here():
    """The correction starts with Windows (a Startup entry exists): from this roomfix. For
    roomfix.exe installed where roomfix ran from Python before, or moved."""
    path = _startup_file()
    if WINDOWS and path.exists() and path.read_text(encoding="utf-8", errors="replace") != _startup_text():
        path.write_text(_startup_text(), encoding="utf-8")


def _yamls():
    """Configuration files of the correction, one per output device."""
    return sorted(config.data_dir().glob("camilladsp*.yml"))


def _tv_app_yaml(i=0):
    """Configuration file of the "TV App" source's engine for the i-th output device."""
    return config.data_dir() / ("tvapp.yml" if i == 0 else f"tvapp_{i + 1}.yml")


def _tv_app_files():
    """The TV App source's configuration files; they exist if that source is set up."""
    return sorted(config.data_dir().glob("tvapp*.yml"))


def _check(files):
    for path in files:
        check = subprocess.run([str(_exe()), "--check", str(path)], capture_output=True, text=True,
                               creationflags=0x08000000)
        if check.returncode != 0:
            raise RuntimeError("CamillaDSP rejected the configuration: " + (check.stdout + check.stderr).strip()[-400:])


def correction_start():
    """Start the applied correction: a supervisor in the background, which runs the engines,
    restarts them when a device returns, and holds the volume. Returns a message."""
    from . import supervisor
    _check(_yamls() + _tv_app_files())
    supervisor.switch_on()
    return _started(supervisor.start())


def _started(status):
    """A message about the correction, from the status of the supervisor that runs it."""
    from . import supervisor
    if status is None:
        raise RuntimeError("The playback engine could not be started. See engine.log in " + str(config.data_dir()))
    tv = status.get("source") == "tv_app"
    # (from the TV, an engine that waits for sound is not a failure: the TV may be off)
    waiting = [e["detail"] or e["device"] for e in status["engines"]
               if e["status"] != "running" and not (tv and e["status"] == "starting")]
    if len(waiting) == len(status["engines"]):
        why = " ".join(waiting)
        hint = "" if "not connected" in why else (" Check that the output devices and the system audio source "
                                                  "are all set to the same sample rate in Windows sound settings.")
        raise RuntimeError(f"CamillaDSP could not start: {why}.{hint} roomfix keeps trying.")
    active = "Correction is active (CamillaDSP" + (", source TV App)" if tv else ")")
    if waiting:
        return active + ", but waiting for a device: " + "; ".join(waiting) + "."
    if any(e.get("signal") is False for e in status["engines"]):
        return active + ", but there is " + supervisor.NO_SIGNAL
    return active + "."


def camilla_start(files, ports):
    """Start one engine per configuration file, without supervision (for measuring).
    ports: websocket ports, to query the engines."""
    _check(files)
    procs = []
    for i, path in enumerate(files):
        log = path.with_suffix(".log")
        port = ["-p", str(ports[i])] if ports else []
        procs.append((log, subprocess.Popen([str(_exe()), *port, "-o", str(log), str(path)],
                                            creationflags=0x08000000 | 0x00000008, close_fds=True)))  # no window, detached
    time.sleep(2.0)
    for log, proc in procs:
        if proc.poll() is not None:
            camilla_stop()
            tail = log.read_text(errors="replace")[-600:] if log.exists() else ""
            raise RuntimeError("CamillaDSP could not start. Check that the output devices and the system audio "
                               "source are all set to the same sample rate in Windows sound settings.\n" + tail)


def _ws(port, command):
    """One request to CamillaDSP's websocket API. Returns the value, or None on any failure."""
    return (_request(port, command) or {}).get("value")


def _request(port, command):
    """One request to CamillaDSP's websocket API: "GetState", {"SetVolume": -20}...
    Returns the reply ({"result": "Ok", "value": ...}), or None on any failure."""
    import base64
    import json
    import socket
    import struct
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
            stream = sock.makefile("rb")
            while stream.readline().strip():  # skip the handshake reply
                pass
            payload, mask = json.dumps(command).encode(), os.urandom(4)
            sock.sendall(bytes([0x81, 0x80 | len(payload)]) + mask
                         + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))
            size = stream.read(2)[1] & 0x7F
            if size == 126:
                size = struct.unpack(">H", stream.read(2))[0]
            reply = next(iter(json.loads(stream.read(size)).values()))
            sock.sendall(bytes([0x88, 0x80]) + mask)  # say goodbye, or every request leaves a
            stream.read(2)                            # "lost connection" warning in the engine's log
            return reply if isinstance(reply, dict) else None
    except (OSError, ValueError, IndexError, AttributeError, StopIteration):
        return None


class session:
    """Temporary engines, to measure through the system audio path. Stops them on exit."""

    def __init__(self, configs):
        self.configs = configs

    def __enter__(self):
        import socket
        self.ports, files = {}, []
        for i, (device, conf) in enumerate(self.configs):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                self.ports[device] = sock.getsockname()[1]
            files.append(config.data_dir() / f"measure_{i + 1}.yml")
            files[-1].write_text(yaml.safe_dump(conf, sort_keys=False), encoding="utf-8")
        _download()
        camilla_stop()
        camilla_start(files, list(self.ports.values()))
        return self

    def __exit__(self, *exc):
        camilla_stop()
        return False

    def buffers(self):
        """Playback buffer level of each engine: {device: samples, or None}."""
        return {device: _ws(port, "GetBufferLevel") for device, port in self.ports.items()}

    def settle(self, timeout=90.0):
        """Wait until every engine holds its buffer at the target level (stable latency)."""
        good, end = 0, time.monotonic() + timeout
        while good < 4 and time.monotonic() < end:
            time.sleep(1.0)
            levels = list(self.buffers().values())
            ok = all(v is not None and abs(v - TARGET_LEVEL) < 0.06 * TARGET_LEVEL for v in levels)  # 1.3 ms
            good = good + 1 if ok else 0
        return good >= 4


# ---- public --------------------------------------------------------------
def engine_files(cfg, design, channels=None):
    """{path: text} of the applied correction's engine files for this design."""
    if cfg["backend"] == "equalizerapo":
        return {config.data_dir() / "equalizerapo.txt": eqapo_config(design)}
    files = {}
    for i, (_, conf) in enumerate(camilladsp_configs(cfg, design, channels)):
        path = _yaml() if i == 0 else _yaml().with_name(f"camilladsp_{i + 1}.yml")
        files[path] = yaml.safe_dump(conf, sort_keys=False)
    for i, (_, conf) in enumerate(tv_app_configs(cfg, design, channels)):
        files[_tv_app_yaml(i)] = yaml.safe_dump(conf, sort_keys=False)
    return files


def _put(files):
    """Put new engine files in place of the applied ones. CamillaDSP checks them first: if it
    refuses one, the applied correction stays as it is."""
    staged = {path: path.with_name(path.name + ".new") for path in files}
    try:
        for path, text in files.items():
            staged[path].write_text(text, encoding="utf-8")
        if WINDOWS and any(path.suffix == ".yml" for path in files):
            _download()
            _check([tmp for path, tmp in staged.items() if path.suffix == ".yml"])
    except Exception:
        for tmp in staged.values():
            tmp.unlink(missing_ok=True)
        raise
    if any(path.suffix == ".yml" for path in files):
        for old in _yamls() + _tv_app_files():
            if old not in files:
                old.unlink()
    for path, tmp in staged.items():
        config.replace_file(tmp, path)


def apply(cfg, design, channels=None, written=None):
    """Write the correction and switch it on. written(paths) is called once the new engine
    files are in place (to save the correction). Returns a message for the user.

    A running supervisor reloads its engines, which takes effect without a pause while the
    output devices stay the same; otherwise it is started."""
    if cfg["backend"] == "camilladsp" and WINDOWS and not cfg["capture_device"]:
        raise RuntimeError("Select the system audio source in Setup first.")
    files = engine_files(cfg, design, channels)
    _put(files)
    if written:
        written(list(files))
    return switch_to_files(cfg)


def switch_to_files(cfg):
    """Make the engine files in place the ones that play. Returns a message for the user."""
    if cfg["backend"] == "equalizerapo":
        path = config.data_dir() / "equalizerapo.txt"
        if not WINDOWS:
            return f"Demo: Equalizer APO config written to {path}"
        _eqapo_write(path.read_text(encoding="utf-8"))
        return "Correction is active (Equalizer APO)."
    if not WINDOWS:
        return f"Demo: CamillaDSP config written to {_yaml()}"
    from . import supervisor
    if cfg["autostart"]:
        _startup_file().write_text(_startup_text(), encoding="utf-8")
    elif _startup_file().exists():
        _startup_file().unlink()
    status = supervisor.reload()
    if status is not None:
        return _started(status)
    camilla_stop()  # (also engines left over from a measurement that was cut short)
    return correction_start()


def bypass(cfg):
    """Switch the correction off."""
    if cfg["backend"] == "equalizerapo":
        if WINDOWS:
            _eqapo_write("# roomfix: correction off\n")
        return "Correction is off."
    camilla_stop()
    if WINDOWS and _startup_file().exists():
        _startup_file().unlink()
    return "Correction is off. With CamillaDSP stopped, no sound reaches the DAC."


class paused:
    """Context manager: free the DAC for measuring, restore the engine afterwards."""

    def __init__(self, cfg):
        self.cfg, self.resume = cfg, None

    def __enter__(self):
        if not WINDOWS:
            return self
        if self.cfg["backend"] == "camilladsp":
            if camilla_running():
                camilla_stop()
                self.resume = correction_start
        else:
            path = (eqapo_dir() or Path()) / "roomfix.txt"
            if path.exists():
                text = path.read_text(encoding="utf-8")
                _eqapo_write("# roomfix: measuring\n")
                time.sleep(1.0)
                self.resume = lambda: _eqapo_write(text)
        return self

    def __exit__(self, *exc):
        if self.resume:
            try:
                self.resume()
            except Exception:
                pass
        return False


def playback():
    """Volume and state of the running engines, from their supervisor: {"volume", "mute",
    "engines": [{"device", "status", "detail"}]}, or None if it is not running."""
    if not WINDOWS:
        return None
    from . import supervisor
    return supervisor.request("/api/status")


def set_playback(path, body):
    """Change the volume ("/api/volume") or mute ("/api/mute") of the running engines."""
    from . import supervisor
    if supervisor.request(path, body) is None:
        raise RuntimeError("The correction is not running. Press Apply correction first.")


def status(cfg):
    if not WINDOWS:
        return "demo"
    if cfg["backend"] == "camilladsp":
        return "on" if camilla_running() else "off"
    path = (eqapo_dir() or Path()) / "roomfix.txt"
    return "on" if path.exists() and "Channel:" in path.read_text(encoding="utf-8", errors="replace") else "off"
