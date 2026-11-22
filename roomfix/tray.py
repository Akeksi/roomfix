"""The supervisor's face on Windows: an icon in the notification area with a menu, the
volume keys of the keyboard, and a small on-screen volume display. Plain Win32, no extra
packages. Runs in its own thread of the supervisor process (`roomfix --engine`)."""
import ctypes
import logging
import os
import struct
import subprocess
import threading
import zlib

import numpy as np

from . import config

log = logging.getLogger("roomfix.engine")
COLORS = {"on": (60, 190, 140), "muted": (235, 160, 50), "waiting": (225, 85, 85), "off": (130, 136, 142)}


def source_name(status):
    """The chosen source, when there is more than one to choose from: "TV App", or ""."""
    sources = status.get("sources") or {}
    return sources.get(status.get("source"), "") if len(sources) > 1 else ""


def summary(status):
    """(icon, text) for the state of the correction: icon is a key of COLORS."""
    if not status["enabled"]:
        return "off", "roomfix: correction switched off"
    name = "roomfix" + (f" ({source_name(status)})" if source_name(status) else "")
    waiting = [e["detail"] or e["device"] for e in status["engines"] if e["status"] == "waiting"]
    if waiting:
        return "waiting", f"{name}: waiting, " + "; ".join(waiting)
    silent = [e["detail"] for e in status["engines"] if e.get("signal") is False]
    if silent:
        return "waiting", f"{name}: " + "; ".join(silent)
    if status["mute"]:
        return "muted", f"{name}: muted ({status['volume']:.1f} dB)"
    return "on", f"{name}: {status['volume']:.1f} dB"


# ---- icons ---------------------------------------------------------------
def _picture(state, size):
    """A loudspeaker as RGBA [size, size, 4]: sound waves when on, a cross when not."""
    n = size * 4  # drawn larger and averaged down, for smooth edges
    y, x = (np.mgrid[0:n, 0:n] + 0.5) / n
    cone = (x > 0.26) & (x < 0.52) & (np.abs(y - 0.5) < 0.15 + (x - 0.26) * 0.85)
    shape = ((x > 0.06) & (x <= 0.26) & (np.abs(y - 0.5) < 0.15)) | cone
    if state in ("on", "waiting"):
        r = np.hypot(x - 0.50, y - 0.5)
        beam = (x > 0.56) & (np.abs(y - 0.5) < (x - 0.48) * 1.1)
        shape |= beam & ((np.abs(r - 0.22) < 0.045) | (np.abs(r - 0.40) < 0.045))
    else:
        u, v = x - 0.78, y - 0.5
        shape |= (np.abs(u) < 0.17) & (np.abs(v) < 0.17) & ((np.abs(u - v) < 0.06) | (np.abs(u + v) < 0.06))
    alpha = shape.reshape(size, 4, size, 4).mean(axis=(1, 3))
    img = np.zeros((size, size, 4), np.uint8)
    img[..., :3] = COLORS[state]
    img[..., 3] = np.round(alpha * 255)
    return img


def _png(img):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\0" + row.tobytes() for row in img)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", img.shape[1], img.shape[0], 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def app_icon(size):
    """PNG of the phone page's home-screen icon: the loudspeaker on a dark tile. It stays
    inside the middle of the tile, which a phone keeps when it rounds the icon off."""
    inner = int(size * 0.64)
    picture = _picture("on", inner).astype(float)
    tile = np.zeros((size, size, 4))
    tile[...] = (17, 19, 21, 255)
    at = (size - inner) // 2
    part = tile[at:at + inner, at:at + inner]
    alpha = picture[..., 3:] / 255
    part[..., :3] = picture[..., :3] * alpha + part[..., :3] * (1 - alpha)
    return _png(np.round(tile).astype(np.uint8))


def icon_file(state, sizes=(16, 20, 24, 32, 48)):
    """Windows .ico data of the icon for a state."""
    images = [_png(_picture(state, s)) for s in sizes]
    head, offset = struct.pack("<HHH", 0, 1, len(images)), 6 + 16 * len(images)
    for size, data in zip(sizes, images):
        head += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return head + b"".join(images)


# ---- Win32 ---------------------------------------------------------------
WM_DESTROY, WM_PAINT, WM_CLOSE, WM_TIMER, WM_COMMAND, WM_HOTKEY = 0x02, 0x0F, 0x10, 0x113, 0x111, 0x312
WM_LBUTTONUP, WM_RBUTTONUP, WM_APP = 0x202, 0x205, 0x8000
WM_ICON, WM_OSD = WM_APP + 1, WM_APP + 2
KEYS = {1: (0, 0xAF, 1.0), 2: (0, 0xAE, -1.0), 3: (0, 0xAD, None),  # id: (modifier, key, steps)
        4: (4, 0xAF, 0.5), 5: (4, 0xAE, -0.5)}                      # 4 = Shift
SOURCE_ITEMS = {"pc": "PC: what Windows plays", "tv_app": "TV App: what the TV plays (subwoofer only)"}
MF_STRING, MF_GRAYED, MF_CHECKED, MF_POPUP, MF_SEPARATOR = 0, 1, 8, 0x10, 0x800


def _win32():
    from ctypes import wintypes as w
    u, s, g, k = (ctypes.WinDLL(n, use_last_error=True) for n in ("user32", "shell32", "gdi32", "kernel32"))
    lresult = ctypes.c_ssize_t
    proc = ctypes.WINFUNCTYPE(lresult, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WndClass(ctypes.Structure):
        _fields_ = [("style", w.UINT), ("proc", proc), ("cls_extra", ctypes.c_int), ("wnd_extra", ctypes.c_int),
                    ("instance", w.HINSTANCE), ("icon", w.HICON), ("cursor", w.HANDLE), ("background", w.HBRUSH),
                    ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]

    class IconData(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("hwnd", w.HWND), ("id", w.UINT), ("flags", w.UINT), ("message", w.UINT),
                    ("icon", w.HICON), ("tip", w.WCHAR * 128), ("state", w.DWORD), ("state_mask", w.DWORD),
                    ("info", w.WCHAR * 256), ("version", w.UINT), ("info_title", w.WCHAR * 64),
                    ("info_flags", w.DWORD), ("guid", ctypes.c_byte * 16), ("balloon_icon", w.HICON)]

    class Paint(ctypes.Structure):
        _fields_ = [("hdc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT), ("restore", w.BOOL), ("update", w.BOOL),
                    ("reserved", ctypes.c_byte * 32)]

    def sig(lib, name, res, *args):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args

    P = ctypes.POINTER
    sig(k, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
    sig(u, "RegisterClassW", w.ATOM, P(WndClass))
    sig(u, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
    sig(u, "DefWindowProcW", lresult, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    sig(u, "DestroyWindow", w.BOOL, w.HWND)
    sig(u, "PostMessageW", w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    sig(u, "GetMessageW", w.BOOL, P(w.MSG), w.HWND, w.UINT, w.UINT)
    sig(u, "TranslateMessage", w.BOOL, P(w.MSG))
    sig(u, "DispatchMessageW", lresult, P(w.MSG))
    sig(u, "PostQuitMessage", None, ctypes.c_int)
    sig(u, "RegisterWindowMessageW", w.UINT, w.LPCWSTR)
    sig(u, "SetTimer", ctypes.c_size_t, w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID)
    sig(u, "KillTimer", w.BOOL, w.HWND, ctypes.c_size_t)
    sig(u, "RegisterHotKey", w.BOOL, w.HWND, ctypes.c_int, w.UINT, w.UINT)
    sig(u, "UnregisterHotKey", w.BOOL, w.HWND, ctypes.c_int)
    sig(u, "LoadImageW", w.HANDLE, w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT)
    sig(u, "DestroyIcon", w.BOOL, w.HICON)
    sig(u, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
    sig(u, "CreatePopupMenu", w.HMENU)
    sig(u, "AppendMenuW", w.BOOL, w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR)
    sig(u, "DestroyMenu", w.BOOL, w.HMENU)
    sig(u, "TrackPopupMenu", ctypes.c_int, w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND,
        w.LPVOID)
    sig(u, "SetForegroundWindow", w.BOOL, w.HWND)
    sig(u, "GetCursorPos", w.BOOL, P(w.POINT))
    sig(u, "SetWindowPos", w.BOOL, w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT)
    sig(u, "ShowWindow", w.BOOL, w.HWND, ctypes.c_int)
    sig(u, "InvalidateRect", w.BOOL, w.HWND, w.LPVOID, w.BOOL)
    sig(u, "SetLayeredWindowAttributes", w.BOOL, w.HWND, w.DWORD, ctypes.c_ubyte, w.DWORD)
    sig(u, "BeginPaint", w.HDC, w.HWND, P(Paint))
    sig(u, "EndPaint", w.BOOL, w.HWND, P(Paint))
    sig(u, "FillRect", ctypes.c_int, w.HDC, P(w.RECT), w.HBRUSH)
    sig(u, "DrawTextW", ctypes.c_int, w.HDC, w.LPCWSTR, ctypes.c_int, P(w.RECT), w.UINT)
    sig(u, "GetDpiForSystem", w.UINT)
    sig(g, "CreateSolidBrush", w.HBRUSH, w.DWORD)
    sig(g, "DeleteObject", w.BOOL, w.HANDLE)
    sig(g, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
    sig(g, "SetTextColor", w.DWORD, w.HDC, w.DWORD)
    sig(g, "SetBkMode", ctypes.c_int, w.HDC, ctypes.c_int)
    sig(g, "CreateFontW", w.HANDLE, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
    sig(s, "Shell_NotifyIconW", w.BOOL, w.DWORD, P(IconData))
    return u, s, g, k, w, proc, WndClass, IconData, Paint


class Tray:
    def __init__(self, sup):
        self.sup, self.hwnd, self.ready = sup, None, threading.Event()
        self.osd = self.taskbar_created = None
        self.thread = threading.Thread(target=self._guarded, daemon=True)
        sup.listeners.append(self._changed_elsewhere)

    def start(self):
        self.thread.start()

    def _changed_elsewhere(self):
        """The volume, mute or power was changed through the API (the phone page, a hotkey
        script): show it like a volume key does. Runs in the server's thread; the window's
        own thread draws."""
        if self.hwnd:
            self.u.PostMessageW(self.hwnd, WM_OSD, 0, 0)

    def close(self):
        """Remove the icon and give the volume keys back to Windows."""
        if self._changed_elsewhere in self.sup.listeners:
            self.sup.listeners.remove(self._changed_elsewhere)
        if self.ready.wait(5) and self.hwnd:
            self.u.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self.thread.join(3)

    def _guarded(self):
        try:
            self._run()
        except Exception:  # the correction must run even if the icon cannot be shown
            log.exception("tray icon failed")
        finally:
            self.ready.set()

    # ---- window thread -----------------------------------------------------
    def _run(self):
        (self.u, self.s, self.g, k, w, proc, WndClass, self.IconData, self.Paint) = _win32()
        u, self.w = self.u, w
        self.instance = k.GetModuleHandleW(None)
        self.proc = proc(self._message)  # keep a reference: Windows calls it
        for name in ("roomfix-tray", "roomfix-osd"):
            cls = WndClass(proc=self.proc, instance=self.instance, name=name)
            u.RegisterClassW(ctypes.byref(cls))
        self.hwnd = u.CreateWindowExW(0, "roomfix-tray", "roomfix", 0, 0, 0, 0, 0, None, None, self.instance, None)
        # the volume display: on top, never takes the focus, clicks pass through it
        self.scale = (u.GetDpiForSystem() or 96) / 96
        self.osd = u.CreateWindowExW(0x08000000 | 0x80000 | 0x80 | 0x20 | 0x8, "roomfix-osd", "", 0x80000000,
                                     0, 0, 10, 10, None, None, self.instance, None)
        u.SetLayeredWindowAttributes(self.osd, 0, 235, 2)
        self.font = self.g.CreateFontW(int(-20 * self.scale), 0, 0, 0, 500, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        self.osd_text, self.osd_bar = "", None
        self.icons, self.shown, self.hot = {}, None, False
        self.source_seen = self.sup.status().get("source")
        self.taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
        self._icon(add=True)
        self._refresh()
        u.SetTimer(self.hwnd, 1, 1000, None)
        self.ready.set()
        msg = w.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def _message(self, hwnd, msg, wparam, lparam):
        try:
            if hwnd == self.osd:
                if msg == WM_PAINT:
                    return self._paint()
            elif msg == WM_ICON and lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                self._menu()
            elif msg == WM_HOTKEY and wparam in KEYS:
                steps = KEYS[wparam][2]
                self._act(self.sup.set_mute if steps is None
                          else lambda: self.sup.set_volume(delta=steps * self.sup.status()["step_db"]))
            elif msg == WM_OSD:
                self._act(lambda: None)
            elif msg == WM_TIMER and wparam == 1:
                self._refresh()
            elif msg == WM_TIMER and wparam == 2:
                self.u.KillTimer(hwnd, 2)
                self.u.ShowWindow(self.osd, 0)
            elif msg == self.taskbar_created:  # Explorer restarted: the icon is gone
                self.shown = None
                self._icon(add=True)
                self._refresh()
            elif msg == WM_CLOSE:
                self._keys(False)
                self.s.Shell_NotifyIconW(2, ctypes.byref(self._data()))
                self.u.DestroyWindow(self.osd)
                self.u.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                self.u.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("tray icon")
        return self.u.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---- icon ----------------------------------------------------------------
    def _data(self):
        data = self.IconData(hwnd=self.hwnd, id=1)
        data.size = ctypes.sizeof(data)
        return data

    def _icon(self, add=False):
        state, text = summary(self.sup.status())
        if (state, text) == self.shown and not add:
            return
        if state not in self.icons:
            path = config.data_dir() / f"icon_{state}.ico"
            path.write_bytes(icon_file(state))
            size = self.u.GetSystemMetrics(49)  # small icon, in pixels of this screen
            self.icons[state] = self.u.LoadImageW(None, str(path), 1, size, size, 0x10)
        data = self._data()
        data.flags, data.message, data.icon, data.tip = 1 | 2 | 4, WM_ICON, self.icons[state], text[:127]
        if self.s.Shell_NotifyIconW(0 if add else 1, ctypes.byref(data)):
            self.shown = (state, text)

    def _keys(self, want):
        """Take the volume keys of the keyboard, or give them back to Windows."""
        if want == self.hot:
            return
        self.hot = want
        for ident, (mod, key, _) in KEYS.items():
            if not want:
                self.u.UnregisterHotKey(self.hwnd, ident)
            elif not self.u.RegisterHotKey(self.hwnd, ident, mod, key):
                log.warning("volume key %#x is taken by another program", key)

    def _refresh(self):
        status = self.sup.status()
        self._keys(status["enabled"] and status["keys"])
        if self.shown is None:
            self._icon(add=True)  # the notification area was not ready before
        else:
            self._icon()

    def _act(self, action, show=True):
        """Do something the user asked for, and show the new state."""
        action()
        self._refresh()
        if show:
            status = self.sup.status()
            if status.get("source") != self.source_seen:  # switched: say to what
                self.source_seen = status.get("source")
                return self._show(f"Source: {source_name(status) or 'PC'}", None)
            if not status["enabled"]:
                return self._show("Correction switched off", None)
            level = (status["volume"] - status["min_db"]) / (status["max_db"] - status["min_db"])
            text = f"{status['volume']:.1f} dB"
            self._show(f"Muted ({text})" if status["mute"] else text, 0.0 if status["mute"] else level)

    # ---- menu ------------------------------------------------------------------
    def _menu(self):
        u, status = self.u, self.sup.status()
        on, step = status["enabled"], status["step_db"]
        actions, menus = {}, []

        def menu():
            menus.append(u.CreatePopupMenu())
            return menus[-1]

        def item(m, text, action=None, checked=False, enabled=True):
            ident = len(actions) + 1
            actions[ident] = action
            flags = MF_STRING | (MF_CHECKED if checked else 0) | (0 if enabled and action else MF_GRAYED)
            u.AppendMenuW(m, flags, ident, text)

        top, vol = menu(), menu()
        item(top, summary(status)[1])
        for eng in status["engines"] if on else []:
            item(top, f"    {eng['device'] or 'engine'}: {eng['detail'] or eng['status']}")
        u.AppendMenuW(top, MF_SEPARATOR, 0, None)
        sources = status.get("sources") or {}
        if len(sources) > 1:  # the TV App source is set up
            for name in sources:
                item(top, SOURCE_ITEMS.get(name, sources[name]),
                     lambda name=name: self._act(lambda: self.sup.set_source(name)), checked=status["source"] == name)
            u.AppendMenuW(top, MF_SEPARATOR, 0, None)
        item(top, "Mute", lambda: self._act(self.sup.set_mute), checked=on and status["mute"], enabled=on)
        for db in (-10, -20, -30, -40):
            item(vol, f"{db} dB", lambda db=db: self._act(lambda: self.sup.set_volume(value=db)))
        u.AppendMenuW(vol, MF_SEPARATOR, 0, None)
        item(vol, f"+{step:g} dB", lambda: self._act(lambda: self.sup.set_volume(delta=step)))
        item(vol, f"−{step:g} dB", lambda: self._act(lambda: self.sup.set_volume(delta=-step)))
        u.AppendMenuW(top, MF_POPUP | (0 if on else MF_GRAYED), vol, "Volume")
        u.AppendMenuW(top, MF_SEPARATOR, 0, None)
        item(top, "Open roomfix", self._open_page)
        item(top, "Restart engines", lambda: self._act(self.sup.restart, show=False), enabled=on)
        u.AppendMenuW(top, MF_SEPARATOR, 0, None)
        item(top, "Correction on", lambda: self._act(self.sup.set_enabled), checked=on)
        item(top, "Volume keys set this volume", lambda: self._act(self.sup.set_keys, show=False),
             checked=status["keys"])
        u.AppendMenuW(top, MF_SEPARATOR, 0, None)
        item(top, "Open log folder", lambda: os.startfile(config.data_dir()))
        item(top, "Stop correction and close", self.sup.quit.set)
        point = self.w.POINT()
        u.GetCursorPos(ctypes.byref(point))
        u.SetForegroundWindow(self.hwnd)  # or the menu stays open when clicking elsewhere
        chosen = u.TrackPopupMenu(top, 0x100 | 0x20, point.x, point.y, 0, self.hwnd, None)  # returns the choice
        u.PostMessageW(self.hwnd, 0, 0, 0)
        u.DestroyMenu(top)
        if actions.get(chosen):
            actions[chosen]()

    @staticmethod
    def _open_page():
        """Open the roomfix page: starts roomfix, which points to a running one if there is."""
        from . import supervisor
        info = subprocess.STARTUPINFO()
        info.dwFlags, info.wShowWindow = 1, 7  # its console window: minimized, without taking the focus
        subprocess.Popen(supervisor.command(page=True), creationflags=0x10, startupinfo=info)  # own console

    # ---- on-screen display ---------------------------------------------------
    def _show(self, text, bar):
        u, k = self.u, self.scale
        self.osd_text, self.osd_bar = text, bar
        width, height = int(320 * k), int(84 * k)
        x, y = (u.GetSystemMetrics(0) - width) // 2, u.GetSystemMetrics(1) - height - int(110 * k)
        u.SetWindowPos(self.osd, -1, x, y, width, height, 0x10 | 0x40)  # topmost, shown, not activated
        u.InvalidateRect(self.osd, None, True)
        u.SetTimer(self.hwnd, 2, 1500, None)

    def _paint(self):
        u, g, k = self.u, self.g, self.scale
        paint = self.Paint()
        dc = u.BeginPaint(self.osd, ctypes.byref(paint))
        width, height = int(320 * k), int(84 * k)

        def fill(left, top, right, bottom, color):
            brush = g.CreateSolidBrush(color)
            u.FillRect(dc, ctypes.byref(self.w.RECT(int(left), int(top), int(right), int(bottom))), brush)
            g.DeleteObject(brush)

        fill(0, 0, width, height, 0x211E1B)  # colours are 0xBBGGRR
        old = g.SelectObject(dc, self.font)
        g.SetBkMode(dc, 1)
        g.SetTextColor(dc, 0xF3F1EE)
        area = self.w.RECT(0, int(12 * k), width, int((54 if self.osd_bar is not None else 72) * k))
        u.DrawTextW(dc, self.osd_text, -1, ctypes.byref(area), 0x1 | 0x4 | 0x20 | 0x8000)  # centred, one line
        g.SelectObject(dc, old)
        if self.osd_bar is not None:
            left, right, top = 28 * k, width - 28 * k, 60 * k
            fill(left, top, right, top + 8 * k, 0x332E2A)
            fill(left, top, left + (right - left) * self.osd_bar, top + 8 * k, 0x8CBE3C)
        u.EndPaint(self.osd, ctypes.byref(paint))
        return 0
