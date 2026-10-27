"""Radio 3 Player — reproductor ligero de escritorio para los programas de Radio 3.

Ejecutar:  pythonw radio3.py   (o doble clic en Radio3.bat)
"""

from __future__ import annotations

import queue
import re
import sys
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox, simpledialog, ttk

import rtve
from player import Player, PlayerUnavailable
from rtve import Episode, Program
from storage import Store
import wintegration as wi

APP_NAME = "Radio 3"
PROGRAMS_TTL = 24 * 3600  # caché del listado de programas
EPISODES_TTL = 30 * 60  # caché de la primera página de episodios
CONTINUE = "__continue__"

# Paleta
BG = "#141417"
PANEL = "#1c1c21"
PANEL2 = "#24242b"
FG = "#ece9e4"
MUTED = "#9b98a3"
ACCENT = "#ff5a36"
SEL = "#3a2a27"
DONE = "#6f6c75"


def parse_goto(txt: str) -> float | None:
    """'45' -> 45 min · '12:30' -> 12 min 30 s · '1:05:00' -> 1 h 5 min · '1h20m', '90m', '30s'."""
    s = txt.strip().lower().replace(",", ".").replace(" ", "")
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        if len(parts) > 3 or not all(p.replace(".", "", 1).isdigit() for p in parts):
            return None
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return total
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?:in)?)?(?:(\d+(?:\.\d+)?)s)?", s)
    if m and any(m.groups()):
        h, mi, se = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + se
    try:
        return float(s) * 60  # número suelto = minutos
    except ValueError:
        return None


def fmt_time(s: float) -> str:
    s = int(max(0, s or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class SlideBar(tk.Canvas):
    """Barra de progreso/volumen: clic = ir a ese punto, arrastrar, y al pasar el
    ratón muestra a qué tiempo saltarías."""

    def __init__(self, parent, *, width=200, height=28, on_drag=None, on_release=None,
                 hover_text=None, enabled=None, bg=PANEL):
        super().__init__(parent, width=width, height=height, bg=bg, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.on_drag_cb = on_drag
        self.on_release_cb = on_release
        self.hover_text = hover_text
        self.enabled = enabled or (lambda: True)
        self.value = 0.0
        self.dragging = False
        self.hover_x: int | None = None
        self.knob = 7
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Enter>", self._motion)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    # geometría
    def _span(self):
        w = max(self.winfo_width(), int(self.cget("width")))
        return self.knob + 1, w - self.knob - 1

    def _frac(self, x: int) -> float:
        a, b = self._span()
        return 0.0 if b <= a else min(1.0, max(0.0, (x - a) / (b - a)))

    def set(self, frac: float, force: bool = False):
        if self.dragging and not force:
            return
        frac = min(1.0, max(0.0, frac or 0.0))
        if abs(frac - self.value) > 1e-4:
            self.value = frac
            self.redraw()

    # eventos
    def _motion(self, e):
        self.hover_x = e.x
        self.redraw()

    def _leave(self, _):
        self.hover_x = None
        if not self.dragging:
            self.redraw()

    def _press(self, e):
        if not self.enabled():
            return
        self.dragging = True
        self._drag(e)

    def _drag(self, e):
        if not self.dragging:
            return
        self.hover_x = e.x
        self.value = self._frac(e.x)
        self.redraw()
        if self.on_drag_cb:
            self.on_drag_cb(self.value)

    def _release(self, e):
        if not self.dragging:
            return
        self.dragging = False
        self.value = self._frac(e.x)
        self.redraw()
        if self.on_release_cb:
            self.on_release_cb(self.value)

    def redraw(self):
        self.delete("all")
        a, b = self._span()
        h = max(self.winfo_height(), int(self.cget("height")))
        y = h - self.knob - 3 if self.hover_text else h // 2
        active = self.enabled()
        self.create_rectangle(a, y - 2, b, y + 2, fill=PANEL2, outline="")
        x = a + (b - a) * self.value
        if active:
            self.create_rectangle(a, y - 2, x, y + 2, fill=ACCENT, outline="")
            r = self.knob if (self.hover_x is not None or self.dragging) else self.knob - 2
            self.create_oval(x - r, y - r, x + r, y + r, fill="#ffffff" if self.dragging else ACCENT,
                             outline="")
        if self.hover_text and self.hover_x is not None and active:
            hx = min(max(self.hover_x, a), b)
            txt = self.hover_text(self._frac(hx))
            if txt:
                self.create_line(hx, y - 5, hx, y + 5, fill=FG)
                anchor = "s" if a + 24 < hx < b - 24 else ("sw" if hx <= a + 24 else "se")
                self.create_text(hx, y - 7, text=txt, fill=FG, anchor=anchor, font=("Segoe UI", 8)
                                 if sys.platform == "win32" else ("DejaVu Sans", 8))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.store = Store()
        wi.set_log_path(self.store.dir / "app.log")
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.results: queue.Queue = queue.Queue()
        self.ui_cmds: queue.Queue = queue.Queue()  # órdenes de teclas multimedia / barra de tareas
        self.media = None
        self.thumbbar = None
        self._last_timeline = 0.0

        self.programs: list[Program] = []
        self.prog_by_slug: dict[str, Program] = {}
        self.current_prog: Program | None = None
        self.episodes: list[Episode] = []
        self.ep_by_id: dict[str, Episode] = {}
        self.page = 1
        self.total_pages = 1
        self.request_token = 0

        self.now: Episode | None = None
        self.live = False
        self.live_idx = 0
        self.live_started = 0.0
        self.seek_target = 0.0
        self.seek_hold_until = 0.0
        self.last_saved = 0.0
        self.last_store_flush = time.time()
        self.ended_handled = False
        self._show_program = False

        self.player: Player | None = None
        self.player_error = ""
        try:
            self.player = Player(self.store.config.get("network_caching_ms", 3000))
            self.player.set_volume(self.store.state.get("volume", 80))
            self.player.set_rate(self.store.state.get("rate", 1.0))
        except PlayerUnavailable as e:
            self.player_error = str(e)

        self._style()
        self._build()
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._set_window_icon()
        self.media = wi.MediaControls(self._emit)
        self.root.after(300, self._setup_thumbbar)

        self.load_programs()
        self.root.after(100, self._poll_results)
        self.root.after(500, self._tick)
        if self.player_error:
            self.root.after(400, lambda: messagebox.showwarning(APP_NAME, self.player_error))

    # ------------------------------------------------------------------
    # Estilo e interfaz
    # ------------------------------------------------------------------
    def _style(self):
        r = self.root
        r.title(APP_NAME)
        r.geometry("1180x740")
        r.minsize(860, 520)
        r.configure(bg=BG)
        base = ("Segoe UI", 10) if sys.platform == "win32" else ("DejaVu Sans", 10)
        self.f_base = base
        self.f_small = (base[0], 9)
        self.f_title = (base[0], 17, "bold")
        self.f_bold = (base[0], 10, "bold")
        r.option_add("*Font", base)

        st = ttk.Style(r)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, bordercolor=PANEL2,
                     lightcolor=PANEL2, darkcolor=PANEL2, troughcolor=PANEL2, font=base)
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Panel.TLabel", background=PANEL, foreground=FG)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=self.f_small)
        st.configure("PMuted.TLabel", background=PANEL, foreground=MUTED, font=self.f_small)
        st.configure("Title.TLabel", background=BG, foreground=FG, font=self.f_title)
        st.configure("Now.TLabel", background=PANEL, foreground=FG, font=self.f_bold)
        st.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0, padding=(10, 5),
                     focuscolor=PANEL2)
        st.map("TButton", background=[("active", "#33333c"), ("disabled", PANEL)],
               foreground=[("disabled", DONE)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", font=self.f_bold)
        st.map("Accent.TButton", background=[("active", "#ff7455")])
        st.configure("Play.TButton", background=ACCENT, foreground="#ffffff", font=(base[0], 12, "bold"),
                     padding=(14, 4))
        st.map("Play.TButton", background=[("active", "#ff7455")])
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=26,
                     borderwidth=0)
        st.map("Treeview", background=[("selected", SEL)], foreground=[("selected", "#ffffff")])
        st.configure("Treeview.Heading", background=PANEL2, foreground=MUTED, relief="flat",
                     font=self.f_small, padding=(6, 4))
        st.map("Treeview.Heading", background=[("active", PANEL2)])
        st.configure("TEntry", fieldbackground=PANEL2, foreground=FG, insertcolor=FG, padding=6)
        st.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=FG,
                     arrowcolor=FG, padding=3)
        st.map("TCombobox", fieldbackground=[("readonly", PANEL2)], foreground=[("readonly", FG)])
        r.option_add("*TCombobox*Listbox.background", PANEL2)
        r.option_add("*TCombobox*Listbox.foreground", FG)
        st.configure("Horizontal.TScale", background=ACCENT, troughcolor=PANEL2, bordercolor=PANEL2,
                     lightcolor=PANEL2, darkcolor=PANEL2, sliderlength=14, borderwidth=0)
        st.map("Horizontal.TScale", background=[("active", "#ff7455")])
        st.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=BG, arrowcolor=MUTED,
                     bordercolor=BG, lightcolor=PANEL2, darkcolor=PANEL2, gripcount=0, borderwidth=0,
                     arrowsize=12)
        st.map("Vertical.TScrollbar", background=[("active", "#3a3a44")])
        st.configure("TPanedwindow", background=BG)
        st.configure("Sash", sashthickness=6, background=BG)

    def _text(self, parent, height, bg=BG):
        t = tk.Text(parent, height=height, wrap="word", bg=bg, fg=FG, relief="flat", bd=0,
                    highlightthickness=0, padx=2, pady=4, cursor="arrow", insertbackground=FG,
                    selectbackground=SEL, font=self.f_base)
        t.tag_configure("h", font=(self.f_base[0], 12, "bold"))
        t.tag_configure("muted", foreground=MUTED, font=self.f_small)
        t.configure(state="disabled")
        return t

    def _set_text(self, widget: tk.Text, parts):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for text, tag in parts:
            widget.insert("end", text, tag)
        widget.configure(state="disabled")

    def _build(self):
        r = self.root
        outer = ttk.Frame(r)
        outer.pack(fill="both", expand=True)

        # -- barra inferior del reproductor --------------------------------
        bar = ttk.Frame(outer, style="Panel.TFrame", padding=(14, 10))
        bar.pack(side="bottom", fill="x")
        self._build_player_bar(bar)

        self.status = ttk.Label(outer, text="", style="Muted.TLabel", anchor="w", padding=(12, 2))
        self.status.pack(side="bottom", fill="x")

        paned = ttk.PanedWindow(outer, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        # -- columna izquierda: directo + programas ------------------------
        left = ttk.Frame(paned, style="Panel.TFrame", padding=10)
        paned.add(left, weight=0)
        ttk.Button(left, text="●  Radio 3 en directo", style="Accent.TButton",
                   command=self.play_live).pack(fill="x")
        ttk.Button(left, text="⏵  Continuar escuchando", command=self.show_continue).pack(fill="x", pady=(6, 10))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh_program_tree())
        se = ttk.Entry(left, textvariable=self.search_var)
        se.pack(fill="x")
        self.search_entry = se
        self._placeholder(se, self.search_var, "Buscar programa…")

        tf = ttk.Frame(left, style="Panel.TFrame")
        tf.pack(fill="both", expand=True, pady=(8, 6))
        self.prog_tree = ttk.Treeview(tf, show="tree", selectmode="browse")
        self.prog_tree.column("#0", width=250)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.prog_tree.yview)
        self.prog_tree.configure(yscrollcommand=sb.set)
        self.prog_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.prog_tree.tag_configure("fav", foreground=ACCENT)
        self.prog_tree.tag_configure("section", foreground=MUTED, font=self.f_small)
        self.prog_tree.bind("<<TreeviewSelect>>", self.on_program_select)
        self.prog_tree.bind("<Button-3>", self.on_program_menu)

        lb = ttk.Frame(left, style="Panel.TFrame")
        lb.pack(fill="x")
        ttk.Button(lb, text="+ Añadir por URL", command=self.add_by_url).pack(side="left")
        ttk.Button(lb, text="↻", width=3, command=lambda: self.load_programs(force=True)).pack(side="right")

        # -- columna derecha: programa, episodios, detalle ------------------
        right = ttk.Frame(paned, padding=(14, 4, 4, 4))
        paned.add(right, weight=1)

        head = ttk.Frame(right)
        head.pack(fill="x")
        self.prog_title = ttk.Label(head, text="Elige un programa", style="Title.TLabel")
        self.prog_title.pack(side="left", anchor="w")
        self.web_btn = ttk.Button(head, text="Web", command=self.open_program_web, state="disabled")
        self.web_btn.pack(side="right")
        self.fav_btn = ttk.Button(head, text="☆ Favorito", command=self.toggle_fav_current, state="disabled")
        self.fav_btn.pack(side="right", padx=6)

        self.prog_desc = self._text(right, 4)
        self.prog_desc.pack(fill="x", pady=(4, 8))

        vpaned = ttk.PanedWindow(right, orient="vertical")
        vpaned.pack(fill="both", expand=True)

        epf = ttk.Frame(vpaned)
        vpaned.add(epf, weight=3)
        cols = ("estado", "titulo", "fecha", "dur")
        self.ep_tree = ttk.Treeview(epf, columns=cols, show="headings", selectmode="browse")
        for c, label, w, anchor, stretch in (
            ("estado", "", 74, "center", False),
            ("titulo", "Episodio", 420, "w", True),
            ("fecha", "Fecha", 92, "center", False),
            ("dur", "Duración", 80, "e", False),
        ):
            self.ep_tree.heading(c, text=label, anchor=anchor)
            self.ep_tree.column(c, width=w, anchor=anchor, stretch=stretch)
        esb = ttk.Scrollbar(epf, orient="vertical", command=self.ep_tree.yview)
        self.ep_tree.configure(yscrollcommand=esb.set)
        self.more_btn = ttk.Button(epf, text="Cargar más episodios", command=self.load_more)
        epf.columnconfigure(0, weight=1)
        epf.rowconfigure(0, weight=1)
        self.ep_tree.grid(row=0, column=0, sticky="nsew")
        esb.grid(row=0, column=1, sticky="ns")
        self.more_btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.ep_tree.tag_configure("done", foreground=DONE)
        self.ep_tree.tag_configure("playing", foreground=ACCENT)
        self.ep_tree.bind("<<TreeviewSelect>>", self.on_episode_select)
        self.ep_tree.bind("<Double-1>", lambda e: self.play_selected())
        self.ep_tree.bind("<Return>", lambda e: self.play_selected())
        self.ep_tree.bind("<Button-3>", self.on_episode_menu)
        self.more_btn.grid_remove()

        detf = ttk.Frame(vpaned, padding=(0, 8, 0, 0))
        vpaned.add(detf, weight=2)
        self.ep_desc = self._text(detf, 6)
        dsb = ttk.Scrollbar(detf, orient="vertical", command=self.ep_desc.yview)
        self.ep_desc.configure(yscrollcommand=dsb.set)
        self.ep_desc.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")

    def _build_player_bar(self, bar):
        # Fila 1: barra de progreso a todo el ancho (más precisión al elegir el minuto)
        row1 = ttk.Frame(bar, style="Panel.TFrame")
        row1.pack(fill="x")
        self.pos_label = ttk.Label(row1, text="0:00", style="PMuted.TLabel", width=8, anchor="e",
                                   cursor="hand2")
        self.pos_label.pack(side="left")
        self.pos_label.bind("<Button-1>", lambda e: self.ask_goto())  # clic = escribir el minuto
        self.seek = SlideBar(row1, height=30, on_drag=self._seek_preview, on_release=self._seek_commit,
                             hover_text=self._seek_hover_text, enabled=lambda: bool(self._current_length()))
        self.seek.pack(side="left", fill="x", expand=True, padx=8)
        self.seek.bind("<MouseWheel>", lambda e: self.skip(30 if e.delta > 0 else -15))
        self.seek.bind("<Button-4>", lambda e: self.skip(30))
        self.seek.bind("<Button-5>", lambda e: self.skip(-15))
        self.len_label = ttk.Label(row1, text="0:00", style="PMuted.TLabel", width=8, anchor="w")
        self.len_label.pack(side="left")

        # Fila 2: qué suena · controles · velocidad y volumen
        row2 = ttk.Frame(bar, style="Panel.TFrame")
        row2.pack(fill="x", pady=(4, 0))
        info = ttk.Frame(row2, style="Panel.TFrame")
        info.pack(side="left", fill="y")
        self.now_prog = ttk.Label(info, text="", style="PMuted.TLabel", width=44)
        self.now_prog.pack(anchor="w")
        self.now_title = ttk.Label(info, text="Nada sonando", style="Now.TLabel", width=40)
        self.now_title.pack(anchor="w")

        right = ttk.Frame(row2, style="Panel.TFrame")
        right.pack(side="right")
        ttk.Label(right, text="Vel.", style="PMuted.TLabel").pack(side="left")
        self.rate_var = tk.StringVar(value=f"{self.store.state.get('rate', 1.0):g}x")
        rate = ttk.Combobox(right, textvariable=self.rate_var, width=6, state="readonly",
                            values=["0.75x", "1x", "1.25x", "1.5x", "1.75x", "2x"])
        rate.pack(side="left", padx=(4, 14))
        rate.bind("<<ComboboxSelected>>", self.on_rate)
        ttk.Label(right, text="Vol.", style="PMuted.TLabel").pack(side="left")
        self.vol = SlideBar(right, width=110, height=24, on_drag=self.on_volume, on_release=self.on_volume)
        self.vol.pack(side="left", padx=(4, 0))
        self.vol.set(self.store.state.get("volume", 80) / 100)

        ctr = ttk.Frame(row2, style="Panel.TFrame")
        ctr.pack(side="top", pady=2)  # centrado entre ambos lados
        ttk.Button(ctr, text="−15s", command=lambda: self.skip(-15)).pack(side="left")
        self.play_btn = ttk.Button(ctr, text="▶", style="Play.TButton", width=3, command=self.toggle)
        self.play_btn.pack(side="left", padx=6)
        ttk.Button(ctr, text="+30s", command=lambda: self.skip(30)).pack(side="left")

    def _placeholder(self, entry, var, text):
        def show(*_):
            if not var.get():
                entry.configure(foreground=MUTED)
                self._ph_active = True
                var.set(text)

        def hide(*_):
            if getattr(self, "_ph_active", False):
                self._ph_active = False
                var.set("")
                entry.configure(foreground=FG)

        self._ph_text = text
        entry.bind("<FocusIn>", hide)
        entry.bind("<FocusOut>", show)
        show()

    def _search_text(self) -> str:
        if getattr(self, "_ph_active", False):
            return ""
        return self.search_var.get().strip().lower()

    def _bind_keys(self):
        def not_typing():
            return not isinstance(self.root.focus_get(), (ttk.Entry, tk.Entry, ttk.Combobox))

        self.root.bind("<space>", lambda e: (self.toggle(), "break")[1] if not_typing() else None)
        self.root.bind("<Control-Right>", lambda e: self.skip(30))
        self.root.bind("<Control-Left>", lambda e: self.skip(-15))
        self.root.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        self.root.bind("<Control-g>", lambda e: self.ask_goto())

    def set_status(self, text: str):
        self.status.configure(text=text)

    # ------------------------------------------------------------------
    # Tareas en segundo plano (la red nunca bloquea la interfaz)
    # ------------------------------------------------------------------
    def bg(self, fn, on_ok=None, on_err=None):
        fut = self.pool.submit(fn)
        fut.add_done_callback(lambda f: self.results.put((f, on_ok, on_err)))

    def _poll_results(self):
        try:
            while True:
                fut, ok, err = self.results.get_nowait()
                exc = fut.exception()
                try:
                    if exc is None:
                        if ok:
                            ok(fut.result())
                    elif err:
                        err(exc)
                    else:
                        self.set_status(f"Error: {exc}")
                except Exception as e:  # nunca romper el bucle
                    self.set_status(f"Error: {e}")
        except queue.Empty:
            pass
        try:
            while True:
                cmd, arg = self.ui_cmds.get_nowait()
                try:
                    self.on_media_cmd(cmd, arg)
                except Exception as e:
                    wi.log(f"orden {cmd}: {e}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_results)

    # ------------------------------------------------------------------
    # Programas
    # ------------------------------------------------------------------
    def load_programs(self, force: bool = False):
        cached = None if force else self.store.cache_get("programas", PROGRAMS_TTL)
        if cached:
            self._set_programs([Program.from_dict(d) for d in cached])
            return
        self.set_status("Cargando programas de Radio 3…")

        def work():
            return rtve.fetch_programs()

        def ok(progs):
            self.store.cache_set("programas", [p.to_dict() for p in progs])
            self._set_programs(progs)
            self.set_status(f"{len(progs)} programas")

        def err(e):
            stale = self.store.cache_get("programas", 10 ** 9)
            if stale:
                self._set_programs([Program.from_dict(d) for d in stale])
            else:
                self._set_programs([])
            self.set_status(f"No se pudo cargar el listado: {e}. Puedes añadir programas con '+ Añadir por URL'.")

        self.bg(work, ok, err)

    def _set_programs(self, progs: list[Program]):
        by = {p.slug: p for p in progs}
        for d in self.store.state["custom_programs"]:
            p = Program.from_dict(d)
            by.setdefault(p.slug, p)
        for slug, p in by.items():
            known = self.store.known_program(slug)
            if known:
                p.id = p.id or known.get("id")
                p.description = p.description or known.get("description", "")
                p.image = p.image or known.get("image", "")
                if known.get("title") and p.title == slug.replace("-", " ").title():
                    p.title = known["title"]
            # Limpia también títulos guardados por versiones anteriores ("la página de 'X'")
            p.title = rtve.clean_title(p.title, slug)
        self.programs = sorted(by.values(), key=lambda p: p.title.lower())
        self.prog_by_slug = by
        self.refresh_program_tree()

    def refresh_program_tree(self):
        if not hasattr(self, "prog_tree"):
            return
        q = self._search_text()
        tree = self.prog_tree
        sel = tree.selection()
        tree.delete(*tree.get_children())
        favs = [p for p in self.programs if self.store.is_favorite(p.slug)]
        rest = [p for p in self.programs if not self.store.is_favorite(p.slug)]
        if q:
            favs = [p for p in favs if q in p.title.lower()]
            rest = [p for p in rest if q in p.title.lower()]
        if favs:
            tree.insert("", "end", iid="__h_fav", text="FAVORITOS", tags=("section",))
            for p in favs:
                tree.insert("", "end", iid=p.slug, text=f"★  {p.title}", tags=("fav",))
            tree.insert("", "end", iid="__h_all", text="TODOS", tags=("section",))
        for p in rest:
            tree.insert("", "end", iid=p.slug, text=f"     {p.title}" if favs else p.title)
        if sel and tree.exists(sel[0]):
            tree.selection_set(sel[0])

    def on_program_select(self, _=None):
        sel = self.prog_tree.selection()
        if not sel or sel[0].startswith("__h_"):
            return
        prog = self.prog_by_slug.get(sel[0])
        if prog and prog is not self.current_prog:
            self.open_program(prog)

    def open_program(self, prog: Program, force: bool = False):
        self.current_prog = prog
        self.request_token += 1
        token = self.request_token
        self.page, self.total_pages = 1, 1
        self.prog_title.configure(text=prog.title)
        self.fav_btn.configure(state="normal",
                               text="★ Favorito" if self.store.is_favorite(prog.slug) else "☆ Favorito")
        self.web_btn.configure(state="normal")
        self._set_text(self.prog_desc, [(prog.description or "Cargando…", "muted" if not prog.description else None)])
        self._fill_episodes([])
        self._set_text(self.ep_desc, [])
        self.set_status(f"Cargando episodios de {prog.title}…")

        key = f"ep_{prog.slug}_p1"
        cached = None if force else self.store.cache_get(key, EPISODES_TTL)

        def work():
            if not prog.id or not prog.description:
                try:
                    rtve.resolve_program(prog)
                except rtve.RTVEError:
                    if not prog.id:
                        raise
            if cached:
                return [Episode.from_dict(d) for d in cached["eps"]], cached["total"], "caché"
            return rtve.fetch_episodes(prog, 1)

        def ok(res):
            eps, total, src = res
            self.store.remember_program(prog.to_dict())
            if not cached:
                self.store.cache_set(key, {"eps": [e.to_dict() for e in eps], "total": total})
            if token != self.request_token:
                return
            self.prog_title.configure(text=prog.title)
            self._set_text(self.prog_desc, [(prog.description or "Sin descripción.", None)])
            self.total_pages = total
            self._fill_episodes(eps)
            self.set_status(f"{prog.title}: {len(eps)} episodios")

        def err(e):
            if token != self.request_token:
                return
            self._set_text(self.prog_desc, [(prog.description or "", None)])
            self._set_text(self.ep_desc, [("No se pudieron cargar los episodios.\n\n", "h"), (str(e), "muted")])
            self.set_status(f"Error con {prog.title}")

        self.bg(work, ok, err)

    def load_more(self):
        prog = self.current_prog
        if not prog or self.page >= self.total_pages:
            return
        token = self.request_token
        nxt = self.page + 1
        self.more_btn.configure(state="disabled", text="Cargando…")

        def ok(res):
            eps, total, _ = res
            if token != self.request_token:
                return
            self.page, self.total_pages = nxt, total
            known = set(self.ep_by_id)
            self._fill_episodes(self.episodes + [e for e in eps if e.id not in known], keep_scroll=True)

        def err(e):
            self.more_btn.configure(state="normal", text="Cargar más episodios")
            self.set_status(f"Error: {e}")

        self.bg(lambda: rtve.fetch_episodes(prog, nxt), ok, err)

    def show_continue(self):
        self.current_prog = None
        self.request_token += 1
        self.prog_tree.selection_remove(*self.prog_tree.selection())
        self.prog_title.configure(text="Continuar escuchando")
        self.fav_btn.configure(state="disabled", text="☆ Favorito")
        self.web_btn.configure(state="disabled")
        eps = []
        for eid, r in self.store.in_progress():
            eps.append(Episode(
                id=eid, title=r.get("title", ""), date=r.get("date", 0), duration=r.get("dur", 0),
                description=r.get("description", ""), audio_url=r.get("audio_url", ""),
                web_url=r.get("web_url", ""), program_slug=r.get("program_slug", ""),
                program_title=r.get("program_title", ""),
            ))
        self._set_text(self.prog_desc, [("Episodios que dejaste a medias, del más reciente al más antiguo.", "muted")])
        self.total_pages = self.page = 1
        self._fill_episodes(eps, show_program=True)
        self._set_text(self.ep_desc, [] if eps else [("Aún no hay nada a medias.", "muted")])

    # ------------------------------------------------------------------
    # Episodios
    # ------------------------------------------------------------------
    def _status_cell(self, ep: Episode) -> tuple[str, tuple]:
        rec = self.store.progress(ep.id)
        if rec.get("done"):
            return "✓", ("done",)
        if self.now and self.now.id == ep.id and not self.live:
            return "♪", ("playing",)
        pos = rec.get("pos") or 0
        if pos >= self.store.MIN_RESUME:
            dur = rec.get("dur") or ep.duration
            return (f"◐ {int(100 * pos / dur)}%" if dur else f"◐ {fmt_time(pos)}"), ()
        return "", ()

    def _row(self, ep: Episode):
        st, tags = self._status_cell(ep)
        title = ep.title
        if self._show_program and ep.program_title:
            title = f"{ep.program_title} · {ep.title}"
        return (st, title, ep.date_str, fmt_time(ep.duration) if ep.duration else ""), tags

    def _fill_episodes(self, eps: list[Episode], keep_scroll: bool = False, show_program: bool = False):
        self._show_program = show_program
        y = self.ep_tree.yview()[0] if keep_scroll else 0
        self.episodes = eps
        self.ep_by_id = {e.id: e for e in eps}
        self.ep_tree.delete(*self.ep_tree.get_children())
        for e in eps:
            if self.ep_tree.exists(e.id):
                continue
            values, tags = self._row(e)
            self.ep_tree.insert("", "end", iid=e.id, values=values, tags=tags)
        self.ep_tree.yview_moveto(y)
        if self.current_prog and self.page < self.total_pages:
            self.more_btn.configure(state="normal", text="Cargar más episodios")
            self.more_btn.grid()
        else:
            self.more_btn.grid_remove()

    def refresh_row(self, ep_id: str):
        ep = self.ep_by_id.get(ep_id)
        if ep and self.ep_tree.exists(ep_id):
            values, tags = self._row(ep)
            self.ep_tree.item(ep_id, values=values, tags=tags)

    def selected_episode(self) -> Episode | None:
        sel = self.ep_tree.selection()
        return self.ep_by_id.get(sel[0]) if sel else None

    def on_episode_select(self, _=None):
        ep = self.selected_episode()
        if not ep:
            return
        meta = []
        if ep.program_title and self._show_program:
            meta.append(ep.program_title)
        if ep.date_str:
            meta.append(ep.date_str)
        if ep.duration:
            meta.append(fmt_time(ep.duration))
        rec = self.store.progress(ep.id)
        if rec.get("done"):
            meta.append("escuchado")
        elif (rec.get("pos") or 0) >= self.store.MIN_RESUME:
            meta.append(f"te quedaste en {fmt_time(rec['pos'])}")
        self._set_text(self.ep_desc, [
            (ep.title + "\n", "h"),
            ("  ·  ".join(meta) + "\n\n", "muted"),
            (ep.description or "Sin descripción.", None),
        ])

    def play_selected(self, from_start: bool = False):
        ep = self.selected_episode()
        if ep:
            self.play_episode(ep, from_start)

    # ------------------------------------------------------------------
    # Reproducción
    # ------------------------------------------------------------------
    def _need_player(self) -> bool:
        if not self.player:
            messagebox.showwarning(APP_NAME, self.player_error or "Reproductor no disponible.")
            return False
        return True

    def play_episode(self, ep: Episode, from_start: bool = False):
        if not self._need_player():
            return
        self._save_current_progress()
        start = 0.0 if from_start else self.store.resume_position(ep.id)
        if from_start and self.store.progress(ep.id).get("done"):
            self.store.set_done(ep, False)
        self.set_status(f"Abriendo {ep.title}…")

        def go(url):
            prev = self.now.id if self.now else None
            self.live = False
            self.now = ep
            self.ended_handled = False
            self.player.play(url, start)
            self.store.set_progress(ep, start, ep.duration)
            self.now_prog.configure(text=ep.program_title or "")
            self.now_title.configure(text=ep.title[:42] + ("…" if len(ep.title) > 42 else ""))
            self.root.title(f"{ep.title} — {APP_NAME}")
            self.set_status(f"Reanudando en {fmt_time(start)}" if start else "")
            self._last_timeline = 0.0
            self._update_media_meta()
            for i in (prev, ep.id):
                if i:
                    self.refresh_row(i)

        if ep.audio_url:
            go(ep.audio_url)
        else:
            self.bg(lambda: rtve.resolve_audio_url(ep), go,
                    lambda e: self.set_status(f"No se pudo reproducir: {e}"))

    def play_live(self):
        if not self._need_player():
            return
        self._save_current_progress()
        prev = self.now.id if self.now else None
        self.now = None
        self.live = True
        self.live_idx = 0
        self._start_live()
        self.now_prog.configure(text="EN DIRECTO")
        self.now_title.configure(text="Radio 3")
        self.root.title(f"Radio 3 en directo — {APP_NAME}")
        self._update_media_meta()
        if prev:
            self.refresh_row(prev)

    def _start_live(self):
        urls = self.store.config.get("live_urls") or []
        if self.live_idx >= len(urls):
            self.live = False
            self.set_status("No se pudo abrir el directo. Revisa 'live_urls' en config.json "
                            f"({self.store.config_path}).")
            return
        self.live_started = time.time()
        self.player.play(urls[self.live_idx])
        self.set_status("Conectando con el directo…")

    def toggle(self):
        if not self.player:
            return
        if not self.now and not self.live:
            ep = self.selected_episode()
            if ep:
                self.play_episode(ep)
            return
        self.player.toggle()
        self._save_current_progress()

    def skip(self, delta: float):
        if self.player and self.now and not self.live:
            base = self.seek_target if time.time() < self.seek_hold_until else self.player.time()
            self.seek_to(base + delta)

    def _current_length(self) -> float:
        if not self.player or not self.now or self.live:
            return 0.0
        return self.player.length() or self.now.duration or 0.0

    def _seek_preview(self, f: float):
        length = self._current_length()
        if length:
            self.pos_label.configure(text=fmt_time(f * length))

    def _seek_commit(self, f: float):
        length = self._current_length()
        if length:
            self.seek_to(f * length)

    def _seek_hover_text(self, f: float) -> str | None:
        length = self._current_length()
        return fmt_time(f * length) if length else None

    def seek_to(self, seconds: float):
        """Salta a una posición y evita que el tick devuelva la barra atrás mientras VLC se recoloca."""
        if not self.player or not self.now or self.live:
            return
        length = self._current_length()
        if length:
            seconds = min(max(0.0, seconds), length - 1)
        self.player.seek(seconds)
        self.ended_handled = False
        self._last_timeline = 0.0
        self.seek_target = seconds
        self.seek_hold_until = time.time() + 2.0
        if length:
            self.seek.set(seconds / length, force=True)
        self.pos_label.configure(text=fmt_time(seconds))
        if self.store.progress(self.now.id).get("done") and length and seconds < length - self.store.DONE_MARGIN:
            self.store.set_done(self.now, False)
        self.store.set_progress(self.now, seconds, length)
        self.last_saved = time.time()
        self.refresh_row(self.now.id)

    def ask_goto(self):
        if not self.now or self.live:
            return
        length = self._current_length()
        txt = simpledialog.askstring(
            APP_NAME,
            "¿A qué punto quieres ir?\n\n"
            "  45        → minuto 45\n"
            "  12:30     → 12 min 30 s\n"
            "  1:05:00   → 1 h 5 min"
            + (f"\n\nDuración: {fmt_time(length)}" if length else ""),
            parent=self.root,
        )
        if not txt:
            return
        secs = parse_goto(txt)
        if secs is None:
            messagebox.showerror(APP_NAME, f"No entiendo «{txt}». Usa 45, 12:30 o 1:05:00.")
            return
        self.seek_to(secs)

    def on_volume(self, f: float):
        v = int(round(f * 100))
        if self.player:
            self.player.set_volume(v)
        self.store.state["volume"] = v
        self.store.mark_dirty()

    def on_rate(self, _=None):
        rate = float(self.rate_var.get().rstrip("x"))
        if self.player:
            self.player.set_rate(rate)
        self.store.state["rate"] = rate
        self.store.mark_dirty()

    def _save_current_progress(self):
        if self.player and self.now and not self.live:
            t = self.player.time()
            if t > 0:
                self.store.set_progress(self.now, t, self.player.length())
                self.last_saved = time.time()

    def _tick(self):
        try:
            self._tick_inner()
            self._sync_system_controls()
        finally:
            self.root.after(500, self._tick)

    def _tick_inner(self):
        p = self.player
        if not p:
            return
        state = p.state()
        playing = state in ("playing", "opening", "buffering")
        self.play_btn.configure(text="❚❚" if playing else "▶")

        if self.live:
            self.pos_label.configure(text="")
            self.len_label.configure(text="")
            self.seek.set(0)
            if state == "error" or (state == "ended" and time.time() - self.live_started < 10):
                self.live_idx += 1
                self._start_live()
            elif state == "playing" and self.status.cget("text").startswith("Conectando"):
                self.set_status("Radio 3 en directo")
            return

        if not self.now:
            return
        t = p.time()
        length = p.length() or self.now.duration
        if length and not self.now.duration:
            self.now.duration = length
        if time.time() < self.seek_hold_until:
            # VLC tarda un instante en recolocarse tras un salto
            if abs(t - self.seek_target) < 3:
                self.seek_hold_until = 0.0
            else:
                t = self.seek_target
        if not self.seek.dragging:
            if length:
                self.seek.set(t / length)
            self.pos_label.configure(text=fmt_time(t))
        self.len_label.configure(text=fmt_time(length) if length else "")

        if state == "playing" and time.time() - self.last_saved > 5:
            self._save_current_progress()
        if state == "ended" and not self.ended_handled:
            self.ended_handled = True
            self.store.set_done(self.now, True)
            self.refresh_row(self.now.id)
            self.set_status("Episodio terminado")
        if state == "error" and not self.ended_handled:
            self.ended_handled = True
            self.set_status("Error al reproducir este audio.")
        if time.time() - self.last_store_flush > 20:
            self.last_store_flush = time.time()
            try:
                self.store.save()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Teclas multimedia, panel de Windows y botones de la barra de tareas
    # ------------------------------------------------------------------
    def _emit(self, cmd: str, arg=None):
        """Llamado desde otros hilos (Windows): solo encola."""
        self.ui_cmds.put((cmd, arg))

    def _set_window_icon(self):
        ico = Path(__file__).with_name("icono.ico")
        try:
            if sys.platform == "win32" and ico.exists():
                self.root.iconbitmap(default=str(ico))
            else:
                size = 32
                rgba = wi.render_rgba("app", size, ss=2)
                img = tk.PhotoImage(width=size, height=size)
                for y in range(size):
                    for x in range(size):
                        r, g, b, a = rgba[(y * size + x) * 4:(y * size + x) * 4 + 4]
                        if a > 96:
                            img.put(f"#{r:02x}{g:02x}{b:02x}", (x, y))
                self._icon_img = img
                self.root.iconphoto(True, img)
        except Exception as e:
            wi.log(f"icono: {e}")

    def _setup_thumbbar(self):
        if sys.platform != "win32":
            return
        self.thumbbar = wi.ThumbBar(self.root, self._emit, self.store.dir / "iconos")
        self._sync_system_controls()

    def on_media_cmd(self, cmd: str, arg=None):
        p = self.player
        if not p:
            return
        state = p.state()
        active = state in ("playing", "opening", "buffering")
        if cmd == "toggle":
            self.toggle()
        elif cmd == "play":
            if not active:
                self.toggle()
        elif cmd == "pause":
            if active:
                self.toggle()
        elif cmd == "stop":
            if self.live:
                p.stop()
            elif active:
                self.toggle()  # en episodios, "stop" = pausa (conserva la posición)
        elif cmd == "next":
            self.skip(30)
        elif cmd == "prev":
            self.skip(-15)
        elif cmd == "seek" and arg is not None:
            self.seek_to(float(arg))
        self.root.after(150, self._sync_system_controls)

    def _update_media_meta(self):
        if not self.media:
            return
        if self.live:
            self.media.set_metadata("Radio 3 en directo", "Radio 3", "RNE", None, live=True)
        elif self.now:
            prog = self.prog_by_slug.get(self.now.program_slug)
            image = prog.image if prog and prog.image else None
            self.media.set_metadata(self.now.title, self.now.program_title or "Radio 3", "Radio 3",
                                    image, live=False)

    def _sync_system_controls(self):
        p = self.player
        if not p:
            return
        state = p.state()
        playing = state in ("playing", "opening", "buffering")
        loaded = bool(self.now or self.live)
        if self.media:
            if not loaded:
                status = "closed"
            elif playing:
                status = "playing"
            elif state == "paused":
                status = "paused"
            else:
                status = "stopped"
            self.media.set_status(status)
            if self.now and not self.live and time.time() - self._last_timeline > 5:
                self._last_timeline = time.time()
                t = self.seek_target if time.time() < self.seek_hold_until else p.time()
                self.media.set_timeline(t, self._current_length())
        if self.thumbbar:
            self.thumbbar.set_state(playing, bool(self.now and not self.live))

    # ------------------------------------------------------------------
    # Menús y acciones varias
    # ------------------------------------------------------------------
    def _menu(self):
        return tk.Menu(self.root, tearoff=0, bg=PANEL2, fg=FG, activebackground=SEL,
                       activeforeground="#fff", bd=0)

    def on_episode_menu(self, event):
        iid = self.ep_tree.identify_row(event.y)
        if not iid:
            return
        self.ep_tree.selection_set(iid)
        ep = self.ep_by_id.get(iid)
        if not ep:
            return
        m = self._menu()
        resume = self.store.resume_position(ep.id)
        m.add_command(label=f"Reanudar ({fmt_time(resume)})" if resume else "Reproducir",
                      command=lambda: self.play_episode(ep))
        m.add_command(label="Reproducir desde el principio", command=lambda: self.play_episode(ep, True))
        m.add_separator()
        if self.store.progress(ep.id).get("done"):
            m.add_command(label="Marcar como no escuchado", command=lambda: self._mark(ep, False))
        else:
            m.add_command(label="Marcar como escuchado", command=lambda: self._mark(ep, True))
        m.add_separator()
        if ep.web_url:
            m.add_command(label="Abrir en rtve.es", command=lambda: webbrowser.open(ep.web_url))
        m.add_command(label="Copiar enlace del audio", command=lambda: self._copy_audio(ep))
        m.tk_popup(event.x_root, event.y_root)

    def _mark(self, ep, done):
        self.store.set_done(ep, done)
        self.refresh_row(ep.id)
        self.on_episode_select()

    def _copy_audio(self, ep):
        def ok(url):
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.set_status("Enlace copiado")

        if ep.audio_url:
            ok(ep.audio_url)
        else:
            self.bg(lambda: rtve.resolve_audio_url(ep), ok)

    def on_program_menu(self, event):
        iid = self.prog_tree.identify_row(event.y)
        prog = self.prog_by_slug.get(iid)
        if not prog:
            return
        self.prog_tree.selection_set(iid)
        m = self._menu()
        fav = self.store.is_favorite(prog.slug)
        m.add_command(label="Quitar de favoritos" if fav else "Añadir a favoritos",
                      command=lambda: self._toggle_fav(prog))
        m.add_command(label="Actualizar episodios", command=lambda: self.open_program(prog, force=True))
        m.add_command(label="Abrir en rtve.es", command=lambda: webbrowser.open(prog.url))
        m.tk_popup(event.x_root, event.y_root)

    def _toggle_fav(self, prog):
        fav = self.store.toggle_favorite(prog.slug)
        self.refresh_program_tree()
        if self.current_prog is prog:
            self.fav_btn.configure(text="★ Favorito" if fav else "☆ Favorito")

    def toggle_fav_current(self):
        if self.current_prog:
            self._toggle_fav(self.current_prog)

    def open_program_web(self):
        if self.current_prog:
            webbrowser.open(self.current_prog.url)

    def add_by_url(self):
        url = simpledialog.askstring(
            APP_NAME, "Pega la URL del programa en rtve.es\n(p. ej. https://www.rtve.es/play/audios/turbo-3/)",
            parent=self.root)
        if not url:
            return
        slug = rtve.slug_from_url(url)
        if not slug:
            messagebox.showerror(APP_NAME, "No reconozco esa URL. Debe ser del tipo\n"
                                           "https://www.rtve.es/play/audios/<programa>/")
            return
        prog = self.prog_by_slug.get(slug) or Program(slug=slug, title=slug.replace("-", " ").title(),
                                                      url=f"{rtve.BASE}/play/audios/{slug}/")
        self.store.add_custom_program(prog.to_dict())
        if slug not in self.prog_by_slug:
            self.programs.append(prog)
            self.programs.sort(key=lambda p: p.title.lower())
            self.prog_by_slug[slug] = prog
        self.refresh_program_tree()
        if self.prog_tree.exists(slug):
            self.prog_tree.selection_set(slug)
            self.prog_tree.see(slug)
        self.open_program(prog)

    def on_close(self):
        try:
            self._save_current_progress()
            self.store.save(force=True)
        except Exception:
            pass
        for integ in (self.thumbbar, self.media):
            if integ:
                try:
                    integ.close()
                except Exception:
                    pass
        if self.player:
            self.player.release()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main():
    if sys.platform == "win32":
        try:  # texto nítido en pantallas con escalado
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Radio3Player")
        except Exception:
            pass
        wi.init_com_sta()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
