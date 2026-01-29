import os
import re
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import random
import subprocess
import sys

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

NUMBERS_FILE = "numbers.txt"
VALID_FILE = "valid.txt"
REGSOON_FILE = "regsoon.txt"

WAIT_LOGIN_SECONDS = 600
WAIT_UI_SECONDS = 12

POLL = 0.05

SPEED_WAIT_SECONDS = 1.7
ACCURACY_WAIT_SECONDS = 1.8

# ✅ prefixes from your screenshot
DEFAULT_PREFIXES = ["67", "96", "98", "63", "93"]

# Chrome profiles root (parallel safe)
PROFILES_ROOT = "chrome_profiles"
LAUNCHERS_DIR = "launchers"


# =======================
# CLI ARGS
# =======================
def parse_cli_args(argv):
    """
    Supports:
      --profile-id N
      --no-profile
    """
    out = {"profile_id": None, "use_profile": None}
    i = 0
    while i < len(argv):
        a = argv[i].strip()
        if a == "--profile-id" and i + 1 < len(argv):
            try:
                out["profile_id"] = int(argv[i + 1])
            except Exception:
                pass
            i += 2
            continue
        if a == "--no-profile":
            out["use_profile"] = False
            i += 1
            continue
        i += 1
    return out


CLI = parse_cli_args(sys.argv[1:])


# =======================
# HELPERS
# =======================
def normalize_to_9_digits(raw_digits: str):
    d = re.sub(r"\D+", "", raw_digits or "")
    if not d:
        return None
    if d.startswith("380") and len(d) == 12:
        return d[3:]
    if d.startswith("0") and len(d) == 10:
        return d[1:]
    if len(d) == 9:
        return d
    return None


def extract_number_from_line(line: str):
    digits = re.sub(r"\D+", "", line)
    m = re.search(r"380\d{9}", digits)
    if m:
        return normalize_to_9_digits(m.group(0))
    m = re.search(r"0\d{9}", digits)
    if m:
        return normalize_to_9_digits(m.group(0))
    m = re.search(r"\b\d{9}\b", line)
    if m:
        return normalize_to_9_digits(m.group(0))
    return None


def load_lines_with_numbers(path: str):
    if not os.path.exists(path):
        return [], []
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f]
    items = []
    seen = set()
    for idx, line in enumerate(lines):
        num = extract_number_from_line(line)
        if not num:
            continue
        if num in seen:
            continue
        seen.add(num)
        items.append({"idx": idx, "line": line, "number": num})
    return lines, items


def append_lines(path, lines):
    if not lines:
        return
    with open(path, "a", encoding="utf-8") as f:
        for x in lines:
            f.write(x + "\n")


def rewrite_numbers_file(original_lines, to_delete_numbers: set, keep_non_numbers: bool):
    new_lines = []
    for ln in original_lines:
        num = extract_number_from_line(ln)
        if not num:
            if keep_non_numbers:
                new_lines.append(ln)
            continue
        if num in to_delete_numbers:
            continue
        new_lines.append(ln)

    with open(NUMBERS_FILE, "w", encoding="utf-8") as f:
        for ln in new_lines:
            f.write(ln + "\n")


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}г {m}хв {s}с"
    if m > 0:
        return f"{m}хв {s}с"
    return f"{s}с"


def open_folder(path: str):
    path = os.path.abspath(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def open_file_in_default_app(filepath: str):
    filepath = os.path.abspath(filepath)
    if not os.path.exists(filepath):
        try:
            with open(filepath, "a", encoding="utf-8"):
                pass
        except Exception:
            return
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


def is_frozen_exe() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_script_path() -> str:
    try:
        return os.path.abspath(__file__)
    except Exception:
        return ""


def quote_win(s: str) -> str:
    if not s:
        return '""'
    if '"' in s:
        s = s.replace('"', '\\"')
    return f'"{s}"'


# =======================
# APP
# =======================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Firk — Lifecell Helper")
        self.root.geometry("1180x920")
        self.root.minsize(1080, 780)

        self._apply_win11_theme()

        # ---- State vars
        self.status_text = tk.StringVar(value="Готово")

        # ✅ default = CUSTOM 2.0s
        self.mode = tk.StringVar(value="custom")
        self.custom_seconds = tk.DoubleVar(value=2.0)

        # other UI defaults (per your request)
        self.pause_seconds = tk.DoubleVar(value=0.3)
        self.save_every_n = tk.IntVar(value=500)   # ✅
        self.ui_every_n = tk.IntVar(value=20)      # ✅
        self.log_every_n = tk.IntVar(value=50)     # ✅

        self.order = tk.StringVar(value="start")
        self.keep_non_numbers = tk.BooleanVar(value=True)
        self.write_regsoon = tk.BooleanVar(value=True)

        # ✅ generator ON by default (infinite)
        self.use_generator = tk.BooleanVar(value=True)

        # ✅ chrome profile ON by default
        self.use_chrome_profile = tk.BooleanVar(value=True)
        self.profile_id = tk.IntVar(value=1)

        # CLI overrides
        if CLI.get("use_profile") is False:
            self.use_chrome_profile.set(False)
        if CLI.get("profile_id"):
            self.profile_id.set(max(1, int(CLI["profile_id"])))

        # ✅ prefixes from screenshot
        self.prefixes_text = tk.StringVar(value=", ".join(DEFAULT_PREFIXES))
        self.prefixes_list = list(DEFAULT_PREFIXES)

        # runtime
        self.stop_event = threading.Event()
        self.worker = None
        self.valid_count = 0
        self.skipped_count = 0
        self.regsoon_count = 0
        self.already_count = 0
        self.run_started_at = None
        self.done_count = 0
        self.total_count = 0

        # generator cache
        self.gen_recent = set()

        # file mode buffers
        self.file_lines = []
        self.to_delete_numbers = set()
        self.valid_buf = []
        self.regsoon_buf = []

        # turbo cache for selenium elements
        self._cached = {"msisdn": None, "search_btn": None}

        # UI
        self._build_ui()

    # =======================
    # THEME / STYLES
    # =======================
    def _apply_win11_theme(self):
        self.style = ttk.Style(self.root)
        if sys.platform.startswith("win") and "vista" in self.style.theme_names():
            self.style.theme_use("vista")
        elif "clam" in self.style.theme_names():
            self.style.theme_use("clam")

        self.root.option_add("*Font", ("Segoe UI", 10))
        self.style.configure("H1.TLabel", font=("Segoe UI", 18, "bold"))
        self.style.configure("H2.TLabel", font=("Segoe UI", 12, "bold"))
        self.style.configure("Muted.TLabel", font=("Segoe UI", 10))
        self.style.configure("Card.TLabelframe", padding=12)
        self.style.configure("Card.TLabelframe.Label", font=("Segoe UI", 11, "bold"))
        self.style.configure("Primary.TButton", font=("Segoe UI", 11, "bold"))
        self.style.configure("Danger.TButton", font=("Segoe UI", 11, "bold"))
        self.style.configure("StatNum.TLabel", font=("Segoe UI", 16, "bold"))
        self.style.configure("StatCap.TLabel", font=("Segoe UI", 10))
        self.style.configure("Pill.TLabel", font=("Segoe UI", 10, "bold"), padding=(10, 4))
        try:
            self.style.configure("TNotebook.Tab", padding=(14, 8))
        except Exception:
            pass

    # =======================
    # UI BUILD
    # =======================
    def _build_ui(self):
        header = ttk.Frame(self.root, padding=(16, 14, 16, 8))
        header.pack(fill="x")

        left = ttk.Frame(header)
        left.pack(side="left", fill="x", expand=True)
        ttk.Label(left, text="Firk", style="H1.TLabel").pack(anchor="w")
        ttk.Label(left, text="Автоматизація перевірки/реєстрації • Turbo+Stable", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

        right = ttk.Frame(header)
        right.pack(side="right")
        self.pill = ttk.Label(right, textvariable=self.status_text, style="Pill.TLabel")
        self.pill.pack(anchor="e")

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=14, pady=10)

        self.tab_run = ttk.Frame(self.nb, padding=12)
        self.tab_settings = ttk.Frame(self.nb, padding=12)
        self.tab_logs = ttk.Frame(self.nb, padding=12)

        self.nb.add(self.tab_run, text="Запуск")
        self.nb.add(self.tab_settings, text="Налаштування")
        self.nb.add(self.tab_logs, text="Логи")

        self._build_run_tab()
        self._build_settings_tab()
        self._build_logs_tab()

        self._set_running_ui(False)

    def _build_run_tab(self):
        badges_frame = ttk.Frame(self.tab_run)
        badges_frame.pack(fill="x", pady=(0, 10))

        self._c_valid_bg = "#1F9D55"
        self._c_regsoon_bg = "#D9A400"
        self._c_skip_bg = "#6B7280"
        self._c_badge_fg = "#FFFFFF"

        self.badge_valid_var = tk.StringVar(value="VALID: 0")
        self.badge_regsoon_var = tk.StringVar(value="REGSOON: 0")
        self.badge_skip_var = tk.StringVar(value="SKIP: 0")

        self.badge_valid = tk.Label(
            badges_frame, textvariable=self.badge_valid_var,
            bg=self._c_valid_bg, fg=self._c_badge_fg,
            font=("Segoe UI", 10, "bold"), padx=12, pady=6, cursor="hand2"
        )
        self.badge_regsoon = tk.Label(
            badges_frame, textvariable=self.badge_regsoon_var,
            bg=self._c_regsoon_bg, fg=self._c_badge_fg,
            font=("Segoe UI", 10, "bold"), padx=12, pady=6, cursor="hand2"
        )
        self.badge_skip = tk.Label(
            badges_frame, textvariable=self.badge_skip_var,
            bg=self._c_skip_bg, fg=self._c_badge_fg,
            font=("Segoe UI", 10, "bold"), padx=12, pady=6, cursor="hand2"
        )

        self.badge_valid.pack(side="left")
        self.badge_regsoon.pack(side="left", padx=10)
        self.badge_skip.pack(side="left")

        self.badge_valid.bind("<Button-1>", lambda e: self.open_valid_file())
        self.badge_regsoon.bind("<Button-1>", lambda e: self.open_regsoon_file())
        self.badge_skip.bind("<Button-1>", lambda e: self.open_numbers_file())

        row = ttk.Frame(self.tab_run)
        row.pack(fill="x")

        self.card_valid = self._stat_card(row, "VALID", "0")
        self.card_skip = self._stat_card(row, "Пропущено", "0")
        self.card_regsoon = self._stat_card(row, "RegSoon", "0")
        self.card_rate = self._stat_card(row, "Середній/номер", "-")

        self.card_valid.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.card_skip.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.card_regsoon.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.card_rate.pack(side="left", fill="x", expand=True)

        prog = ttk.LabelFrame(self.tab_run, text="Прогрес", style="Card.TLabelframe")
        prog.pack(fill="x", pady=12)

        top = ttk.Frame(prog)
        top.pack(fill="x")
        self.progress_caption = tk.StringVar(value="0 / 0")
        ttk.Label(top, textvariable=self.progress_caption, style="H2.TLabel").pack(side="left")
        self.eta_caption = tk.StringVar(value="ETA: - | Пройшло: - | Режим: -")
        ttk.Label(top, textvariable=self.eta_caption, style="Muted.TLabel").pack(side="right")

        self.pbar = ttk.Progressbar(prog, orient="horizontal", mode="determinate", maximum=100)
        self.pbar.pack(fill="x", pady=(10, 0))

        actions = ttk.LabelFrame(self.tab_run, text="Керування", style="Card.TLabelframe")
        actions.pack(fill="x")

        btnrow = ttk.Frame(actions)
        btnrow.pack(fill="x")
        self.btn_start = ttk.Button(btnrow, text="▶ Почати", style="Primary.TButton", command=self.start)
        self.btn_stop = ttk.Button(btnrow, text="⏹ Стоп", style="Danger.TButton", command=self.stop)
        self.btn_start.pack(side="left")
        self.btn_stop.pack(side="left", padx=10)

        ttk.Button(btnrow, text="📁 Папка", command=self.open_files_folder).pack(side="left", padx=(10, 6))
        ttk.Button(btnrow, text="📄 valid.txt", command=self.open_valid_file).pack(side="left", padx=6)
        ttk.Button(btnrow, text="🕒 regsoon.txt", command=self.open_regsoon_file).pack(side="left", padx=6)
        ttk.Button(btnrow, text="🧾 numbers.txt", command=self.open_numbers_file).pack(side="left", padx=6)
        ttk.Button(btnrow, text="Очистити логи", command=self.clear_logs).pack(side="right")

        hint = ("Turbo+Stable: швидкі JS-перевірки + кеш елементів. "
                "Капча працює (картинки НЕ блокуємо). "
                "Є генерація 10 .bat для профілів.")
        ttk.Label(actions, text=hint, style="Muted.TLabel", wraplength=980).pack(anchor="w", pady=(10, 0))

    def _build_settings_tab(self):
        grid = ttk.Frame(self.tab_settings)
        grid.pack(fill="both", expand=True)

        left = ttk.Frame(grid)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))

        right = ttk.Frame(grid)
        right.pack(side="left", fill="both", expand=True)

        # Source
        src = ttk.LabelFrame(left, text="Джерело номерів", style="Card.TLabelframe")
        src.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(
            src,
            text="Генерувати номери автоматично (нескінченно, поки галочка увімкнена)",
            variable=self.use_generator
        ).pack(anchor="w")

        prefbox = ttk.LabelFrame(left, text="Префікси генератора", style="Card.TLabelframe")
        prefbox.pack(fill="x", pady=(0, 10))
        ttk.Label(prefbox, text="Впиши префікси (2 цифри) через кому/пробіл:", style="Muted.TLabel").pack(anchor="w")
        rowp = ttk.Frame(prefbox)
        rowp.pack(fill="x", pady=(6, 0))
        self.prefix_entry = ttk.Entry(rowp, textvariable=self.prefixes_text)
        self.prefix_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(rowp, text="Застосувати", command=self.apply_prefixes).pack(side="left", padx=10)
        self.prefix_hint = ttk.Label(prefbox, text=f"Поточні: {', '.join(self.prefixes_list)}", style="Muted.TLabel", wraplength=520)
        self.prefix_hint.pack(anchor="w", pady=(6, 0))
        self.prefix_entry.bind("<FocusOut>", lambda e: self.apply_prefixes(silent=True))

        filebox = ttk.LabelFrame(left, text="numbers.txt", style="Card.TLabelframe")
        filebox.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(filebox)
        row.pack(fill="x")
        ttk.Label(row, text="Порядок:", style="Muted.TLabel").pack(side="left")
        ttk.Radiobutton(row, text="З початку", variable=self.order, value="start").pack(side="left", padx=10)
        ttk.Radiobutton(row, text="З кінця", variable=self.order, value="end").pack(side="left", padx=10)
        ttk.Checkbutton(
            filebox,
            text="Зберігати рядки без номерів (текст/дати) у numbers.txt",
            variable=self.keep_non_numbers
        ).pack(anchor="w", pady=(8, 0))

        # Chrome profile
        prof = ttk.LabelFrame(left, text="Chrome профіль", style="Card.TLabelframe")
        prof.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(
            prof,
            text="Використовувати окремий профіль Chrome (рекомендовано для 10 запусків)",
            variable=self.use_chrome_profile
        ).pack(anchor="w")
        rprof = ttk.Frame(prof)
        rprof.pack(fill="x", pady=(8, 0))
        ttk.Label(rprof, text="Номер профілю (для цієї програми):", style="Muted.TLabel").pack(side="left")
        ttk.Spinbox(rprof, from_=1, to=100, width=8, textvariable=self.profile_id).pack(side="left", padx=8)
        ttk.Button(rprof, text="📁 Профілі", command=self.open_profiles_folder).pack(side="left", padx=8)

        rprof2 = ttk.Frame(prof)
        rprof2.pack(fill="x", pady=(8, 0))
        ttk.Button(rprof2, text="🧩 Згенерувати 10 .bat (профілі 1..10)", command=self.generate_10_bats).pack(side="left")
        ttk.Button(rprof2, text="📁 launchers", command=self.open_launchers_folder).pack(side="left", padx=10)

        # Wait mode
        waitbox = ttk.LabelFrame(right, text="Очікування «Реєстрація послуг»", style="Card.TLabelframe")
        waitbox.pack(fill="x", pady=(0, 10))
        row1 = ttk.Frame(waitbox)
        row1.pack(fill="x")
        ttk.Radiobutton(row1, text=f"Швидко ({SPEED_WAIT_SECONDS}s)", variable=self.mode, value="speed").pack(side="left")
        ttk.Radiobutton(row1, text=f"Надійно ({ACCURACY_WAIT_SECONDS}s)", variable=self.mode, value="accuracy").pack(side="left", padx=10)
        ttk.Radiobutton(row1, text="Кастом", variable=self.mode, value="custom").pack(side="left", padx=(0, 8))
        ttk.Entry(row1, width=6, textvariable=self.custom_seconds).pack(side="left")
        ttk.Label(row1, text="сек", style="Muted.TLabel").pack(side="left", padx=6)

        pace = ttk.LabelFrame(right, text="Швидкість", style="Card.TLabelframe")
        pace.pack(fill="x", pady=(0, 10))

        r2 = ttk.Frame(pace)
        r2.pack(fill="x")
        ttk.Label(r2, text="Пауза між номерами (сек):", style="Muted.TLabel").pack(side="left")
        ttk.Entry(r2, width=8, textvariable=self.pause_seconds).pack(side="left", padx=8)

        r3 = ttk.Frame(pace)
        r3.pack(fill="x", pady=(8, 0))
        ttk.Label(r3, text="Зберігати прогрес кожні N номерів:", style="Muted.TLabel").pack(side="left")
        ttk.Spinbox(r3, from_=1, to=5000, width=8, textvariable=self.save_every_n).pack(side="left", padx=8)
        ttk.Label(r3, text="(valid.txt + numbers.txt)", style="Muted.TLabel").pack(side="left")

        r4 = ttk.Frame(pace)
        r4.pack(fill="x", pady=(8, 0))
        ttk.Label(r4, text="Оновлювати UI кожні N номерів:", style="Muted.TLabel").pack(side="left")
        ttk.Spinbox(r4, from_=1, to=500, width=8, textvariable=self.ui_every_n).pack(side="left", padx=8)

        r5 = ttk.Frame(pace)
        r5.pack(fill="x", pady=(8, 0))
        ttk.Label(r5, text="Логувати кожні N номерів:", style="Muted.TLabel").pack(side="left")
        ttk.Spinbox(r5, from_=1, to=500, width=8, textvariable=self.log_every_n).pack(side="left", padx=8)

        reg = ttk.LabelFrame(right, text="RegSoon", style="Card.TLabelframe")
        reg.pack(fill="x")
        ttk.Checkbutton(
            reg,
            text="Записувати в regsoon.txt, якщо є «Реєстрація стартового пакету», але немає «Реєстрація послуг»",
            variable=self.write_regsoon
        ).pack(anchor="w")

    def _build_logs_tab(self):
        top = ttk.Frame(self.tab_logs)
        top.pack(fill="x")
        ttk.Label(top, text="Журнал", style="H2.TLabel").pack(side="left")
        ttk.Button(top, text="Скопіювати все", command=self.copy_logs).pack(side="right")
        ttk.Button(top, text="Очистити", command=self.clear_logs).pack(side="right", padx=8)

        body = ttk.Frame(self.tab_logs)
        body.pack(fill="both", expand=True, pady=(10, 0))
        self.log_box = tk.Text(body, height=18, wrap="word", undo=False)
        self.log_box.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.log_box.yview)
        sb.pack(side="right", fill="y")
        self.log_box.configure(yscrollcommand=sb.set)
        self.log_box.configure(font=("Consolas", 10))
        self.log_box.configure(state="disabled")

    def _stat_card(self, parent, caption, value):
        lf = ttk.LabelFrame(parent, text=caption, style="Card.TLabelframe")
        num = ttk.Label(lf, text=value, style="StatNum.TLabel")
        num.pack(anchor="w")
        cap = ttk.Label(lf, text=" ", style="StatCap.TLabel")
        cap.pack(anchor="w")
        lf._num = num
        lf._cap = cap
        return lf

    # =======================
    # Launchers (.bat)
    # =======================
    def generate_10_bats(self):
        base_dir = os.path.abspath(os.getcwd())
        out_dir = os.path.join(base_dir, LAUNCHERS_DIR)
        os.makedirs(out_dir, exist_ok=True)

        exe_or_py = os.path.abspath(sys.executable)  # python.exe or app.exe
        script = current_script_path()
        frozen = is_frozen_exe()

        def cmd_for(pid: int) -> str:
            if frozen:
                return f'{quote_win(exe_or_py)} --profile-id {pid}'
            return f'{quote_win(exe_or_py)} {quote_win(script)} --profile-id {pid}'

        for pid in range(1, 11):
            bat_path = os.path.join(out_dir, f"run_profile_{pid:02d}.bat")
            lines = [
                "@echo off",
                "cd /d \"%~dp0..\"",
                f"start \"Firk profile {pid:02d}\" {cmd_for(pid)}",
            ]
            with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
                f.write("\r\n".join(lines) + "\r\n")

        all_path = os.path.join(out_dir, "start_all_10.bat")
        all_lines = ["@echo off", "cd /d \"%~dp0..\""]
        for pid in range(1, 11):
            all_lines.append(f"start \"Firk profile {pid:02d}\" {cmd_for(pid)}")
            all_lines.append("timeout /t 1 /nobreak >nul")
        with open(all_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\r\n".join(all_lines) + "\r\n")

        self._log(f"🧩 Згенеровано .bat у: {os.path.abspath(out_dir)}")
        try:
            messagebox.showinfo("Готово", f"Згенеровано .bat у папці:\n{os.path.abspath(out_dir)}\n\nЄ також start_all_10.bat")
        except Exception:
            pass
        open_folder(out_dir)

    def open_launchers_folder(self):
        out_dir = os.path.abspath(LAUNCHERS_DIR)
        os.makedirs(out_dir, exist_ok=True)
        open_folder(out_dir)

    # =======================
    # File open actions
    # =======================
    def open_files_folder(self):
        folder = os.path.abspath(os.getcwd())
        self._log(f"📁 Відкриваю папку: {folder}")
        open_folder(folder)

    def open_profiles_folder(self):
        folder = os.path.abspath(PROFILES_ROOT)
        os.makedirs(folder, exist_ok=True)
        self._log(f"📁 Відкриваю папку профілів: {folder}")
        open_folder(folder)

    def open_valid_file(self):
        self._log("📄 Відкриваю valid.txt")
        open_file_in_default_app(VALID_FILE)

    def open_regsoon_file(self):
        self._log("🕒 Відкриваю regsoon.txt")
        open_file_in_default_app(REGSOON_FILE)

    def open_numbers_file(self):
        self._log("🧾 Відкриваю numbers.txt")
        open_file_in_default_app(NUMBERS_FILE)

    # =======================
    # Prefixes apply
    # =======================
    def apply_prefixes(self, silent: bool = False):
        parsed = parse_prefixes(self.prefixes_text.get())
        if not parsed:
            if not silent:
                messagebox.showwarning("Префікси", "Нема валідних префіксів (треба 2 цифри). Повертаю дефолтні.")
            self.prefixes_list = list(DEFAULT_PREFIXES)
            self.prefixes_text.set(", ".join(self.prefixes_list))
        else:
            self.prefixes_list = parsed
        try:
            self.prefix_hint.configure(text=f"Поточні: {', '.join(self.prefixes_list)}")
        except Exception:
            pass
        self._log(f"🔧 Префікси генератора: {', '.join(self.prefixes_list)}")

    # =======================
    # UI UTIL
    # =======================
    def _set_running_ui(self, running: bool):
        def _u():
            if running:
                self.btn_start.configure(state="disabled")
                self.btn_stop.configure(state="normal")
            else:
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
        self.root.after(0, _u)

    def _set_status(self, text: str):
        self.root.after(0, lambda: self.status_text.set(text))

    def _log(self, msg: str):
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(0, _append)

    def clear_logs(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def copy_logs(self):
        text = self.log_box.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._log("📋 Логи скопійовано в буфер обміну")

    def _update_badges(self):
        def _u():
            self.badge_valid_var.set(f"VALID: {self.valid_count}")
            self.badge_regsoon_var.set(f"REGSOON: {self.regsoon_count}")
            self.badge_skip_var.set(f"SKIP: {self.skipped_count}")
        self.root.after(0, _u)

    def _update_cards(self, avg_text="-"):
        def _u():
            self.card_valid._num.configure(text=str(self.valid_count))
            self.card_skip._num.configure(text=str(self.skipped_count))
            self.card_regsoon._num.configure(text=str(self.regsoon_count))
            self.card_rate._num.configure(text=str(avg_text))
        self.root.after(0, _u)
        self._update_badges()

    def _update_progress(self, done: int, total: int):
        def _u():
            if total <= 0:
                self.progress_caption.set(f"{done} / ∞")
                if str(self.pbar["mode"]) != "indeterminate":
                    self.pbar.configure(mode="indeterminate")
                    self.pbar.start(12)
            else:
                self.progress_caption.set(f"{done} / {total}")
                if str(self.pbar["mode"]) != "determinate":
                    self.pbar.stop()
                    self.pbar.configure(mode="determinate")
                self.pbar["value"] = (done / total) * 100.0 if total else 0
        self.root.after(0, _u)

    def _finish_progressbar(self):
        def _u():
            try:
                if str(self.pbar["mode"]) == "indeterminate":
                    self.pbar.stop()
                    self.pbar.configure(mode="determinate")
                    self.pbar["value"] = 0
            except Exception:
                pass
        self.root.after(0, _u)

    def _update_eta(self):
        if not self.run_started_at:
            return
        elapsed = time.time() - self.run_started_at
        done = max(0, self.done_count)
        avg = None if done <= 0 else (elapsed / done)
        avg_txt = "-" if avg is None else fmt_duration(avg)

        if self.total_count and self.total_count > 0 and avg is not None:
            left = max(0, self.total_count - done)
            eta = avg * left
            eta_txt = fmt_duration(eta)
        else:
            eta_txt = "∞" if self.total_count == 0 else "-"

        wait_s = self.get_services_wait()
        mode = self.mode.get()
        mode_name = "Швидко" if mode == "speed" else ("Надійно" if mode == "accuracy" else "Кастом")

        def _u():
            self.eta_caption.set(f"ETA: {eta_txt} | Пройшло: {fmt_duration(elapsed)} | Режим: {mode_name} ({wait_s:.1f}с)")
        self.root.after(0, _u)
        self._update_cards(avg_txt)

    # =======================
    # SETTINGS GETTERS
    # =======================
    def get_services_wait(self):
        try:
            mode = self.mode.get()
            if mode == "speed":
                return SPEED_WAIT_SECONDS
            if mode == "accuracy":
                return ACCURACY_WAIT_SECONDS
            return max(0.3, float(self.custom_seconds.get()))
        except Exception:
            return 2.0

    def get_pause(self):
        try:
            return max(0.0, float(self.pause_seconds.get()))
        except Exception:
            return 0.3

    def get_save_every_n(self):
        try:
            return max(1, int(self.save_every_n.get()))
        except Exception:
            return 500

    def get_ui_every_n(self):
        try:
            return max(1, int(self.ui_every_n.get()))
        except Exception:
            return 20

    def get_log_every_n(self):
        try:
            return max(1, int(self.log_every_n.get()))
        except Exception:
            return 50

    # =======================
    # TURBO CACHED ELEMENT GETTERS
    # =======================
    def _cache_reset(self):
        self._cached["msisdn"] = None
        self._cached["search_btn"] = None

    def get_msisdn_el(self, driver):
        el = self._cached.get("msisdn")
        try:
            if el and el.is_displayed():
                return el
        except Exception:
            pass
        el = driver.find_element(By.ID, "msisdn")
        self._cached["msisdn"] = el
        return el

    def get_search_btn_el(self, driver):
        el = self._cached.get("search_btn")
        try:
            if el and el.is_displayed():
                return el
        except Exception:
            pass
        el = driver.find_element(By.XPATH, "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Пошук']]")
        self._cached["search_btn"] = el
        return el

    # =======================
    # SELENIUM HELPERS
    # =======================
    def js_click(self, driver, el):
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        driver.execute_script("arguments[0].click();", el)

    def wait_client_button(self, driver):
        return WebDriverWait(driver, WAIT_UI_SECONDS, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH, "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"))
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
            self._log(" ⚠ Виявлено екран «Помилка» → натискаю Ок/ОК/OK/Добре")
            try:
                self.click_ok_anywhere(driver, timeout=2)
                time.sleep(0.10)
            except Exception:
                pass
            self._cache_reset()
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
        try:
            inp = self.get_msisdn_el(driver)
        except Exception:
            self._cache_reset()
            inp = wait.until(EC.element_to_be_clickable((By.ID, "msisdn")))
            self._cached["msisdn"] = inp

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
        try:
            btn = self.get_search_btn_el(driver)
            self.js_click(driver, btn)
            return
        except Exception:
            self._cache_reset()

        btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Пошук']]")))
        self._cached["search_btn"] = btn
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
            EC.element_to_be_clickable((By.XPATH, "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація стартового пакету']]"))
        )
        self.js_click(driver, el)

    def click_register(self, driver, timeout=6):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH, "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Зареєструвати']]"))
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
    # Generator
    # =======================
    def gen_next_number(self) -> str:
        if len(self.gen_recent) > 200_000:
            self.gen_recent.clear()
        prefs = self.prefixes_list if self.prefixes_list else DEFAULT_PREFIXES
        while True:
            pref = random.choice(prefs)
            tail = f"{random.randint(0, 9_999_999):07d}"
            num = pref + tail
            if num not in self.gen_recent:
                self.gen_recent.add(num)
                return num

    # =======================
    # CHECKPOINTS
    # =======================
    def _flush_buffers(self):
        if self.valid_buf:
            append_lines(VALID_FILE, self.valid_buf)
            self.valid_buf.clear()
        if self.regsoon_buf:
            append_lines(REGSOON_FILE, self.regsoon_buf)
            self.regsoon_buf.clear()

    def checkpoint_save_filemode(self):
        self._flush_buffers()
        rewrite_numbers_file(self.file_lines, self.to_delete_numbers, self.keep_non_numbers.get())
        self._log(f"💾 Checkpoint: збережено прогрес (кожні {self.get_save_every_n()} номерів)")

    def checkpoint_save_generator(self):
        self._flush_buffers()
        self._log("💾 Checkpoint: збережено valid.txt/regsoon.txt (режим генератора)")

    # =======================
    # RUN CONTROL
    # =======================
    def start(self):
        if self.worker and self.worker.is_alive():
            return

        self.stop_event.clear()
        self._set_running_ui(True)

        self.valid_count = 0
        self.skipped_count = 0
        self.regsoon_count = 0
        self.already_count = 0
        self.done_count = 0
        self.total_count = 0
        self.run_started_at = time.time()

        self._update_cards("-")
        self._set_status("Працюю…")
        self._log("▶ Запуск…")
        self.apply_prefixes(silent=True)

        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()
        self.nb.select(self.tab_run)

    def stop(self):
        self.stop_event.set()
        self._set_status("Зупинка…")
        self._log("⏹ Stop натиснуто — зупиняю…")

    # =======================
    # MAIN WORKER
    # =======================
    def run(self):
        pause = self.get_pause()
        save_every = self.get_save_every_n()
        wait_seconds = self.get_services_wait()
        ui_n = self.get_ui_every_n()
        log_n = self.get_log_every_n()

        options = webdriver.ChromeOptions()
        options.add_argument("--disable-notifications")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-sync")
        options.add_argument("--metrics-recording-only")
        options.add_argument("--disable-features=Translate,BackForwardCache")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")

        options.add_argument("--use-gl=angle")
        options.add_argument("--use-angle=default")
        options.add_argument("--enable-gpu-rasterization")
        options.add_argument("--enable-zero-copy")
        options.add_argument("--disable-software-rasterizer")

        # keep background windows fast (reduce 3s vs 6s)
        options.add_argument("--disable-background-timer-throttling")
        options.add_argument("--disable-backgrounding-occluded-windows")
        options.add_argument("--disable-renderer-backgrounding")

        # dedicated profile per instance
        if self.use_chrome_profile.get():
            pid = max(1, int(self.profile_id.get()))
            prof_dir = os.path.abspath(os.path.join(PROFILES_ROOT, f"profile_{pid:02d}"))
            os.makedirs(prof_dir, exist_ok=True)
            options.add_argument(f"--user-data-dir={prof_dir}")
            options.add_argument("--profile-directory=Default")

        options.page_load_strategy = "eager"
        options.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications": 2})

        driver = None
        try:
            driver = webdriver.Chrome(options=options)
            driver.implicitly_wait(0)

            # TURBO: block ONLY trackers/video (NOT images)
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                driver.execute_cdp_cmd("Network.setBlockedURLs", {
                    "urls": ["*doubleclick*", "*googletagmanager*", "*google-analytics*", "*.mp4", "*.webm", "*.avi"]
                })
            except Exception:
                pass

            wait_login = WebDriverWait(driver, WAIT_LOGIN_SECONDS, poll_frequency=POLL)
            driver.get(URL)
            self._log("Очікую логін/2FA/капчу… (картинки увімкнено)")

            wait_login.until(EC.presence_of_element_located((
                By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            )))
            self._log("✅ Авторизація OK")

            generator_mode = bool(self.use_generator.get())
            if generator_mode:
                self.total_count = 0
                self._log("🧩 Режим генератора: нескінченно, доки не натиснеш Stop або не знімеш галочку.")
            else:
                self.file_lines, items = load_lines_with_numbers(NUMBERS_FILE)
                if not items:
                    self.root.after(0, lambda: messagebox.showerror("Помилка", "numbers.txt не містить жодного валідного номера"))
                    return
                items_iter = list(reversed(items)) if self.order.get() == "end" else list(items)
                self.total_count = len(items_iter)
                self.to_delete_numbers = set()

            self._update_progress(0, self.total_count)
            self.run_started_at = time.time()

            def maybe_update_ui():
                if (self.done_count % ui_n) == 0:
                    self._update_eta()

            def maybe_log(msg: str, force: bool = False):
                if force or (self.done_count % log_n) == 0:
                    self._log(msg)

            def process_one(number: str, line_info: str = ""):
                try:
                    if generator_mode and not self.use_generator.get():
                        self._log("🛑 Галочка генератора знята — зупиняю.")
                        self.stop_event.set()
                        return False
                    if self.stop_event.is_set():
                        return False

                    if (self.done_count % ui_n) == 0:
                        self._set_status(f"380{number}")

                    maybe_log(f"→ 380{number}" + (f" | рядок: {line_info}" if line_info else ""))

                    wait = self.back_to_home_and_open_client(driver)
                    self.set_number_safe(driver, wait, number)

                    if not self.wait_search_ready(driver, timeout=WAIT_UI_SECONDS):
                        self.skipped_count += 1
                        maybe_log(" ⚠ Пошук не активувався → пропуск")
                        maybe_update_ui()
                        return True

                    self.click_search(driver, wait)
                    self.handle_error_screen_once(driver)

                    services = self.wait_services_only_fast(driver, wait_seconds)
                    has_start_pack = self.has_start_pack_fast(driver)

                    # REGSOON
                    if has_start_pack and not services:
                        if self.write_regsoon.get():
                            self.regsoon_buf.append(number)
                            self.regsoon_count += 1
                            self._log(" 🕒 RegSoon: є старт.пакет, але нема «Реєстрація послуг» → у буфер")
                        else:
                            self._log(" 🕒 RegSoon умова спрацювала, але запис вимкнено → пропуск")
                        self.skipped_count += 1
                        maybe_update_ui()
                        return True

                    if (not services) and (not has_start_pack):
                        self.skipped_count += 1
                        maybe_log(" ⏭ пропуск (нема «Реєстрація послуг» і нема старт.пакету)")
                        maybe_update_ui()
                        return True

                    if services and has_start_pack:
                        self.click_start_pack(driver)
                        time.sleep(0.14)
                        self.click_register(driver)

                        already = self.wait_already_error_short_fast(driver, seconds=1.1)
                        try:
                            self.click_ok_anywhere(driver, timeout=4)
                        except Exception:
                            pass

                        if already:
                            self.already_count += 1
                            self._log(" 🟡 Номер вже було зареєстровано → видалити з numbers.txt")
                            if not generator_mode:
                                self.to_delete_numbers.add(number)
                        else:
                            self.valid_count += 1
                            self._log(" ✔ Зареєстровано (VALID) → видалити з numbers.txt")
                            self.valid_buf.append(number)
                            if not generator_mode:
                                self.to_delete_numbers.add(number)

                        maybe_update_ui()
                        return True

                    self.skipped_count += 1
                    maybe_log(" ⏭ незрозумілий стан → пропуск")
                    maybe_update_ui()
                    return True

                except (TimeoutException, StaleElementReferenceException) as e:
                    self.skipped_count += 1
                    maybe_log(f" ⚠ Selenium timeout/stale → SKIP ({type(e).__name__})", force=True)
                    self._cache_reset()
                    maybe_update_ui()
                    return True

                except WebDriverException as e:
                    self.skipped_count += 1
                    msg = e.msg if hasattr(e, "msg") else str(e)
                    self._log(f" ⚠ WebDriverException → SKIP: {msg[:220]}")
                    self._cache_reset()
                    maybe_update_ui()
                    return True

            if generator_mode:
                i = 0
                while not self.stop_event.is_set():
                    i += 1
                    number = self.gen_next_number()
                    ok = process_one(number)
                    if not ok:
                        break
                    self.done_count += 1
                    self._update_progress(self.done_count, self.total_count)
                    time.sleep(pause)
                    if i % save_every == 0:
                        self.checkpoint_save_generator()
                self.checkpoint_save_generator()
            else:
                items_iter = list(reversed(items)) if self.order.get() == "end" else list(items)
                for idx, it in enumerate(items_iter, 1):
                    if self.stop_event.is_set():
                        break
                    number = it["number"]
                    line_info = it.get("line", "")
                    ok = process_one(number, line_info=line_info)
                    if not ok:
                        break
                    self.done_count += 1
                    self._update_progress(self.done_count, self.total_count)
                    time.sleep(pause)
                    if idx % save_every == 0:
                        self.checkpoint_save_filemode()
                self.checkpoint_save_filemode()

        except Exception as e:
            self._log(f"❌ Критична помилка: {type(e).__name__}: {e}")

        finally:
            try:
                self._flush_buffers()
            except Exception:
                pass
            try:
                if driver:
                    driver.quit()
            except Exception:
                pass
            self._set_status("Готово")
            self._set_running_ui(False)
            self._finish_progressbar()
            self._update_eta()
            self._log("✅ Готово. Прогрес збережено.")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
