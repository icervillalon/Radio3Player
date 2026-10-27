"""Persistencia local: progreso de escucha, favoritos, caché y ajustes."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        d = base / "Radio3Player"
    else:
        d = Path.home() / ".radio3player"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


DEFAULT_LIVE = [
    "https://rtvelivestream.akamaized.net/rtvesec/rne/rne_r3_main.m3u8",
    "https://rtvelivestream.rtve.es/rtvesec/rne/rne_r3_main.m3u8",
]


class Store:
    # A partir de aquí el episodio cuenta como escuchado
    DONE_MARGIN = 45  # segundos antes del final
    MIN_RESUME = 15  # no reanudar si solo se escuchó un poco

    def __init__(self, directory: Path | None = None):
        self.dir = directory or data_dir()
        self.state_path = self.dir / "estado.json"
        self.config_path = self.dir / "config.json"
        self.cache_dir = self.dir / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.state = _read(self.state_path, {})
        self.state.setdefault("progress", {})
        self.state.setdefault("favorites", [])
        self.state.setdefault("custom_programs", [])
        self.state.setdefault("known_programs", {})
        self.state.setdefault("volume", 80)
        self.state.setdefault("rate", 1.0)
        self.config = _read(self.config_path, {})
        changed = False
        if "live_urls" not in self.config:
            self.config["live_urls"] = DEFAULT_LIVE
            changed = True
        if "network_caching_ms" not in self.config:
            self.config["network_caching_ms"] = 3000
            changed = True
        if changed:
            try:
                _atomic_write(self.config_path, self.config)
            except OSError:
                pass
        self._dirty = False

    # -- guardado ------------------------------------------------------
    def save(self, force: bool = False) -> None:
        if self._dirty or force:
            _atomic_write(self.state_path, self.state)
            self._dirty = False

    def mark_dirty(self):
        self._dirty = True

    # -- progreso ------------------------------------------------------
    def progress(self, ep_id: str) -> dict:
        return self.state["progress"].get(ep_id, {})

    def set_progress(self, ep, pos: float, dur: float) -> None:
        rec = self.state["progress"].setdefault(ep.id, {})
        dur = dur or rec.get("dur") or ep.duration or 0
        rec.update(
            pos=round(max(0.0, pos), 1),
            dur=round(dur, 1),
            updated=time.time(),
            title=ep.title,
            program_slug=ep.program_slug,
            program_title=ep.program_title,
            date=ep.date,
            audio_url=ep.audio_url,
            web_url=ep.web_url,
            description=ep.description[:4000],
        )
        if dur and pos >= dur - self.DONE_MARGIN:
            rec["done"] = True
        self.mark_dirty()

    def set_done(self, ep, done: bool) -> None:
        rec = self.state["progress"].setdefault(ep.id, {})
        rec["done"] = done
        rec["updated"] = time.time()
        rec.setdefault("title", ep.title)
        rec.setdefault("program_slug", ep.program_slug)
        rec.setdefault("program_title", ep.program_title)
        rec.setdefault("date", ep.date)
        rec.setdefault("audio_url", ep.audio_url)
        rec.setdefault("web_url", ep.web_url)
        rec.setdefault("description", ep.description[:4000])
        if not done:
            rec["pos"] = 0
        self.mark_dirty()

    def resume_position(self, ep_id: str) -> float:
        rec = self.progress(ep_id)
        if not rec or rec.get("done"):
            return 0.0
        pos = rec.get("pos", 0) or 0
        return pos if pos >= self.MIN_RESUME else 0.0

    def in_progress(self, limit: int = 60) -> list[tuple[str, dict]]:
        items = [
            (eid, r)
            for eid, r in self.state["progress"].items()
            if not r.get("done") and (r.get("pos") or 0) >= self.MIN_RESUME
        ]
        items.sort(key=lambda kv: kv[1].get("updated", 0), reverse=True)
        return items[:limit]

    # -- favoritos y programas añadidos a mano -------------------------
    def is_favorite(self, slug: str) -> bool:
        return slug in self.state["favorites"]

    def toggle_favorite(self, slug: str) -> bool:
        favs = self.state["favorites"]
        if slug in favs:
            favs.remove(slug)
            res = False
        else:
            favs.append(slug)
            res = True
        self.mark_dirty()
        return res

    def add_custom_program(self, prog_dict: dict) -> None:
        cps = self.state["custom_programs"]
        if not any(p.get("slug") == prog_dict.get("slug") for p in cps):
            cps.append(prog_dict)
            self.mark_dirty()

    def remember_program(self, prog_dict: dict) -> None:
        """Guarda id/descripcion resueltos para no volver a buscarlos."""
        slug = prog_dict.get("slug")
        if slug:
            self.state["known_programs"][slug] = prog_dict
            self.mark_dirty()

    def known_program(self, slug: str) -> dict | None:
        return self.state["known_programs"].get(slug)

    # -- caché con caducidad -------------------------------------------
    def cache_get(self, key: str, max_age: float):
        path = self.cache_dir / f"{key}.json"
        try:
            if time.time() - path.stat().st_mtime > max_age:
                return None
        except OSError:
            return None
        return _read(path, None)

    def cache_set(self, key: str, data) -> None:
        try:
            _atomic_write(self.cache_dir / f"{key}.json", data)
        except OSError:
            pass
