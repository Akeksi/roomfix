"""Saved corrections: every applied correction is kept, so that it can be used again.

Each one is a folder in corrections/ with exactly the engine files that played it (one
CamillaDSP configuration per output device, the TV App engine, or the Equalizer APO text)
and meta.json: its name, when it was applied, and the settings that shaped it (target
curve, crossovers, changes by hand) plus the measurement it was made from, so that the
roomfix page can show and change it again.

A correction with a name is a preset and is kept until it is deleted. Unnamed ones form
the history: the newest HISTORY are kept. Using a correction copies its engine files into
place; the supervisor then reloads the engines, which takes effect without a pause when
the output devices stay the same.

Both the page (Apply, rename, delete) and the supervisor (switching from the phone or the
tray icon) use these functions. Every write replaces one small file at once.
"""
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from . import config

HISTORY = 20
ENGINE_FILES = {"camilladsp": ("camilladsp*.yml", "tvapp*.yml"), "equalizerapo": ("equalizerapo.txt",)}
SETTINGS = ("target", "crossover", "adjust")  # the parts of the settings a correction keeps
NAME_MAX = 60


def _root():
    return config.data_dir() / "corrections"


def _measurements():
    return _root() / "measurements"


def _write(path, text):
    """Replace a file in one step: a reader sees the old or the new one, never half."""
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(text, encoding="utf-8")
    config.replace_file(tmp, path)


def live_files(folder=None, backend=None):
    """The engine files in a folder (the applied correction's by default), of one playback
    engine or, without backend, of the one they are for."""
    folder = Path(folder or config.data_dir())
    found = {name: sorted(p for pattern in patterns for p in folder.glob(pattern) if p.is_file())
             for name, patterns in ENGINE_FILES.items()}
    if backend:
        return found[backend]
    return found["camilladsp"] or found["equalizerapo"]


def backend_of(files):
    return "equalizerapo" if files and all(Path(p).suffix == ".txt" for p in files) else "camilladsp"


def settings_of(cfg):
    """What a correction keeps of the settings. Not the devices or channels: those are the
    setup, which the measurement belongs to."""
    kept = {key: cfg.get(key) for key in SETTINGS}
    kept["sub_crossover"] = (cfg.get("sub") or {}).get("crossover")
    tv = cfg.get("tv_app") or {}
    kept["tv_app"] = {k: tv.get(k) for k in ("speakers", "delay_ms", "invert", "gain_db")}
    return json.loads(json.dumps(kept))


def restore_settings(cfg, kept):
    """Put a correction's settings back into cfg (in place)."""
    for key in SETTINGS:
        if kept.get(key) is not None:
            cfg[key] = kept[key]
    if kept.get("sub_crossover") is not None:
        cfg["sub"]["crossover"] = kept["sub_crossover"]
    if kept.get("tv_app"):
        cfg["tv_app"].update({k: v for k, v in kept["tv_app"].items() if v is not None})


_ID = re.compile(r"\d{8}-\d{6}(?:-\d+)?")


def _ids():
    root = _root()
    return sorted((p.name for p in root.iterdir() if _ID.fullmatch(p.name) and (p / "meta.json").exists()),
                  reverse=True) if root.exists() else []


def _meta(cid):
    try:
        return json.loads((_root() / cid / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _active_file():
    return _root() / "active.json"


def active():
    """The id of the correction in use, or None."""
    try:
        cid = json.loads(_active_file().read_text(encoding="utf-8")).get("id")
    except (OSError, ValueError, AttributeError):
        return None
    return cid if cid and (_root() / cid / "meta.json").exists() else None


def _set_active(cid):
    _write(_active_file(), json.dumps({"id": cid, "at": time.time()}))


def entries():
    """All saved corrections, newest first: [{"id", "name", "created", "summary", "active",
    "editable" (has its settings and measurement), "layout"}]."""
    ensure()
    now = active()
    result = []
    for cid in _ids():
        meta = _meta(cid)
        if meta is None:
            continue
        result.append({"id": cid, "name": meta.get("name") or "", "created": meta.get("created"),
                       "summary": meta.get("summary") or "", "active": cid == now,
                       "backend": meta.get("backend") or "camilladsp",
                       "editable": bool(meta.get("settings") and meta.get("measurement")),
                       "layout": meta.get("layout")})
    return result


def presets():
    """The corrections with a name, and the one in use: what the phone and the tray offer."""
    return [e for e in entries() if e["name"] or e["active"]]


def get(cid):
    meta = _meta(cid) if isinstance(cid, str) and _ID.fullmatch(cid) else None
    if meta is None:
        raise ValueError(f"no saved correction {cid!r}")
    return dict(meta, id=cid)


def _new_id(created):
    base = time.strftime("%Y%m%d-%H%M%S", time.localtime(created))
    cid, n = base, 1
    while (_root() / cid).exists():
        n += 1
        cid = f"{base}-{n}"
    return cid


def _same_files(folder, files):
    """Whether a saved correction has exactly these engine files."""
    mine = {p.name: p.read_bytes() for p in live_files(folder)}
    return mine == {p.name: p.read_bytes() for p in files}


def _keep_measurement(path):
    """Copy a measurement file into the store (once) and return its id."""
    if not path or not Path(path).exists():
        return None
    data = Path(path).read_bytes()
    mid = hashlib.sha1(data).hexdigest()[:16]
    target = _measurements() / f"{mid}.npz"
    if not target.exists():
        _measurements().mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".new")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    return mid


def measurement_id(path):
    """The id a measurement file has in the store (None if there is no file)."""
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def measurement_file(mid):
    path = _measurements() / f"{mid}.npz"
    return path if mid and path.exists() else None


def record(files, settings=None, layout=None, measurement=None, summary="", name="", created=None):
    """Save the engine files just applied as a correction and mark it as the one in use.
    The same files as a correction that is already saved: that one is used again instead.
    Returns the id. (Call ensure() before the new files replace the earlier ones.)"""
    _root().mkdir(parents=True, exist_ok=True)
    files = [Path(p) for p in files]
    for cid in _ids():
        if _same_files(_root() / cid, files):
            _set_active(cid)
            return cid
    created = created or time.time()
    cid = _new_id(created)
    folder = _root() / cid
    tmp = _root() / (cid + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for path in files:
        shutil.copyfile(path, tmp / path.name)
    meta = {"name": _clean(name), "created": created, "summary": summary, "backend": backend_of(files),
            "settings": settings, "layout": layout, "measurement": _keep_measurement(measurement)}
    (tmp / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.replace(tmp, folder)
    _set_active(cid)
    _prune()
    return cid


def _clean(name):
    name = " ".join(str(name or "").split())
    if len(name) > NAME_MAX:
        raise ValueError(f"a name can have at most {NAME_MAX} characters")
    return name


def rename(cid, name):
    """Give a correction a name (it becomes a preset and is kept) or take it away ("")."""
    meta = get(cid)
    meta.pop("id")
    meta["name"] = _clean(name)
    _write(_root() / cid / "meta.json", json.dumps(meta, indent=1))
    _prune()


def delete(cid):
    get(cid)
    if cid == active():
        raise ValueError("this correction is in use: use another one first")
    trash = _root() / (cid + ".old")
    os.replace(_root() / cid, trash)
    shutil.rmtree(trash, ignore_errors=True)
    _prune()


def use(cid, backend=None, folder=None):
    """Put a saved correction's engine files in place of the applied ones (it does not tell
    the engines: the caller does). backend: the playback engine in use. Returns its meta."""
    meta = get(cid)
    kind = meta.get("backend") or "camilladsp"
    if backend and kind != backend:
        raise ValueError("this correction was made for " + ("Equalizer APO" if kind == "equalizerapo" else "CamillaDSP")
                         + ": choose that playback engine in Setup first")
    folder = Path(folder or config.data_dir())
    files = live_files(_root() / cid, kind)
    if not files:
        raise ValueError("this saved correction has no engine files")
    for path in files:
        tmp = folder / (path.name + ".new")
        shutil.copyfile(path, tmp)
        config.replace_file(tmp, folder / path.name)
    keep = {p.name for p in files}
    for path in live_files(folder, kind):
        if path.name not in keep:
            path.unlink()
    _set_active(cid)
    return meta


def _prune():
    """Keep the presets, the one in use and the newest unnamed ones; drop measurements that
    no correction needs any more."""
    now, unnamed = active(), 0
    needed = set()
    for cid in _ids():
        meta = _meta(cid) or {}
        if not meta.get("name"):
            unnamed += 1
            if unnamed > HISTORY and cid != now:
                shutil.rmtree(_root() / cid, ignore_errors=True)
                continue
        needed.add(meta.get("measurement"))
    if _measurements().exists():
        for path in _measurements().glob("*.npz"):
            if path.stem not in needed:
                path.unlink(missing_ok=True)


def ensure():
    """The first time: keep the correction applied so far, before there was a history, as a
    preset, so that nothing is lost when a new one is applied. Before anything is written."""
    try:
        _root().mkdir(parents=True)  # whoever creates it does this, once
    except FileExistsError:
        return
    files = live_files()
    if files:
        created = max(p.stat().st_mtime for p in files)
        record(files, summary="Applied before roomfix kept a history", name="Earlier correction",
               created=created)
