import os
import re
import time
import json
import shutil
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import random
import subprocess
import sys
from queue import Queue, Empty
from collections import deque

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, WebDriverException


# =======================
# CONFIG
# =======================
URL = "https://my-ambassador.lifecell.ua"

VALID_FILE = "valid.txt"
REGSOON_FILE = "regsoon.txt"

WAIT_LOGIN_SECONDS = 600
WAIT_UI_SECONDS = 12
POLL = 0.05

DEFAULT_PREFIXES = ["67", "96", "98", "63", "93"]
PROFILES_ROOT = "chrome_profiles"

CONFIG_FILE = "config.json"

# Watchdog
MAX_CONSEC_ERRORS = 12
RESTART_COOLDOWN_SEC = 2.0

# Generator uniqueness window
GEN_WINDOW = 250_000


# =======================
# HELPERS
# =======================
def ensure_file(path: str):
    ap = os.path.abspath(path)
    os.makedirs(os.path.dirname(ap) or ".", exist_ok=True)
    if not os.path.exists(ap):
        with open(ap, "a", encoding="utf-8"):
            pass


def append_lines(path, lines):
    if not lines:
        return
    ensure_file(path)
    with open(path, "a", encoding="utf-8") as f:
        for x in lines:
            f.write(x + "\n")


def open_file_in_default_app(filepath: str):
    filepath = os.path.abspath(filepath)
    ensure_file(filepath)
    try:
        if sys.platform.startswith("win"):
            os.startfile(filepath)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", filepath])
        else:
            subprocess.Popen(["xdg-open", filepath])
    except Exception:
        pass


def parse_prefixes(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,\s;|/]+", raw)
    out = []
    seen = set()
    for p in parts:
        p = re.sub(r"\D+", "", p.strip())
        if len(p) == 2 and p.isdigit():
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def fmt_rate(x):
    try:
        return f"{x:.2f}/s"
    except Exception:
        return "-"


def clamp(n, lo, hi):
    return max(lo, min(hi, n))


def recommended_workers():
    # Без внешних библиотек: берём CPU как ориентир.
    c = os.cpu_count() or 4
    return clamp(c * 2, 2, 16)


def profile_dir(profile_id: int):
    return os.path.abspath(os.path.join(PROFILES_ROOT, f"profile_{profile_id:02d}"))


# =======================
# APP
# =======================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Firk — Multi Chrome Worker")
        self.root.geometry("1280x900")
        self.root.minsize(1160, 780)

        # Runtime
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()   # set = "paused"
        self.threads = []

        # Writer
        self.write_q = Queue()
        self.writer_stop = threading.Event()
        self.writer_thread = None

        # UI queue
        self.q = Queue()

        # Locks + counters
        self.count_lock = threading.Lock()
        self.valid_count = 0
        self.regsoon_count = 0
        self.skip_count = 0
        self.already_count = 0
        self.attempts_count = 0
        self.done_count = 0

        # Generator unique window
        self.gen_lock = threading.Lock()
        self.gen_seen = set()
        self.gen_order = deque()

        # Settings (default)
        self.theme_mode = tk.StringVar(value="dark")  # "dark"|"light"
        self.auto_limit = tk.BooleanVar(value=True)

        self.custom_seconds = tk.DoubleVar(value=2.0)
        self.pause_seconds = tk.DoubleVar(value=0.25)

        self.save_every_n = tk.IntVar(value=250)
        self.ui_every_ms = tk.IntVar(value=150)

        self.workers = tk.IntVar(value=10)
        self.profile_base = tk.IntVar(value=1)
        self.use_chrome_profile = tk.BooleanVar(value=True)

        self.prefixes_text = tk.StringVar(value=", ".join(DEFAULT_PREFIXES))
        self.prefixes_list = list(DEFAULT_PREFIXES)

        # "manual launch" drivers (for login/captcha)
        self.launch_drivers_lock = threading.Lock()
        self.launch_drivers = []  # list[webdriver.Chrome]

        # Workers UI table state
        self.worker_rows = {}

        # Build UI
        self._load_config()
        self._setup_theme(self.theme_mode.get())
        self._build_ui()
        self._bind_auto_save()
        self._tick_ui()

        # on close -> stop + save
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # =======================
    # THEME
    # =======================
    def _setup_theme(self, mode: str):
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        # Palettes
        if mode == "light":
            COL = {
                "bg": "#f6f7fb",
                "panel": "#ffffff",
                "panel2": "#eef1f8",
                "text": "#111827",
                "muted": "#526071",
                "accent": "#2563eb",
                "ok": "#059669",
                "warn": "#d97706",
                "bad": "#dc2626",
                "select": "#dbeafe",
            }
        else:
            COL = {
                "bg": "#0f1115",
                "panel": "#141826",
                "panel2": "#10131d",
                "text": "#e9eef7",
                "muted": "#9aa6b2",
                "accent": "#6ea8fe",
                "ok": "#3ddc97",
                "warn": "#ffcc66",
                "bad": "#ff5c7c",
                "select": "#1f2a44",
            }

        self.COL = COL
        self.root.configure(bg=COL["bg"])
        self.root.option_add("*Font", ("Segoe UI", 10))

        style.configure(".", background=COL["bg"], foreground=COL["text"], fieldbackground=COL["panel2"])
        style.configure("TFrame", background=COL["bg"])
        style.configure("Card.TFrame", background=COL["panel"], relief="flat")
        style.configure("Card2.TFrame", background=COL["panel2"], relief="flat")

        style.configure("TLabel", background=COL["bg"], foreground=COL["text"])
        style.configure("Muted.TLabel", background=COL["bg"], foreground=COL["muted"])
        style.configure("Title.TLabel", background=COL["bg"], foreground=COL["text"], font=("Segoe UI", 14, "bold"))
        style.configure("Sub.TLabel", background=COL["bg"], foreground=COL["muted"], font=("Segoe UI", 10))

        style.configure("TNotebook", background=COL["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 8), background=COL["panel2"], foreground=COL["text"])
        style.map("TNotebook.Tab", background=[("selected", COL["panel"])])

        style.configure("TEntry", padding=6, relief="flat")
        style.configure("TSpinbox", padding=6, relief="flat")

        style.configure("Accent.TButton",
                        background=COL["accent"], foreground="#0b0d12" if mode == "dark" else "#ffffff",
                        padding=(14, 10), relief="flat", borderwidth=0)
        style.map("Accent.TButton", background=[("active", COL["accent"])])

        style.configure("Danger.TButton",
                        background=COL["bad"], foreground="#0b0d12" if mode == "dark" else "#ffffff",
                        padding=(14, 10), relief="flat", borderwidth=0)
        style.map("Danger.TButton", background=[("active", COL["bad"])])

        style.configure("Ghost.TButton",
                        background=COL["panel2"], foreground=COL["text"],
                        padding=(12, 10), relief="flat", borderwidth=0)
        style.map("Ghost.TButton", background=[("active", COL["panel"])])

        style.configure("Stat.TLabel", background=COL["panel"], foreground=COL["text"], font=("Segoe UI", 12, "bold"))
        style.configure("Stat2.TLabel", background=COL["panel"], foreground=COL["muted"])

        style.configure("Treeview",
                        background=COL["panel2"], fieldbackground=COL["panel2"],
                        foreground=COL["text"], borderwidth=0, relief="flat", rowheight=28)
        style.configure("Treeview.Heading", background=COL["panel"], foreground=COL["text"], relief="flat")
        style.map("Treeview", background=[("selected", COL["select"])], foreground=[("selected", COL["text"])])

    # =======================
    # UI BUILD
    # =======================
    def _build_ui(self):
        # Header
        header = ttk.Frame(self.root, padding=(18, 16))
        header.pack(fill="x")

        left = ttk.Frame(header)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="Firk Multi", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="Profiles • Launch all • Automation • Stable counters • Writer thread",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        right = ttk.Frame(header)
        right.pack(side="right")

        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(right, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="e")

        # Notebook
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.tab_control = ttk.Frame(nb)
        self.tab_stats = ttk.Frame(nb)
        self.tab_workers = ttk.Frame(nb)
        self.tab_logs = ttk.Frame(nb)

        nb.add(self.tab_control, text="Control")
        nb.add(self.tab_stats, text="Stats")
        nb.add(self.tab_workers, text="Workers")
        nb.add(self.tab_logs, text="Logs")

        self._build_tab_control()
        self._build_tab_stats()
        self._build_tab_workers()
        self._build_tab_logs()

    def _card(self, parent, pady=(0, 12)):
        c = ttk.Frame(parent, style="Card.TFrame", padding=14)
        c.pack(fill="x", pady=pady)
        return c

    def _build_tab_control(self):
        wrap = ttk.Frame(self.tab_control, padding=14)
        wrap.pack(fill="both", expand=True)

        # Actions (top)
        top = self._card(wrap, pady=(0, 14))
        row = ttk.Frame(top, style="Card.TFrame")
        row.pack(fill="x")

        ttk.Button(row, text="▶ Start automation", style="Accent.TButton", command=self.start).pack(side="left")
        ttk.Button(row, text="⏸ Pause/Resume", style="Ghost.TButton", command=self.toggle_pause).pack(side="left", padx=10)
        ttk.Button(row, text="⏹ Stop", style="Danger.TButton", command=self.stop).pack(side="left")

        ttk.Button(row, text="valid.txt", style="Ghost.TButton",
                   command=lambda: open_file_in_default_app(VALID_FILE)).pack(side="right")
        ttk.Button(row, text="regsoon.txt", style="Ghost.TButton",
                   command=lambda: open_file_in_default_app(REGSOON_FILE)).pack(side="right", padx=10)

        # Profiles tools
        prof = self._card(wrap, pady=(0, 14))
        ttk.Label(prof, text="Profiles", style="Stat.TLabel").pack(anchor="w")

        pr = ttk.Frame(prof, style="Card.TFrame")
        pr.pack(fill="x", pady=(10, 0))

        ttk.Button(pr, text="🧩 Create profiles (folders)", style="Ghost.TButton",
                   command=self.create_profiles).pack(side="left")
        ttk.Button(pr, text="🚀 Launch ALL profiles (login/captcha)", style="Accent.TButton",
                   command=self.launch_profiles).pack(side="left", padx=10)
        ttk.Button(pr, text="✖ Close launched", style="Ghost.TButton",
                   command=self.close_launched_profiles).pack(side="left")

        ttk.Button(pr, text="🧹 Clear range", style="Ghost.TButton",
                   command=self.clear_profiles_range).pack(side="right")
        ttk.Button(pr, text="💥 Clear ALL", style="Danger.TButton",
                   command=self.clear_profiles_all).pack(side="right", padx=10)

        # Settings
        settings = self._card(wrap)
        ttk.Label(settings, text="Settings", style="Stat.TLabel").pack(anchor="w")

        g = ttk.Frame(settings, style="Card.TFrame")
        g.pack(fill="x", pady=(10, 0))
        g.grid_columnconfigure(1, weight=1)

        def add_row(r, label, widget):
            ttk.Label(g, text=label).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=8)
            widget.grid(row=r, column=1, sticky="ew", pady=8)

        # Workers row
        wrow = ttk.Frame(g, style="Card.TFrame")
        ttk.Checkbutton(wrow, text="Auto-limit", variable=self.auto_limit).pack(side="left")
        ttk.Label(wrow, text="Workers").pack(side="left", padx=(12, 0))
        ttk.Spinbox(wrow, from_=1, to=30, width=6, textvariable=self.workers).pack(side="left", padx=10)
        ttk.Label(wrow, text="Profile base").pack(side="left", padx=(18, 0))
        ttk.Spinbox(wrow, from_=1, to=999, width=6, textvariable=self.profile_base).pack(side="left", padx=10)
        ttk.Checkbutton(wrow, text="Separate Chrome profiles", variable=self.use_chrome_profile).pack(side="left", padx=14)
        add_row(0, "Parallel", wrow)

        # Prefixes
        prow = ttk.Frame(g, style="Card.TFrame")
        ttk.Entry(prow, textvariable=self.prefixes_text).pack(side="left", fill="x", expand=True)
        ttk.Button(prow, text="Apply", style="Ghost.TButton", command=self.apply_prefixes).pack(side="left", padx=10)
        add_row(1, "Prefixes", prow)

        # Timing
        trow = ttk.Frame(g, style="Card.TFrame")
        ttk.Label(trow, text="Services wait (sec)").pack(side="left")
        ttk.Entry(trow, width=8, textvariable=self.custom_seconds).pack(side="left", padx=10)
        ttk.Label(trow, text="Pause between numbers (sec)").pack(side="left", padx=(18, 0))
        ttk.Entry(trow, width=8, textvariable=self.pause_seconds).pack(side="left", padx=10)
        add_row(2, "Timing", trow)

        # Flush/UI
        frow = ttk.Frame(g, style="Card.TFrame")
        ttk.Label(frow, text="Save every N").pack(side="left")
        ttk.Spinbox(frow, from_=50, to=5000, width=8, textvariable=self.save_every_n).pack(side="left", padx=10)
        ttk.Label(frow, text="UI tick (ms)").pack(side="left", padx=(18, 0))
        ttk.Spinbox(frow, from_=80, to=800, width=8, textvariable=self.ui_every_ms).pack(side="left", padx=10)
        add_row(3, "System", frow)

        # Theme row
        theme_row = ttk.Frame(g, style="Card.TFrame")
        ttk.Label(theme_row, text="Theme").pack(side="left")
        ttk.Radiobutton(theme_row, text="Dark", value="dark", variable=self.theme_mode,
                        command=self.apply_theme).pack(side="left", padx=10)
        ttk.Radiobutton(theme_row, text="Light", value="light", variable=self.theme_mode,
                        command=self.apply_theme).pack(side="left", padx=10)

        # Recommended hint
        rec = recommended_workers()
        ttk.Label(theme_row, text=f"Recommended: {rec}", style="Muted.TLabel").pack(side="left", padx=14)
        add_row(4, "Look", theme_row)

    def _build_tab_stats(self):
        wrap = ttk.Frame(self.tab_stats, padding=14)
        wrap.pack(fill="both", expand=True)

        self.stat_valid = tk.StringVar(value="0")
        self.stat_regsoon = tk.StringVar(value="0")
        self.stat_skip = tk.StringVar(value="0")
        self.stat_already = tk.StringVar(value="0")
        self.stat_attempts = tk.StringVar(value="0")
        self.stat_done = tk.StringVar(value="0")
        self.stat_rate = tk.StringVar(value="0.00/s")

        cards = ttk.Frame(wrap)
        cards.pack(fill="x")

        def stat_card(parent, title, var):
            c = ttk.Frame(parent, style="Card.TFrame", padding=14)
            c.pack(side="left", fill="both", expand=True, padx=(0, 12))
            ttk.Label(c, text=title, style="Stat2.TLabel").pack(anchor="w")
            ttk.Label(c, textvariable=var, style="Stat.TLabel").pack(anchor="w", pady=(6, 0))
            return c

        stat_card(cards, "VALID", self.stat_valid)
        stat_card(cards, "REGSOON", self.stat_regsoon)
        stat_card(cards, "SKIP", self.stat_skip)
        stat_card(cards, "ALREADY", self.stat_already)

        cards2 = ttk.Frame(wrap)
        cards2.pack(fill="x", pady=(12, 0))
        stat_card(cards2, "ATTEMPTS", self.stat_attempts)
        stat_card(cards2, "DONE", self.stat_done)

        c_rate = ttk.Frame(cards2, style="Card.TFrame", padding=14)
        c_rate.pack(side="left", fill="both", expand=True)
        ttk.Label(c_rate, text="RATE", style="Stat2.TLabel").pack(anchor="w")
        ttk.Label(c_rate, textvariable=self.stat_rate, style="Stat.TLabel").pack(anchor="w", pady=(6, 0))

        self.last_rate_tick = time.time()
        self.last_done_snapshot = 0

    def _build_tab_workers(self):
        wrap = ttk.Frame(self.tab_workers, padding=14)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="Workers status", style="Title.TLabel").pack(anchor="w", pady=(0, 10))

        cols = ("wid", "profile", "number", "state", "consec_err", "rate")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", height=18)
        self.tree.pack(fill="both", expand=True)

        headings = {
            "wid": "W",
            "profile": "Profile",
            "number": "Current",
            "state": "State",
            "consec_err": "Errs",
            "rate": "Rate",
        }
        widths = {"wid": 50, "profile": 80, "number": 180, "state": 420, "consec_err": 70, "rate": 90}
        for c in cols:
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor="w")

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=sb.set)
        sb.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")

    def _build_tab_logs(self):
        wrap = ttk.Frame(self.tab_logs, padding=14)
        wrap.pack(fill="both", expand=True)

        self.log_box = tk.Text(
            wrap,
            height=28,
            wrap="word",
            bg=self.COL["panel2"],
            fg=self.COL["text"],
            insertbackground=self.COL["text"],
            relief="flat",
            highlightthickness=0
        )
        self.log_box.pack(fill="both", expand=True)

    # =======================
    # UI LOOP
    # =======================
    def _tick_ui(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log_box.insert("end", payload + "\n")
                    self.log_box.see("end")
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "worker":
                    self._ui_update_worker(**payload)
        except Empty:
            pass

        self._ui_refresh_stats()
        self.root.after(max(80, int(self.ui_every_ms.get())), self._tick_ui)

    def _ui_refresh_stats(self):
        with self.count_lock:
            v = self.valid_count
            r = self.regsoon_count
            s = self.skip_count
            a = self.already_count
            att = self.attempts_count
            d = self.done_count

        self.stat_valid.set(str(v))
        self.stat_regsoon.set(str(r))
        self.stat_skip.set(str(s))
        self.stat_already.set(str(a))
        self.stat_attempts.set(str(att))
        self.stat_done.set(str(d))

        now = time.time()
        dt = now - self.last_rate_tick
        if dt >= 1.0:
            rate = (d - self.last_done_snapshot) / dt
            self.stat_rate.set(fmt_rate(rate))
            self.last_rate_tick = now
            self.last_done_snapshot = d

    def _ui_update_worker(self, wid, profile, number, state, consec_err, rate):
        key = str(wid)
        values = (wid, profile, number, state, consec_err, rate)
        if key not in self.worker_rows:
            iid = self.tree.insert("", "end", values=values)
            self.worker_rows[key] = {"iid": iid}
        else:
            iid = self.worker_rows[key]["iid"]
            self.tree.item(iid, values=values)

    def _log(self, msg):
        self.q.put(("log", msg))

    def _set_status(self, msg):
        self.q.put(("status", msg))

    # =======================
    # Config persistence
    # =======================
    def _load_config(self):
        try:
            if not os.path.exists(CONFIG_FILE):
                return
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)

            self.theme_mode.set(cfg.get("theme_mode", self.theme_mode.get()))
            self.auto_limit.set(bool(cfg.get("auto_limit", self.auto_limit.get())))

            self.custom_seconds.set(float(cfg.get("custom_seconds", self.custom_seconds.get())))
            self.pause_seconds.set(float(cfg.get("pause_seconds", self.pause_seconds.get())))
            self.save_every_n.set(int(cfg.get("save_every_n", self.save_every_n.get())))
            self.ui_every_ms.set(int(cfg.get("ui_every_ms", self.ui_every_ms.get())))
            self.workers.set(int(cfg.get("workers", self.workers.get())))
            self.profile_base.set(int(cfg.get("profile_base", self.profile_base.get())))
            self.use_chrome_profile.set(bool(cfg.get("use_chrome_profile", self.use_chrome_profile.get())))

            ptxt = cfg.get("prefixes_text", self.prefixes_text.get())
            if isinstance(ptxt, str) and ptxt.strip():
                self.prefixes_text.set(ptxt)

            self.prefixes_list = parse_prefixes(self.prefixes_text.get()) or list(DEFAULT_PREFIXES)

        except Exception:
            # quietly ignore
            pass

    def _save_config(self):
        try:
            cfg = {
                "theme_mode": self.theme_mode.get(),
                "auto_limit": bool(self.auto_limit.get()),
                "custom_seconds": float(self.custom_seconds.get()),
                "pause_seconds": float(self.pause_seconds.get()),
                "save_every_n": int(self.save_every_n.get()),
                "ui_every_ms": int(self.ui_every_ms.get()),
                "workers": int(self.workers.get()),
                "profile_base": int(self.profile_base.get()),
                "use_chrome_profile": bool(self.use_chrome_profile.get()),
                "prefixes_text": self.prefixes_text.get(),
            }
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _bind_auto_save(self):
        # Save config when values change (throttled)
        self._save_timer = None

        def schedule_save(*_):
            if self._save_timer is not None:
                try:
                    self.root.after_cancel(self._save_timer)
                except Exception:
                    pass
            self._save_timer = self.root.after(450, self._save_config)

        vars_ = [
            self.theme_mode, self.auto_limit,
            self.custom_seconds, self.pause_seconds,
            self.save_every_n, self.ui_every_ms,
            self.workers, self.profile_base, self.use_chrome_profile,
            self.prefixes_text
        ]
        for v in vars_:
            try:
                v.trace_add("write", schedule_save)
            except Exception:
                pass

    # =======================
    # Settings helpers
    # =======================
    def apply_theme(self):
        # Rebuild theme live (simple and reliable)
        mode = self.theme_mode.get()
        self._setup_theme(mode)

        # Update logs bg/fg immediately if exists
        try:
            self.log_box.configure(bg=self.COL["panel2"], fg=self.COL["text"], insertbackground=self.COL["text"])
        except Exception:
            pass

        self._log(f"🎨 Theme: {mode}")
        self._save_config()

    def apply_prefixes(self):
        parsed = parse_prefixes(self.prefixes_text.get())
        if not parsed:
            self.prefixes_list = list(DEFAULT_PREFIXES)
            self.prefixes_text.set(", ".join(self.prefixes_list))
        else:
            self.prefixes_list = parsed
        self._log(f"🔧 Prefixes: {', '.join(self.prefixes_list)}")

    def get_services_wait(self):
        try:
            return max(0.3, float(self.custom_seconds.get()))
        except Exception:
            return 2.0

    def get_pause(self):
        try:
            return max(0.0, float(self.pause_seconds.get()))
        except Exception:
            return 0.25

    def effective_workers(self):
        n = max(1, int(self.workers.get()))
        if self.auto_limit.get():
            n = min(n, recommended_workers())
        return clamp(n, 1, 20)

    # =======================
    # Thread-safe counters
    # =======================
    def inc(self, name: str, delta: int = 1):
        with self.count_lock:
            if name == "valid":
                self.valid_count += delta
            elif name == "regsoon":
                self.regsoon_count += delta
            elif name == "skip":
                self.skip_count += delta
            elif name == "already":
                self.already_count += delta
            elif name == "attempt":
                self.attempts_count += delta
            elif name == "done":
                self.done_count += delta

    # =======================
    # Generator unique window
    # =======================
    def gen_next_number_shared(self) -> str:
        with self.gen_lock:
            prefs = self.prefixes_list or DEFAULT_PREFIXES
            while True:
                pref = random.choice(prefs)
                tail = f"{random.randint(0, 9_999_999):07d}"
                num = pref + tail

                if num in self.gen_seen:
                    continue

                self.gen_seen.add(num)
                self.gen_order.append(num)

                # shrink if too big
                while len(self.gen_seen) > GEN_WINDOW and self.gen_order:
                    old = self.gen_order.popleft()
                    self.gen_seen.discard(old)

                return num

    # =======================
    # Profiles tools
    # =======================
    def profiles_range(self):
        n = self.effective_workers()
        base = max(1, int(self.profile_base.get()))
        return base, base + n - 1, n

    def create_profiles(self):
        if not self.use_chrome_profile.get():
            messagebox.showinfo("Profiles", "Включи 'Separate Chrome profiles' чтобы использовать папки профилей.")
            return

        base, last, n = self.profiles_range()
        os.makedirs(PROFILES_ROOT, exist_ok=True)
        for pid in range(base, last + 1):
            os.makedirs(profile_dir(pid), exist_ok=True)

        self._log(f"🧩 Created {n} profile folders: {base}..{last}")

    def clear_profiles_range(self):
        base, last, n = self.profiles_range()
        if not os.path.exists(PROFILES_ROOT):
            self._log("🧹 Profiles folder not found — nothing to clear.")
            return

        if not messagebox.askyesno("Clear profiles", f"Удалить профили range {base}..{last}?"):
            return

        deleted = 0
        for pid in range(base, last + 1):
            p = profile_dir(pid)
            if os.path.exists(p):
                try:
                    shutil.rmtree(p, ignore_errors=True)
                    deleted += 1
                except Exception:
                    pass

        self._log(f"🧹 Cleared range: deleted {deleted} folders ({base}..{last})")

    def clear_profiles_all(self):
        if not os.path.exists(PROFILES_ROOT):
            self._log("💥 Profiles folder not found — nothing to clear.")
            return
        if not messagebox.askyesno("Clear ALL", "Удалить ВСЕ профили в chrome_profiles?"):
            return
        try:
            shutil.rmtree(PROFILES_ROOT, ignore_errors=True)
        except Exception:
            pass
        self._log("💥 Cleared ALL profiles in chrome_profiles")

    # =======================
    # Launch profiles (manual login)
    # =======================
    def build_driver(self, profile_id: int, for_manual=False):
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-sync")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")

        # keep background windows fast
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")

        # keep images ON (captcha)
        options.page_load_strategy = "eager"
        options.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications": 2})

        if self.use_chrome_profile.get():
            prof_dir = profile_dir(profile_id)
            os.makedirs(prof_dir, exist_ok=True)
            options.add_argument(f"--user-data-dir={prof_dir}")
            options.add_argument("--profile-directory=Default")

        # "manual" — НЕ блокируем лишнее агрессивно (пусть всё грузится нормально)
        driver = webdriver.Chrome(options=options)
        driver.implicitly_wait(0)

        if not for_manual:
            # block trackers/video only (NOT images)
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                driver.execute_cdp_cmd("Network.setBlockedURLs", {
                    "urls": ["*doubleclick*", "*googletagmanager*", "*google-analytics*", "*.mp4", "*.webm", "*.avi"]
                })
            except Exception:
                pass

        return driver

    def launch_profiles(self):
        if self.launch_drivers:
            messagebox.showinfo("Launch", "Профили уже запущены. Нажми 'Close launched' чтобы закрыть.")
            return

        base, last, n = self.profiles_range()
        self._log(f"🚀 Launch profiles {base}..{last} (manual login/captcha)")

        def _launch_one(pid, wid):
            try:
                d = self.build_driver(pid, for_manual=True)
                d.get(URL)
                with self.launch_drivers_lock:
                    self.launch_drivers.append(d)
                self.q.put(("worker", {
                    "wid": f"M{wid}", "profile": pid, "number": "-", "state": "Manual launched (login here)",
                    "consec_err": "-", "rate": "-"
                }))
            except Exception as e:
                self._log(f"[Manual {pid}] launch error: {type(e).__name__}")

        # launch in background threads (чтобы UI не фризил)
        for idx, pid in enumerate(range(base, last + 1), start=1):
            t = threading.Thread(target=_launch_one, args=(pid, idx), daemon=True)
            t.start()

        self._set_status("Manual profiles launched (login/2FA)")

    def close_launched_profiles(self):
        with self.launch_drivers_lock:
            drivers = list(self.launch_drivers)
            self.launch_drivers.clear()

        closed = 0
        for d in drivers:
            try:
                d.quit()
                closed += 1
            except Exception:
                pass
        self._log(f"✖ Closed launched profiles: {closed}")
        self._set_status("Готово")

    # =======================
    # Pause/Resume
    # =======================
    def toggle_pause(self):
        if not self.threads:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self._log("▶ Resume")
            self._set_status("Працюю…")
        else:
            self.pause_event.set()
            self._log("⏸ Paused")
            self._set_status("Paused")

    # =======================
    # Writer thread
    # =======================
    def writer_loop(self):
        buf_valid = []
        buf_regsoon = []
        last_flush = time.time()

        def flush():
            nonlocal buf_valid, buf_regsoon, last_flush
            if buf_valid:
                append_lines(VALID_FILE, buf_valid)
                buf_valid = []
            if buf_regsoon:
                append_lines(REGSOON_FILE, buf_regsoon)
                buf_regsoon = []
            last_flush = time.time()

        while not self.writer_stop.is_set():
            try:
                kind, num = self.write_q.get(timeout=0.25)
                if kind == "valid":
                    buf_valid.append(num)
                elif kind == "regsoon":
                    buf_regsoon.append(num)
            except Empty:
                pass

            if (len(buf_valid) + len(buf_regsoon)) >= max(20, int(self.save_every_n.get())) or (time.time() - last_flush) > 2.0:
                flush()

        flush()

    # =======================
    # Selenium helpers
    # =======================
    def js_click(self, driver, el):
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        driver.execute_script("arguments[0].click();", el)

    def wait_client_button(self, driver):
        return WebDriverWait(driver, WAIT_UI_SECONDS, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            ))
        )

    def click_client(self, driver):
        self.js_click(driver, self.wait_client_button(driver))

    def wait_msisdn_ready(self, driver):
        wait = WebDriverWait(driver, WAIT_UI_SECONDS, poll_frequency=POLL)
        wait.until(EC.presence_of_element_located((By.ID, "msisdn")))
        wait.until(EC.element_to_be_clickable((By.ID, "msisdn")))
        return wait

    def back_to_home_and_open_client(self, driver):
        if driver.find_elements(By.ID, "msisdn"):
            return self.wait_msisdn_ready(driver)

        backs = driver.find_elements(By.XPATH, "//button[.//mat-icon[normalize-space(text())='arrow_back']]")
        if backs:
            self.js_click(driver, backs[0])
            time.sleep(0.10)
            try:
                return self.wait_msisdn_ready(driver)
            except Exception:
                pass

        self.click_client(driver)
        time.sleep(0.10)
        return self.wait_msisdn_ready(driver)

    def js_has_label_text(self, driver, text_value: str) -> bool:
        return bool(driver.execute_script(
            """
            const t = arguments[0];
            const nodes = document.querySelectorAll('div.label');
            for (const n of nodes) { if ((n.textContent || '').trim() === t) return true; }
            return false;
            """, text_value
        ))

    def js_has_error_text_contains(self, driver, contains_value: str) -> bool:
        return bool(driver.execute_script(
            """
            const t = arguments[0];
            const nodes = document.querySelectorAll('div.error-text');
            for (const n of nodes) {
              const s = (n.textContent || '').trim();
              if (s.includes(t)) return true;
            }
            return false;
            """, contains_value
        ))

    def has_error_screen(self, driver):
        return bool(driver.execute_script(
            "const h=document.querySelector('h1'); return h && (h.textContent||'').trim()==='Помилка';"
        ))

    def click_ok_anywhere(self, driver, timeout=2):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH,
                "//button[.//span[normalize-space(.)='Ок' or normalize-space(.)='ОК' or normalize-space(.)='OK' or normalize-space(.)='Добре']]"
                " | //button[normalize-space(.)='Ок' or normalize-space(.)='ОК' or normalize-space(.)='OK' or normalize-space(.)='Добре']"
            ))
        )
        self.js_click(driver, btn)

    def handle_error_screen_once(self, driver):
        if self.has_error_screen(driver):
            try:
                self.click_ok_anywhere(driver, timeout=2)
                time.sleep(0.10)
            except Exception:
                pass
            return True
        return False

    def wait_search_ready(self, driver, timeout=WAIT_UI_SECONDS) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_event.is_set():
                return False
            self.handle_error_screen_once(driver)
            try:
                ready = bool(driver.execute_script(
                    """
                    const btn = Array.from(document.querySelectorAll('button'))
                      .find(b => (b.textContent || '').trim() === 'Пошук');
                    if (!btn) return false;
                    const cls = btn.getAttribute('class') || '';
                    if (cls.includes('mat-button-disabled')) return false;
                    const r = btn.getBoundingClientRect();
                    return r.width>0 && r.height>0 && !btn.disabled;
                    """
                ))
                if ready:
                    return True
            except Exception:
                pass
            time.sleep(POLL)
        return False

    def set_number_safe(self, driver, wait, number):
        inp = wait.until(EC.element_to_be_clickable((By.ID, "msisdn")))
        full = "380" + number

        try:
            inp.click()
            inp.send_keys(Keys.CONTROL, "a")
            inp.send_keys(Keys.BACKSPACE)
            time.sleep(0.02)
        except Exception:
            pass

        try:
            driver.execute_script(
                """
                const el = arguments[0];
                const v = arguments[1];
                const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
                el.focus();
                setter.call(el,'');
                el.dispatchEvent(new Event('input',{bubbles:true}));
                setter.call(el,v);
                el.dispatchEvent(new Event('input',{bubbles:true}));
                el.dispatchEvent(new Event('change',{bubbles:true}));
                el.blur();
                """,
                inp, full
            )
        except Exception:
            try:
                inp.clear()
            except Exception:
                pass
            inp.send_keys(full)

        self.wait_search_ready(driver, timeout=3)

    def click_search(self, driver, wait):
        btn = wait.until(EC.element_to_be_clickable((By.XPATH,
            "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Пошук']]"
        )))
        self.js_click(driver, btn)

    def wait_services_only_fast(self, driver, wait_seconds: float) -> bool:
        self.handle_error_screen_once(driver)
        if self.js_has_label_text(driver, "Реєстрація послуг"):
            return True
        end = time.time() + wait_seconds
        while time.time() < end:
            if self.stop_event.is_set():
                return False
            self.handle_error_screen_once(driver)
            if self.js_has_label_text(driver, "Реєстрація послуг"):
                return True
            time.sleep(POLL)
        return False

    def has_start_pack_fast(self, driver) -> bool:
        self.handle_error_screen_once(driver)
        return self.js_has_label_text(driver, "Реєстрація стартового пакету")

    def click_start_pack(self, driver, timeout=6):
        el = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація стартового пакету']]"
            ))
        )
        self.js_click(driver, el)

    def click_register(self, driver, timeout=6):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH,
                "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Зареєструвати']]"
            ))
        )
        self.js_click(driver, btn)

    def wait_already_error_short_fast(self, driver, seconds=1.1) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set():
                return False
            self.handle_error_screen_once(driver)
            if self.js_has_error_text_contains(driver, "Номер вже було зареєстровано"):
                return True
            time.sleep(POLL)
        return False

    # =======================
    # Worker loop
    # =======================
    def ui_worker(self, wid, profile, number, state, consec_err, rate):
        self.q.put(("worker", {
            "wid": wid, "profile": profile, "number": number, "state": state,
            "consec_err": consec_err, "rate": rate
        }))

    def worker_loop(self, wid: int, profile_id: int):
        driver = None
        consec_err = 0
        local_done = 0

        wait_seconds = self.get_services_wait()
        pause = self.get_pause()

        last_rate_t = time.time()
        last_rate_done = 0

        def calc_rate():
            nonlocal last_rate_t, last_rate_done
            now = time.time()
            dt = now - last_rate_t
            if dt <= 0:
                return "0.00/s"
            rate = (local_done - last_rate_done) / dt
            if dt >= 1.0:
                last_rate_t = now
                last_rate_done = local_done
            return fmt_rate(rate)

        def restart_driver():
            nonlocal driver, consec_err
            try:
                if driver:
                    driver.quit()
            except Exception:
                pass
            time.sleep(RESTART_COOLDOWN_SEC)
            driver = self.build_driver(profile_id, for_manual=False)
            driver.get(URL)
            consec_err = 0

        try:
            driver = self.build_driver(profile_id, for_manual=False)
            driver.get(URL)

            self._log(f"[W{wid}] Ожидаю логин/2FA/капчу… профиль={profile_id}")
            self.ui_worker(wid, profile_id, "-", "Waiting login", consec_err, calc_rate())

            wait_login = WebDriverWait(driver, WAIT_LOGIN_SECONDS, poll_frequency=POLL)
            wait_login.until(EC.presence_of_element_located((By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            )))
            self._log(f"[W{wid}] ✅ Авторизация OK")
            self.ui_worker(wid, profile_id, "-", "Running", consec_err, calc_rate())

            while not self.stop_event.is_set():
                # Pause support
                if self.pause_event.is_set():
                    self.ui_worker(wid, profile_id, "-", "Paused", consec_err, calc_rate())
                    while self.pause_event.is_set() and not self.stop_event.is_set():
                        time.sleep(0.15)
                    continue

                number = self.gen_next_number_shared()
                full = "380" + number

                self.inc("attempt", 1)
                self._set_status(f"W{wid}: {full}")
                self.ui_worker(wid, profile_id, full, "Typing / Search", consec_err, calc_rate())

                try:
                    wait = self.back_to_home_and_open_client(driver)
                    self.set_number_safe(driver, wait, number)

                    if not self.wait_search_ready(driver, timeout=WAIT_UI_SECONDS):
                        self.inc("skip", 1)
                        self.inc("done", 1)
                        local_done += 1
                        consec_err = max(0, consec_err - 1)
                        self.ui_worker(wid, profile_id, full, "SKIP (search not ready)", consec_err, calc_rate())
                        continue

                    self.click_search(driver, wait)
                    self.handle_error_screen_once(driver)

                    services = self.wait_services_only_fast(driver, wait_seconds)
                    has_start_pack = self.has_start_pack_fast(driver)

                    # REGSOON (НЕ считаем как SKIP)
                    if has_start_pack and not services:
                        self.write_q.put(("regsoon", number))
                        self.inc("regsoon", 1)
                        self.inc("done", 1)
                        local_done += 1
                        consec_err = max(0, consec_err - 1)
                        self.ui_worker(wid, profile_id, full, "REGSOON", consec_err, calc_rate())
                        continue

                    # SKIP
                    if (not services) and (not has_start_pack):
                        self.inc("skip", 1)
                        self.inc("done", 1)
                        local_done += 1
                        consec_err = max(0, consec_err - 1)
                        self.ui_worker(wid, profile_id, full, "SKIP", consec_err, calc_rate())
                        continue

                    # VALID path
                    if services and has_start_pack:
                        self.ui_worker(wid, profile_id, full, "Registering…", consec_err, calc_rate())

                        self.click_start_pack(driver)
                        time.sleep(0.14)
                        self.click_register(driver)

                        already = self.wait_already_error_short_fast(driver, seconds=1.1)
                        try:
                            self.click_ok_anywhere(driver, timeout=4)
                        except Exception:
                            pass

                        if already:
                            self.inc("already", 1)
                            self.inc("done", 1)
                            local_done += 1
                            self.ui_worker(wid, profile_id, full, "ALREADY", consec_err, calc_rate())
                        else:
                            self.write_q.put(("valid", number))
                            self.inc("valid", 1)
                            self.inc("done", 1)
                            local_done += 1
                            self.ui_worker(wid, profile_id, full, "VALID", consec_err, calc_rate())

                        consec_err = max(0, consec_err - 1)
                    else:
                        self.inc("skip", 1)
                        self.inc("done", 1)
                        local_done += 1
                        consec_err = max(0, consec_err - 1)
                        self.ui_worker(wid, profile_id, full, "SKIP (other)", consec_err, calc_rate())

                except (TimeoutException, StaleElementReferenceException, WebDriverException) as e:
                    consec_err += 1
                    self.inc("skip", 1)
                    self.inc("done", 1)
                    local_done += 1
                    self.ui_worker(wid, profile_id, full, f"ERR: {type(e).__name__}", consec_err, calc_rate())

                    if consec_err >= MAX_CONSEC_ERRORS and not self.stop_event.is_set():
                        self._log(f"[W{wid}] ♻ Restart driver (consec_err={consec_err})")
                        self.ui_worker(wid, profile_id, full, "Restarting driver…", consec_err, calc_rate())
                        restart_driver()

                if pause:
                    time.sleep(pause)

        finally:
            try:
                if driver:
                    driver.quit()
            except Exception:
                pass
            self.ui_worker(wid, profile_id, "-", "Closed", consec_err, "0.00/s")
            self._log(f"[W{wid}] 🧹 closed")

    # =======================
    # START / STOP
    # =======================
    def start(self):
        if self.threads:
            return

        self.apply_prefixes()

        n = self.effective_workers()
        base = max(1, int(self.profile_base.get()))

        if self.auto_limit.get() and n != int(self.workers.get()):
            self._log(f"⚡ Auto-limit: workers {self.workers.get()} → {n}")
            self.workers.set(n)

        # Reset
        self.stop_event.clear()
        self.pause_event.clear()
        self.writer_stop.clear()

        with self.count_lock:
            self.valid_count = 0
            self.regsoon_count = 0
            self.skip_count = 0
            self.already_count = 0
            self.attempts_count = 0
            self.done_count = 0

        with self.gen_lock:
            self.gen_seen.clear()
            self.gen_order.clear()

        # writer
        self.writer_thread = threading.Thread(target=self.writer_loop, daemon=True)
        self.writer_thread.start()

        self._set_status("Працюю…")
        self._log(f"▶ Start automation: workers={n}, profiles={base}..{base+n-1}")

        # reset workers table rows
        for k in list(self.worker_rows.keys()):
            try:
                iid = self.worker_rows[k]["iid"]
                self.tree.delete(iid)
            except Exception:
                pass
        self.worker_rows.clear()

        # start workers
        self.threads = []
        for i in range(n):
            wid = i + 1
            pid = base + i
            t = threading.Thread(target=self.worker_loop, args=(wid, pid), daemon=True)
            self.threads.append(t)
            t.start()

    def stop(self):
        if not self.threads:
            return
        self._set_status("Зупинка…")
        self.stop_event.set()
        self.pause_event.clear()
        self._log("⏹ Stop — stopping…")

        def _join():
            # stop workers
            for t in self.threads:
                try:
                    t.join(timeout=12)
                except Exception:
                    pass
            self.threads = []

            # stop writer
            self.writer_stop.set()
            try:
                if self.writer_thread:
                    self.writer_thread.join(timeout=6)
            except Exception:
                pass
            self.writer_thread = None

            self._set_status("Готово")
            self._log("✅ Stopped. All saved.")

        threading.Thread(target=_join, daemon=True).start()

    # =======================
    # Close handler
    # =======================
    def _on_close(self):
        try:
            self._save_config()
        except Exception:
            pass

        # stop automation if running
        if self.threads:
            self.stop_event.set()
            self.pause_event.clear()
            self.writer_stop.set()

        # close manual launched
        try:
            self.close_launched_profiles()
        except Exception:
            pass

        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
