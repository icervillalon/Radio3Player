"""Envoltorio mínimo sobre libVLC (python-vlc), solo audio."""

from __future__ import annotations

import os
import sys


def _add_vlc_dll_dir() -> None:
    """En Windows ayuda a python-vlc a encontrar libvlc.dll."""
    if sys.platform != "win32":
        return
    candidates = [
        os.environ.get("PYTHON_VLC_LIB_PATH", ""),
        r"C:\Program Files\VideoLAN\VLC",
        r"C:\Program Files (x86)\VideoLAN\VLC",
    ]
    for d in candidates:
        if d and os.path.isdir(d):
            d = d if os.path.isdir(d) else os.path.dirname(d)
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            os.environ.setdefault("PYTHON_VLC_MODULE_PATH", os.path.join(d, "plugins"))
            break


class PlayerUnavailable(Exception):
    pass


class Player:
    def __init__(self, network_caching_ms: int = 3000):
        _add_vlc_dll_dir()
        try:
            import vlc  # noqa: WPS433
        except Exception as e:  # ImportError u OSError si falta libvlc
            raise PlayerUnavailable(
                "No se pudo cargar VLC. Instala VLC (64 bits, como tu Python) desde "
                "videolan.org y ejecuta 'pip install python-vlc'.\n\nDetalle: %s" % e
            ) from e
        self.vlc = vlc
        args = [
            "--no-video",
            "--quiet",
            "--intf=dummy",
            "--no-media-library",
            f"--network-caching={int(network_caching_ms)}",
            "--http-reconnect",
        ]
        self.instance = vlc.Instance(args)
        if self.instance is None:
            raise PlayerUnavailable("libVLC no se pudo inicializar.")
        self.mp = self.instance.media_player_new()
        self._rate = 1.0
        self.url = ""

    # -- control -------------------------------------------------------
    def play(self, url: str, start: float = 0.0) -> None:
        self.url = url
        media = self.instance.media_new(url)
        if start and start > 1:
            media.add_option(f":start-time={start:.1f}")
        self.mp.set_media(media)
        media.release()
        self.mp.play()
        self.mp.set_rate(self._rate)

    def toggle(self) -> None:
        if self.mp.is_playing():
            self.mp.set_pause(1)
        else:
            if self.state() in ("ended", "stopped", "error") and self.url:
                self.play(self.url)
            else:
                self.mp.set_pause(0)

    def stop(self) -> None:
        self.mp.stop()

    def seek(self, seconds: float) -> None:
        length = self.length()
        if length:
            seconds = min(max(0.0, seconds), max(0.0, length - 1))
        seconds = max(0.0, seconds)
        state = self.state()
        if state in ("ended", "stopped", "error", "idle") or not self.is_seekable():
            # Sin posibilidad de saltar en caliente: se vuelve a abrir el audio en ese punto
            if self.url:
                was_paused = state == "paused"
                self.play(self.url, seconds)
                if was_paused:
                    self.mp.set_pause(1)
            return
        self.mp.set_time(int(seconds * 1000))

    def skip(self, delta: float) -> None:
        self.seek(self.time() + delta)

    def set_volume(self, vol: int) -> None:
        self.mp.audio_set_volume(int(vol))

    def set_rate(self, rate: float) -> None:
        self._rate = float(rate)
        self.mp.set_rate(self._rate)

    # -- estado --------------------------------------------------------
    def time(self) -> float:
        t = self.mp.get_time()
        return t / 1000.0 if t and t > 0 else 0.0

    def length(self) -> float:
        n = self.mp.get_length()
        return n / 1000.0 if n and n > 0 else 0.0

    def is_playing(self) -> bool:
        return bool(self.mp.is_playing())

    def is_seekable(self) -> bool:
        return bool(self.mp.is_seekable())

    def state(self) -> str:
        st = self.mp.get_state()
        names = {
            self.vlc.State.NothingSpecial: "idle",
            self.vlc.State.Opening: "opening",
            self.vlc.State.Buffering: "buffering",
            self.vlc.State.Playing: "playing",
            self.vlc.State.Paused: "paused",
            self.vlc.State.Stopped: "stopped",
            self.vlc.State.Ended: "ended",
            self.vlc.State.Error: "error",
        }
        return names.get(st, "idle")

    def release(self) -> None:
        try:
            self.mp.stop()
            self.mp.release()
            self.instance.release()
        except Exception:
            pass
