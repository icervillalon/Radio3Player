"""Cliente ligero para el catálogo de audio de RTVE (Radio 3).

Solo usa la biblioteca estándar. La API de RTVE no está documentada, así que
cada paso tiene varias estrategias de respaldo:

  * Programas: se leen del listado A-Z de Radio 3 (HTML) siguiendo su paginación.
  * ID del programa: se busca en el HTML de la página del programa.
  * Episodios: API JSON  /api/programas/{id}/audios.json  y, si falla, el
    podcast RSS        /api/programas/{id}/audios.rss
  * URL del audio: se busca cualquier .mp3/.m4a/.m3u8 dentro de la respuesta;
    si no aparece, se consulta /api/audios/{id}.json al reproducir.
"""

from __future__ import annotations

import gzip
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

BASE = "https://www.rtve.es"
AZ_URL = BASE + "/play/radio/a-z/radio-3/"
API_PROGRAM = BASE + "/api/programas/{id}.json"
API_AUDIOS = BASE + "/api/programas/{id}/audios.json?page={page}&size={size}"
API_RSS = BASE + "/api/programas/{id}/audios.rss"
API_AUDIO = BASE + "/api/audios/{id}.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 Radio3Player/1.0"
)

AUDIO_RE = re.compile(r"\.(mp3|m4a|aac|m3u8)(\?|$)", re.I)

# Rutas de /play/audios/<slug>/ que no son programas.
NOT_PROGRAMS = {"", "directo", "podcast", "podcasts", "radio", "a-z", "buscar"}


class RTVEError(Exception):
    pass


# --------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------


@dataclass
class Program:
    slug: str
    title: str
    url: str
    id: str | None = None
    description: str = ""
    image: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Episode:
    id: str
    title: str
    date: float = 0.0  # timestamp
    duration: float = 0.0  # segundos
    description: str = ""
    audio_url: str = ""
    web_url: str = ""
    program_slug: str = ""
    program_title: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def date_str(self) -> str:
        if not self.date:
            return ""
        return datetime.fromtimestamp(self.date).strftime("%d/%m/%Y")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------


def http_get(url: str, timeout: float = 15.0, retries: int = 2) -> bytes:
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "es-ES,es;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    data = gzip.decompress(data)
                elif enc == "deflate":
                    data = zlib.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 404, 410):
                break
        except Exception as e:  # red, timeout...
            last = e
        time.sleep(0.6 * (attempt + 1))
    raise RTVEError(f"No se pudo descargar {url}: {last}")


def get_text(url: str) -> str:
    return http_get(url).decode("utf-8", errors="replace")


def get_json(url: str):
    raw = http_get(url)
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise RTVEError(f"Respuesta no JSON en {url}") from e


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    if "<" not in s:
        return html.unescape(s).strip()
    p = _TextExtractor()
    try:
        p.feed(s)
        txt = "".join(p.parts)
    except Exception:
        txt = re.sub(r"<[^>]+>", " ", s)
    txt = html.unescape(txt)
    txt = re.sub(r"[ \t\r\f\v]+", " ", txt)
    txt = re.sub(r"\n\s*\n\s*\n+", "\n\n", txt)
    return txt.strip()


def find_audio_urls(obj) -> list[str]:
    """Busca recursivamente cualquier URL de audio en un JSON."""
    found: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            if o.startswith("http") and AUDIO_RE.search(o):
                found.append(o)

    walk(obj)
    # mp3 primero (lo más compatible para reanudar), luego el resto.
    found.sort(key=lambda u: (0 if ".mp3" in u.lower() else 1))
    seen, out = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_date(value) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000 if v > 1e11 else v
    s = str(value).strip()
    if s.isdigit():
        return parse_date(int(s))
    for fmt in (
        "%d-%m-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ):
        for cand in (s, s[:19], s[:10]):
            try:
                return datetime.strptime(cand, fmt).timestamp()
            except ValueError:
                pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).timestamp()
    except Exception:
        return 0.0


def parse_duration(value) -> float:
    """Acepta milisegundos, segundos o 'HH:MM:SS'."""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000 if v > 36000 else v  # >10h en segundos => son ms
    s = str(value).strip()
    if ":" in s:
        total = 0.0
        for part in s.split(":"):
            try:
                total = total * 60 + float(part)
            except ValueError:
                return 0.0
        return total
    try:
        return parse_duration(float(s))
    except ValueError:
        return 0.0


def slug_from_url(url: str) -> str | None:
    m = re.search(r"/play/audios/([a-z0-9][a-z0-9-]*)/?(?:[?#].*)?$", url.strip(), re.I)
    return m.group(1).lower() if m else None


def _first(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


# --------------------------------------------------------------------------
# Programas
# --------------------------------------------------------------------------

_A_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
# Respeta el tipo de comilla de apertura: title="la página de 'Turbo 3'" no se corta en el '
_HREF_RE = re.compile(r"""(?<![\w-])href\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
_ATTR_RE = r"""(?<![\w-]){name}\s*=\s*(?:"([^"]*)"|'([^']*)')"""


_QUOTES = "\"'«»“”‘’`"
_OPEN_Q = "\"'«“‘`"
_VERB = r"(?:ir|accede|acceder|entra|entrar|ver|escuchar|escucha|visita|visitar|abrir|abre)\s+(?:a\s+)?"
_PAGE = (
    r"(?:la\s+)?(?:p[aá]gina|ficha|secci[oó]n|portada|web)\s+(?:del?\s+)?(?:programa\s+)?"
    r"|(?:todos\s+los\s+)?(?:audios|podcasts?|episodios)\s+(?:del?\s+)?(?:programa\s+)?"
    r"|(?:el\s+)?programa\s+(?=[\"'«“‘`])"
)
# Solo se quita el verbo si va seguido de "la página de…" o de comillas, para no
# estropear programas que empiecen por "Ver…" o "Escucha…".
_TITLE_PREFIX = re.compile(
    rf"^\s*(?:(?:{_VERB})?(?:{_PAGE})|(?:{_VERB})(?=[\"'«“‘`]))", re.I
)
_TITLE_SUFFIX = re.compile(
    r"\s*(?:(?:[-|–·]|\ben)\s*(?:RTVE|RNE|Radio\s*3)(?:\s*(?:Play|Audio|\.es))?\s*)+$", re.I
)


def is_share_title(title: str | None) -> bool:
    """Texto de los botones de compartir ("Compartir por Email"), no un programa."""
    return bool(re.match(r"\s*compartir\b", title or "", re.I))


def clean_title(title: str | None, slug: str = "") -> str:
    """'Ir a la página de "Turbo 3"' -> 'Turbo 3'."""
    raw = html.unescape(title or "").strip()
    if is_share_title(raw):
        raw = ""
    t = raw
    for _ in range(2):  # p. ej. "Ir a la página del programa 'X'"
        t = _TITLE_PREFIX.sub("", t, count=1).strip()
    if t[:1] in _OPEN_Q:
        # 'Turbo 3' en RTVE Play  ->  Turbo 3
        m = re.match(r"[\"'«“‘`](.+?)[\"'»”’`](?=\s|$|[.,:;)-])", t)
        if m:
            t = m.group(1)
    t = _TITLE_SUFFIX.sub("", t)
    t = t.strip().strip(_QUOTES).strip(" .:-").strip()
    if not t:
        t = slug.replace("-", " ").title() if slug else raw
    return t[:1].upper() + t[1:]


def _attr(attrs: str, name: str) -> str | None:
    m = re.search(_ATTR_RE.format(name=re.escape(name)), attrs, re.I)
    if not m:
        return None
    return html.unescape(m.group(1) if m.group(1) is not None else m.group(2)).strip() or None


def parse_program_links(page_html: str) -> list[Program]:
    progs: dict[str, Program] = {}
    for attrs, inner in _A_RE.findall(page_html):
        hm = _HREF_RE.search(attrs)
        if not hm:
            continue
        href = urllib.parse.urljoin(BASE, html.unescape(hm.group(1) or hm.group(2) or ""))
        # Los botones de compartir (mailto:, facebook, whatsapp…) acaban en la URL
        # del programa pero no son enlaces a él: "Compartir por Email".
        u = urllib.parse.urlparse(href)
        if u.scheme not in ("http", "https") or not (u.hostname or "").endswith("rtve.es"):
            continue
        slug = slug_from_url(href)
        if not slug or slug in NOT_PROGRAMS:
            continue
        title = (
            _attr(attrs, "title")
            or _attr(attrs, "aria-label")
            or html_to_text(inner).split("\n")[0].strip()
        )
        if not title or len(title) > 120:
            title = slug.replace("-", " ").title()
        title = clean_title(title, slug)
        url = f"{BASE}/play/audios/{slug}/"
        cur = progs.get(slug)
        # Nos quedamos con el título más "bonito" (no derivado del slug)
        if cur is None or cur.title == slug.replace("-", " ").title():
            progs[slug] = Program(slug=slug, title=title, url=url)
    return list(progs.values())


def _az_page_links(page_html: str, base_url: str) -> set[str]:
    out = set()
    path = urllib.parse.urlparse(AZ_URL).path
    for a, b in _HREF_RE.findall(page_html):
        href = a or b
        u = urllib.parse.urljoin(base_url, html.unescape(href))
        if urllib.parse.urlparse(u).path.startswith(path):
            out.add(u.split("#")[0])
    return out


def fetch_programs(max_pages: int = 40, progress=None) -> list[Program]:
    """Recorre el listado A-Z de Radio 3 (y su paginación / letras)."""
    pending = [AZ_URL]
    visited: set[str] = set()
    progs: dict[str, Program] = {}
    while pending and len(visited) < max_pages:
        url = pending.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            page = get_text(url)
        except RTVEError:
            if url == AZ_URL:
                raise
            continue
        for p in parse_program_links(page):
            progs.setdefault(p.slug, p)
        for link in sorted(_az_page_links(page, url)):
            if link not in visited and link not in pending:
                pending.append(link)
        if progress:
            progress(len(visited), len(progs))
    if not progs:
        raise RTVEError("El listado A-Z no devolvió programas (¿ha cambiado la web de RTVE?)")
    return sorted(progs.values(), key=lambda p: p.title.lower())


_ID_PATTERNS = [
    r"/api/programas/(\d+)",
    r"""["']?idPrograma["']?\s*[:=]\s*["']?(\d+)""",
    r"""["']?programId["']?\s*[:=]\s*["']?(\d+)""",
    r"""["']?id_programa["']?\s*[:=]\s*["']?(\d+)""",
    r"""data-(?:id-?programa|idprograma|program-?id)\s*=\s*["'](\d+)""",
    r"/programas/(\d+)/audios",
]


def parse_program_page(page_html: str, prog: Program) -> Program:
    if not prog.id:
        for pat in _ID_PATTERNS:
            m = re.search(pat, page_html, re.I)
            if m:
                prog.id = m.group(1)
                break
        if not prog.id:
            # Último recurso: el valor de "id" de un JSON incrustado junto al slug
            m = re.search(
                r'"id"\s*:\s*"?(\d{3,})"?[^{}]{0,400}?' + re.escape(prog.slug), page_html
            )
            if m:
                prog.id = m.group(1)
    for name in ("og:description", "description", "twitter:description"):
        m = re.search(
            r"""<meta[^>]+(?:property|name)\s*=\s*["']%s["'][^>]*content\s*=\s*["']([^"']*)["']"""
            % re.escape(name),
            page_html,
            re.I,
        ) or re.search(
            r"""<meta[^>]+content\s*=\s*["']([^"']*)["'][^>]*(?:property|name)\s*=\s*["']%s["']"""
            % re.escape(name),
            page_html,
            re.I,
        )
        if m and m.group(1).strip():
            prog.description = prog.description or html_to_text(m.group(1))
            break
    m = re.search(r"""<meta[^>]+property\s*=\s*["']og:image["'][^>]*content\s*=\s*["']([^"']+)""", page_html, re.I)
    if m:
        prog.image = prog.image or m.group(1)
    m = re.search(r"""<meta[^>]+property\s*=\s*["']og:title["'][^>]*content\s*=\s*["']([^"']+)""", page_html, re.I)
    if m and (not prog.title or prog.title == prog.slug.replace("-", " ").title()):
        prog.title = clean_title(m.group(1), prog.slug) or prog.title
    return prog


def resolve_program(prog: Program) -> Program:
    """Rellena id, descripción e imagen del programa."""
    if not prog.id:
        page = get_text(prog.url)
        parse_program_page(page, prog)
    if not prog.id:
        raise RTVEError(f"No encuentro el identificador del programa '{prog.title}'.")
    # La API de programa suele tener una descripción más completa
    try:
        data = get_json(API_PROGRAM.format(id=prog.id))
        item = data
        items = (data.get("page") or {}).get("items") if isinstance(data, dict) else None
        if items:
            item = items[0]
        if isinstance(item, dict):
            desc = _first(item, "description", "longDescription", "shortDescription", "promoDesc")
            if desc:
                desc = html_to_text(desc)
                if len(desc) > len(prog.description):
                    prog.description = desc
            title = _first(item, "name", "title")
            if title and prog.title == prog.slug.replace("-", " ").title():
                prog.title = clean_title(title, prog.slug)
            img = _first(item, "imgPoster", "thumbnail", "imgCol", "image")
            if isinstance(img, str):
                prog.image = prog.image or img
    except RTVEError:
        pass
    return prog


# --------------------------------------------------------------------------
# Episodios
# --------------------------------------------------------------------------


def _episode_from_json(item: dict, prog: Program) -> Episode | None:
    eid = str(_first(item, "id", "idAsset", "uid", default="")).strip()
    if not eid:
        return None
    urls = find_audio_urls(item)
    web = _first(item, "htmlUrl", "htmlShortUrl", "url", default="")
    if web and not str(web).startswith("http"):
        web = urllib.parse.urljoin(BASE, web)
    return Episode(
        id=eid,
        title=html_to_text(_first(item, "longTitle", "title", "name", default="(sin título)")),
        date=parse_date(_first(item, "publicationDate", "dateOfEmission", "fechaEmision", "pubDate", "date")),
        duration=parse_duration(_first(item, "duration", "duracion")),
        description=html_to_text(_first(item, "description", "longDescription", "shortDescription", default="")),
        audio_url=urls[0] if urls else "",
        web_url=str(web or ""),
        program_slug=prog.slug,
        program_title=prog.title,
    )


def fetch_episodes_json(prog: Program, page: int = 1, size: int = 50) -> tuple[list[Episode], int]:
    data = get_json(API_AUDIOS.format(id=prog.id, page=page, size=size))
    pg = data.get("page", data) if isinstance(data, dict) else {}
    items = pg.get("items") or []
    total_pages = int(pg.get("totalPages") or 1)
    eps = [e for e in (_episode_from_json(i, prog) for i in items if isinstance(i, dict)) if e]
    return eps, total_pages


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_rss(xml_bytes: bytes, prog: Program) -> list[Episode]:
    root = ET.fromstring(xml_bytes)
    eps: list[Episode] = []
    for it in root.iter():
        if _local(it.tag) != "item":
            continue
        fields: dict[str, str] = {}
        enclosure = ""
        for ch in it:
            name = _local(ch.tag)
            if name == "enclosure":
                enclosure = ch.attrib.get("url", "")
            elif name not in fields:
                fields[name] = (ch.text or "").strip()
        link = fields.get("link", "")
        guid = fields.get("guid", "")
        eid = ""
        for src in (guid, link):
            nums = re.findall(r"\d{4,}", src)
            if nums:
                eid = nums[-1]
                break
        eid = eid or guid or link or enclosure
        if not eid:
            continue
        desc = fields.get("summary") or fields.get("description") or fields.get("encoded") or ""
        eps.append(
            Episode(
                id=str(eid),
                title=html_to_text(fields.get("title", "(sin título)")),
                date=parse_date(fields.get("pubdate")),
                duration=parse_duration(fields.get("duration")),
                description=html_to_text(desc),
                audio_url=enclosure,
                web_url=link,
                program_slug=prog.slug,
                program_title=prog.title,
            )
        )
    return eps


def fetch_episodes_rss(prog: Program) -> list[Episode]:
    return parse_rss(http_get(API_RSS.format(id=prog.id)), prog)


def fetch_episodes(prog: Program, page: int = 1, size: int = 50) -> tuple[list[Episode], int, str]:
    """Devuelve (episodios, total_paginas, fuente)."""
    if not prog.id:
        resolve_program(prog)
    errors = []
    try:
        eps, total = fetch_episodes_json(prog, page, size)
        if eps:
            # Completa URLs de audio que falten con el RSS (solo en la primera página)
            if page == 1 and any(not e.audio_url for e in eps):
                try:
                    by_id = {e.id: e for e in fetch_episodes_rss(prog)}
                    for e in eps:
                        r = by_id.get(e.id)
                        if r and not e.audio_url:
                            e.audio_url = r.audio_url
                except Exception:
                    pass
            return eps, total, "api"
    except Exception as e:
        errors.append(f"API: {e}")
    if page == 1:
        try:
            eps = fetch_episodes_rss(prog)
            if eps:
                eps.sort(key=lambda e: e.date, reverse=True)
                return eps, 1, "rss"
        except Exception as e:
            errors.append(f"RSS: {e}")
    if errors:
        raise RTVEError("; ".join(errors))
    return [], page, "api"


def resolve_audio_url(ep: Episode) -> str:
    if ep.audio_url:
        return ep.audio_url
    try:
        data = get_json(API_AUDIO.format(id=ep.id))
        urls = find_audio_urls(data)
        if urls:
            ep.audio_url = urls[0]
            return ep.audio_url
    except RTVEError:
        pass
    if ep.web_url:
        try:
            urls = [u for u in re.findall(r"https?://[^\s\"'<>]+", get_text(ep.web_url)) if AUDIO_RE.search(u)]
            if urls:
                ep.audio_url = html.unescape(urls[0])
                return ep.audio_url
        except RTVEError:
            pass
    raise RTVEError("No encuentro el archivo de audio de este episodio.")


# --------------------------------------------------------------------------
# Diagnóstico por consola:  python rtve.py  [slug]
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    slug = sys.argv[1] if len(sys.argv) > 1 else "turbo-3"
    print("1) Listado A-Z de Radio 3...")
    try:
        progs = fetch_programs(max_pages=5)
        print(f"   OK: {len(progs)} programas. Ej.: {', '.join(p.title for p in progs[:5])}")
    except Exception as e:
        print("   FALLO:", e)
    prog = Program(slug=slug, title=slug, url=f"{BASE}/play/audios/{slug}/")
    print(f"2) Página del programa {slug}...")
    try:
        resolve_program(prog)
        print(f"   OK: id={prog.id} título={prog.title!r}")
        print(f"   Descripción: {prog.description[:150]!r}")
    except Exception as e:
        print("   FALLO:", e)
        sys.exit(1)
    print("3) Episodios...")
    try:
        eps, total, src = fetch_episodes(prog)
        print(f"   OK ({src}): {len(eps)} episodios, {total} páginas")
        for e in eps[:3]:
            print(f"   - {e.date_str} {e.title[:60]!r} {int(e.duration)}s audio={'sí' if e.audio_url else 'no'}")
        if eps:
            print("4) URL de audio del primero:", resolve_audio_url(eps[0]))
    except Exception as e:
        print("   FALLO:", e)
