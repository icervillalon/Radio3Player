"""Integración con Windows:

* MediaControls  — controles multimedia del sistema (SMTC): las teclas de play/pausa/
  siguiente/anterior del teclado, el panel multimedia de Windows (el que sale al subir el
  volumen o en la configuración rápida de Windows 11) con título, programa e imagen, y la
  barra de posición de ese panel. Si el paquete `winrt` no está instalado, cae a
  registrar las teclas multimedia como atajos globales.
* ThumbBar       — botones −15 s / play-pausa / +30 s en la miniatura de la barra de tareas
  (ITaskbarList3), como hace Spotify.

Todo es opcional: si algo falla se registra en app.log y la app sigue funcionando.
Los avisos llegan por `emit(comando, argumento)`, que debe ser seguro entre hilos.
Comandos: "toggle", "play", "pause", "stop", "next", "prev", "seek" (arg = segundos).
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import timedelta
from pathlib import Path

IS_WIN = sys.platform == "win32"
_LOG_PATH: Path | None = None


def set_log_path(path: Path) -> None:
    global _LOG_PATH
    _LOG_PATH = path


def log(msg: str) -> None:
    if not _LOG_PATH:
        return
    try:
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > 200_000:
            _LOG_PATH.unlink()
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg.rstrip() + "\n")
    except OSError:
        pass


def init_com_sta() -> None:
    """El hilo de la interfaz como STA: es lo que esperan Tk y la barra de tareas."""
    if not IS_WIN:
        return
    import ctypes

    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
    except Exception:
        log("CoInitializeEx: " + traceback.format_exc())


# ===========================================================================
# Controles multimedia del sistema
# ===========================================================================


class _SMTCBackend:
    name = "SMTC"

    def __init__(self, emit):
        from winrt.windows.media import (  # noqa: F401  (importes comprobados al iniciar)
            MediaPlaybackStatus,
            MediaPlaybackType,
            SystemMediaTransportControlsTimelineProperties,
        )
        from winrt.windows.media.playback import MediaPlayer

        self.emit = emit
        self.Status = MediaPlaybackStatus
        self.Type = MediaPlaybackType
        self.Timeline = SystemMediaTransportControlsTimelineProperties
        # Un MediaPlayer "vacío" solo para obtener los controles del sistema: el audio
        # lo sigue reproduciendo VLC.
        self.mp = MediaPlayer()
        self.mp.command_manager.is_enabled = False
        s = self.smtc = self.mp.system_media_transport_controls
        s.is_enabled = True
        s.is_play_enabled = True
        s.is_pause_enabled = True
        s.is_stop_enabled = True
        s.is_next_enabled = True
        s.is_previous_enabled = True
        self._tok_btn = s.add_button_pressed(self._on_button)
        self._tok_pos = s.add_playback_position_change_requested(self._on_seek)
        s.playback_status = MediaPlaybackStatus.CLOSED
        self._thumb_url = None

    # Eventos de Windows (llegan en otro hilo)
    _BUTTONS = {0: "play", 1: "pause", 2: "stop", 4: "next", 5: "prev", 6: "next", 7: "prev"}

    def _on_button(self, _sender, args):
        try:
            cmd = self._BUTTONS.get(int(args.button))
            if cmd:
                self.emit(cmd, None)
        except Exception:
            log("SMTC botón: " + traceback.format_exc())

    def _on_seek(self, _sender, args):
        try:
            self.emit("seek", args.requested_playback_position.total_seconds())
        except Exception:
            log("SMTC posición: " + traceback.format_exc())

    # Actualizaciones desde la app
    def set_status(self, status: str):
        self.smtc.playback_status = {
            "playing": self.Status.PLAYING,
            "paused": self.Status.PAUSED,
            "stopped": self.Status.STOPPED,
            "closed": self.Status.CLOSED,
        }.get(status, self.Status.STOPPED)

    def set_metadata(self, title: str, artist: str, album: str, image_url: str | None, live: bool):
        s = self.smtc
        s.is_next_enabled = not live
        s.is_previous_enabled = not live
        du = s.display_updater
        du.type = self.Type.MUSIC
        mp = du.music_properties
        mp.title = title or ""
        mp.artist = artist or ""
        try:
            mp.album_title = album or ""
        except Exception:
            pass
        if image_url != self._thumb_url:
            self._thumb_url = image_url
            try:
                if image_url:
                    from winrt.windows.foundation import Uri
                    from winrt.windows.storage.streams import RandomAccessStreamReference

                    du.thumbnail = RandomAccessStreamReference.create_from_uri(Uri(image_url))
                else:
                    du.thumbnail = None
            except Exception:
                log("SMTC imagen: " + traceback.format_exc())
        du.update()

    def set_timeline(self, position: float, duration: float):
        tl = self.Timeline()
        tl.start_time = timedelta(0)
        tl.min_seek_time = timedelta(0)
        tl.end_time = timedelta(seconds=max(0.0, duration))
        tl.max_seek_time = timedelta(seconds=max(0.0, duration))
        tl.position = timedelta(seconds=min(max(0.0, position), max(0.0, duration)))
        self.smtc.update_timeline_properties(tl)

    def close(self):
        try:
            self.smtc.playback_status = self.Status.CLOSED
            self.smtc.display_updater.clear_all()
            self.smtc.remove_button_pressed(self._tok_btn)
            self.smtc.remove_playback_position_change_requested(self._tok_pos)
        except Exception:
            pass
        try:
            self.mp.close()
        except Exception:
            pass


class _HotkeyBackend:
    """Respaldo sin winrt: registra las teclas multimedia como atajos globales mientras
    la app está abierta (otras apps dejan de recibirlas en ese tiempo)."""

    name = "atajos globales"
    VK = {0xB3: "toggle", 0xB2: "stop", 0xB0: "next", 0xB1: "prev"}

    def __init__(self, emit):
        import ctypes  # noqa: F401

        self.emit = emit
        self.tid = None
        self.registered: list[int] = []
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True, name="hotkeys")
        self.thread.start()
        ready.wait(2)
        if not self.registered:
            raise RuntimeError("No se pudo registrar ninguna tecla multimedia (¿las usa otra app?)")

    def _run(self, ready):
        import ctypes
        from ctypes import wintypes as wt

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self.tid = kernel32.GetCurrentThreadId()
        for i, vk in enumerate(self.VK, start=1):
            if user32.RegisterHotKey(None, i, 0x4000, vk):  # MOD_NOREPEAT
                self.registered.append(i)
        ids = {i: cmd for i, cmd in enumerate(self.VK.values(), start=1)}
        ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                cmd = ids.get(int(msg.wParam))
                if cmd:
                    self.emit(cmd, None)
        for i in self.registered:
            user32.UnregisterHotKey(None, i)

    def set_status(self, status):
        pass

    def set_metadata(self, *a, **k):
        pass

    def set_timeline(self, *a, **k):
        pass

    def close(self):
        if self.tid:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT


class _ThreadedSMTC:
    """Aloja los controles del sistema en un hilo propio (apartamento MTA) para no
    interferir con el hilo de la interfaz (STA). Todas las llamadas pasan por una cola."""

    name = "SMTC"

    def __init__(self, emit):
        import queue

        self.q: queue.Queue = queue.Queue()
        self.error: BaseException | None = None
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(emit, ready), daemon=True, name="smtc")
        self.thread.start()
        if not ready.wait(8):
            raise TimeoutError("los controles multimedia de Windows no respondieron")
        if self.error:
            raise self.error

    def _run(self, emit, ready):
        try:
            import ctypes

            ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
            backend = _SMTCBackend(emit)
        except BaseException as e:  # noqa: BLE001
            self.error = e
            ready.set()
            return
        ready.set()
        while True:
            item = self.q.get()
            if item is None:
                backend.close()
                return
            fn, args = item
            try:
                getattr(backend, fn)(*args)
            except Exception:
                log(f"SMTC {fn}: " + traceback.format_exc())

    def set_status(self, *a):
        self.q.put(("set_status", a))

    def set_metadata(self, *a):
        self.q.put(("set_metadata", a))

    def set_timeline(self, *a):
        self.q.put(("set_timeline", a))

    def close(self):
        self.q.put(None)
        self.thread.join(2)


class MediaControls:
    def __init__(self, emit):
        self.backend = None
        self.error = ""
        self._last_meta = None
        self._last_status = None
        if not IS_WIN:
            return
        try:
            self.backend = _ThreadedSMTC(emit)
        except Exception as e:
            self.error = f"SMTC no disponible ({e.__class__.__name__}: {e})"
            log(self.error + "\n" + traceback.format_exc())
            try:
                self.backend = _HotkeyBackend(emit)
            except Exception as e2:
                log(f"Atajos globales no disponibles: {e2}")
                self.backend = None
        if self.backend:
            log(f"Teclas multimedia: {self.backend.name}")

    @property
    def mode(self) -> str:
        return self.backend.name if self.backend else "ninguno"

    def _safe(self, fn, *args):
        if not self.backend:
            return
        try:
            fn(*args)
        except Exception:
            log("MediaControls: " + traceback.format_exc())

    def set_status(self, status: str):
        if status != self._last_status:
            self._last_status = status
            self._safe(self.backend.set_status if self.backend else None, status)

    def set_metadata(self, title, artist, album="", image_url=None, live=False):
        key = (title, artist, album, image_url, live)
        if key != self._last_meta:
            self._last_meta = key
            self._safe(self.backend.set_metadata if self.backend else None, title, artist, album,
                       image_url, live)

    def set_timeline(self, position: float, duration: float):
        if duration > 0:
            self._safe(self.backend.set_timeline if self.backend else None, position, duration)

    def close(self):
        self._safe(self.backend.close if self.backend else None)


# ===========================================================================
# Iconos (generados sin dependencias: ICO con imágenes BMP/PNG)
# ===========================================================================

def _shape_hit(shape: str, x: float, y: float) -> bool:
    """x, y en [0,1]. Formas de los botones."""

    def tri(ax, ay, bx, by, cx, cy):
        d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
        d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
        d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
        neg = d1 < 0 or d2 < 0 or d3 < 0
        pos = d1 > 0 or d2 > 0 or d3 > 0
        return not (neg and pos)

    def rect(x0, y0, x1, y1):
        return x0 <= x <= x1 and y0 <= y <= y1

    if shape == "play":
        return tri(0.27, 0.16, 0.27, 0.84, 0.85, 0.5)
    if shape == "pause":
        return rect(0.22, 0.18, 0.42, 0.82) or rect(0.58, 0.18, 0.78, 0.82)
    if shape == "back":
        return tri(0.50, 0.22, 0.50, 0.78, 0.08, 0.5) or tri(0.92, 0.22, 0.92, 0.78, 0.50, 0.5)
    if shape == "fwd":
        return tri(0.50, 0.22, 0.50, 0.78, 0.92, 0.5) or tri(0.08, 0.22, 0.08, 0.78, 0.50, 0.5)
    raise ValueError(shape)


def render_rgba(shape: str, size: int, color=(255, 255, 255), ss: int = 4) -> bytes:
    """Imagen RGBA (filas de arriba abajo) con antialiasing por supermuestreo."""
    out = bytearray()
    step = 1.0 / (size * ss)
    if shape == "app":
        # Círculo rojo Radio 3 con un "play" blanco
        red = (255, 90, 54)
        for py in range(size):
            for px in range(size):
                cov_c = cov_p = 0
                for sy in range(ss):
                    for sx in range(ss):
                        x = (px * ss + sx + 0.5) * step
                        y = (py * ss + sy + 0.5) * step
                        if (x - 0.5) ** 2 + (y - 0.5) ** 2 <= 0.47 ** 2:
                            cov_c += 1
                            u, v = (x - 0.5) / 0.62 + 0.5 + 0.04, (y - 0.5) / 0.62 + 0.5
                            if _shape_hit("play", u, v):
                                cov_p += 1
                n = ss * ss
                a = cov_c / n
                if a == 0:
                    out += b"\0\0\0\0"
                    continue
                w = cov_p / max(cov_c, 1)
                r = round(red[0] * (1 - w) + 255 * w)
                g = round(red[1] * (1 - w) + 255 * w)
                b = round(red[2] * (1 - w) + 255 * w)
                out += bytes((r, g, b, round(255 * a)))
        return bytes(out)
    for py in range(size):
        for px in range(size):
            cov = 0
            for sy in range(ss):
                for sx in range(ss):
                    if _shape_hit(shape, (px * ss + sx + 0.5) * step, (py * ss + sy + 0.5) * step):
                        cov += 1
            out += bytes((*color, round(255 * cov / (ss * ss))))
    return bytes(out)


def _png(rgba: bytes, size: int) -> bytes:
    import struct
    import zlib

    raw = b"".join(b"\0" + rgba[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(t, data):
        c = struct.pack(">I", len(data)) + t + data
        return c + struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _bmp_entry(rgba: bytes, size: int) -> bytes:
    import struct

    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):  # BMP: de abajo arriba, BGRA
        row = bytearray()
        for x in range(size):
            r, g, b, a = rgba[(y * size + x) * 4:(y * size + x) * 4 + 4]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    mask_row = b"\0" * (((size + 31) // 32) * 4)
    return header + b"".join(rows) + mask_row * size


def make_ico(shape: str, sizes=(16, 20, 24, 32, 40, 48), color=(255, 255, 255)) -> bytes:
    import struct

    images = []
    for s in sizes:
        rgba = render_rgba(shape, s, color)
        images.append((s, _png(rgba, s) if s >= 256 else _bmp_entry(rgba, s)))
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in images:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    return head + entries + blobs


def windows_light_theme() -> bool:
    if not IS_WIN:
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
    except OSError:
        return False


def ensure_icons(directory: Path) -> dict[str, Path]:
    """Genera (una vez) los iconos de los botones en el color del tema de Windows."""
    light = windows_light_theme()
    color = (32, 32, 32) if light else (255, 255, 255)
    tag = "claro" if light else "oscuro"
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for shape in ("back", "play", "pause", "fwd"):
        p = directory / f"{shape}_{tag}.ico"
        if not p.exists():
            p.write_bytes(make_ico(shape, color=color))
        paths[shape] = p
    return paths


# ===========================================================================
# Botones en la miniatura de la barra de tareas
# ===========================================================================

if IS_WIN:
    import ctypes
    from ctypes import wintypes as wt

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

    class _THUMBBUTTON(ctypes.Structure):
        _fields_ = [("dwMask", wt.DWORD), ("iId", wt.UINT), ("iBitmap", wt.UINT), ("hIcon", wt.HICON),
                    ("szTip", ctypes.c_wchar * 260), ("dwFlags", wt.DWORD)]

    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class ThumbBar:
    IDS = {1: "prev", 2: "toggle", 3: "next"}
    THB_ICON, THB_TOOLTIP, THB_FLAGS = 0x2, 0x4, 0x8
    THBF_ENABLED, THBF_DISABLED = 0x0, 0x1
    WM_COMMAND = 0x0111
    THBN_CLICKED = 0x1800

    def __init__(self, root, emit, icon_dir: Path):
        self.ok = False
        self.root = root
        self.emit = emit
        self.icon_dir = icon_dir
        self.tbl = None
        self.hwnd = None
        self._old_proc = None
        self._proc = None
        self._added = False
        self._playing = False
        self._seek_enabled = True
        if not IS_WIN:
            return
        try:
            self._setup()
            self.ok = True
        except Exception:
            log("ThumbBar: " + traceback.format_exc())

    # -- COM -------------------------------------------------------------
    def _vcall(self, index, *args, argtypes=()):
        vtbl = ctypes.cast(self.tbl, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
        return fn(self.tbl, *args)

    def _create_taskbarlist(self):
        ole32 = ctypes.windll.ole32
        clsid, iid = _GUID(), _GUID()
        ole32.CLSIDFromString("{56FDF344-FD6D-11d0-958A-006097C9A090}", ctypes.byref(clsid))
        ole32.CLSIDFromString("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}", ctypes.byref(iid))
        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr))
        if hr != 0 or not ptr:
            raise OSError(f"CoCreateInstance(TaskbarList) = 0x{hr & 0xFFFFFFFF:08X}")
        self.tbl = ptr
        hr = self._vcall(3)  # HrInit
        if hr != 0:
            raise OSError(f"HrInit = 0x{hr & 0xFFFFFFFF:08X}")

    # -- iconos ------------------------------------------------------------
    def _load_icons(self):
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wt.HANDLE
        user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
        size = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON (según el escalado)
        self.icons = {}
        for name, path in ensure_icons(self.icon_dir).items():
            h = user32.LoadImageW(None, str(path), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            if not h:
                raise OSError(f"No se pudo cargar el icono {path}")
            self.icons[name] = h

    def _buttons(self):
        arr = (_THUMBBUTTON * 3)()
        spec = [
            (1, "back", "Retroceder 15 s", self._seek_enabled),
            (2, "pause" if self._playing else "play", "Pausa" if self._playing else "Reproducir", True),
            (3, "fwd", "Avanzar 30 s", self._seek_enabled),
        ]
        for b, (iid, icon, tip, enabled) in zip(arr, spec):
            b.dwMask = self.THB_ICON | self.THB_TOOLTIP | self.THB_FLAGS
            b.iId = iid
            b.hIcon = self.icons[icon]
            b.szTip = tip
            b.dwFlags = self.THBF_ENABLED if enabled else self.THBF_DISABLED
        return arr

    def _add_buttons(self):
        hr = self._vcall(15, self.hwnd, 3, self._buttons(),  # ThumbBarAddButtons
                         argtypes=(wt.HWND, wt.UINT, ctypes.POINTER(_THUMBBUTTON)))
        self._added = hr == 0
        if not self._added:
            log(f"ThumbBarAddButtons = 0x{hr & 0xFFFFFFFF:08X} (se reintentará)")

    def _update_buttons(self):
        if not self._added:
            return
        hr = self._vcall(16, self.hwnd, 3, self._buttons(),  # ThumbBarUpdateButtons
                         argtypes=(wt.HWND, wt.UINT, ctypes.POINTER(_THUMBBUTTON)))
        if hr != 0:
            log(f"ThumbBarUpdateButtons = 0x{hr & 0xFFFFFFFF:08X}")

    # -- ventana -----------------------------------------------------------
    def _setup(self):
        user32 = ctypes.windll.user32
        self.root.update_idletasks()
        self.hwnd = int(self.root.wm_frame(), 16)
        self._load_icons()
        self._create_taskbarlist()
        self.WM_TBC = user32.RegisterWindowMessageW("TaskbarButtonCreated")
        try:  # por si la app se ejecuta como administrador
            user32.ChangeWindowMessageFilterEx(wt.HWND(self.hwnd), self.WM_TBC, 1, None)
            user32.ChangeWindowMessageFilterEx(wt.HWND(self.hwnd), self.WM_COMMAND, 1, None)
        except Exception:
            pass
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        self._proc = _WNDPROC(self._wndproc)
        self._old_proc = user32.SetWindowLongPtrW(self.hwnd, -4, ctypes.cast(self._proc, ctypes.c_void_p))
        if not self._old_proc:
            raise OSError("SetWindowLongPtrW falló")
        # Si el botón de la barra de tareas ya existe, añadimos ya; si no, al llegar TaskbarButtonCreated.
        self._add_buttons()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == self.WM_COMMAND and (wparam >> 16) & 0xFFFF == self.THBN_CLICKED:
                cmd = self.IDS.get(wparam & 0xFFFF)
                if cmd:
                    self.emit(cmd, None)
                return 0
            if msg == self.WM_TBC:
                # Botón (re)creado, p. ej. al reiniciarse el Explorador
                self.root.after(50, self._recreate)
        except Exception:
            log("ThumbBar wndproc: " + traceback.format_exc())
        return ctypes.windll.user32.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)

    def _recreate(self):
        try:
            self._added = False
            self._create_taskbarlist()
            self._add_buttons()
        except Exception:
            log("ThumbBar recreate: " + traceback.format_exc())

    # -- API -----------------------------------------------------------------
    def set_state(self, playing: bool, seek_enabled: bool):
        if not self.ok or (playing, seek_enabled) == (self._playing, self._seek_enabled):
            return
        self._playing, self._seek_enabled = playing, seek_enabled
        try:
            self._update_buttons()
        except Exception:
            log("ThumbBar update: " + traceback.format_exc())

    def close(self):
        if not self.ok:
            return
        try:
            if self._old_proc:
                ctypes.windll.user32.SetWindowLongPtrW(self.hwnd, -4, self._old_proc)
                self._old_proc = None
        except Exception:
            pass


# Generación de iconos desde la línea de órdenes (para el icono de la app)
if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "icono.ico")
    import struct

    sizes = (16, 20, 24, 32, 40, 48, 64, 256)
    images = []
    for s in sizes:
        rgba = render_rgba("app", s, ss=4 if s <= 64 else 2)
        images.append((s, _png(rgba, s) if s >= 64 else _bmp_entry(rgba, s)))
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, data in images:
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    out.write_bytes(head + entries + blobs)
    print("Icono escrito en", out)
