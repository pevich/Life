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

DEFAULT_PREFIXES = ["67", "68", "77", "96", "97", "98", "39", "50", "66", "95", "99", "75", "63", "73", "93"]


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
    """
    Accepts: "67,68 77;96" etc.
    Returns list of unique 2-digit prefixes (strings), preserving order.
    """
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


# =======================
# APP
# =======================

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Firk — Lifecell Helper")
        self.root.geometry("1180x860")
        self.root.minsize(1080, 760)

        self._apply_win11_theme()

        # ---- State vars
        self.status_text = tk.StringVar(value="Готово")

        # ✅ default = reliable
        self.mode = tk.StringVar(value="accuracy")  # speed / accuracy / custom
        self.custom_seconds = tk.DoubleVar(value=ACCURACY_WAIT_SECONDS)

        self.pause_seconds = tk.DoubleVar(value=0.3)
        self.save_every_n = tk.IntVar(value=20)

        self.order = tk.StringVar(value="start")
        self.keep_non_numbers = tk.BooleanVar(value=True)

        self.write_regsoon = tk.BooleanVar(value=True)
        self.use_generator = tk.BooleanVar(value=False)

        # prefixes input
        self.prefixes_text = tk.StringVar(value=", ".join(DEFAULT_PREFIXES))
        self.prefixes_list = list(DEFAULT_PREFIXES)

        # runtime
        self.stop_event = threading.Event()
        self.worker = None

        self.valid_count = 0
        self.skipped_count = 0
        self.already_count = 0

        self.run_started_at = None
        self.done_count = 0
        self.total_count = 0  # 0 => infinite for UI

        # generator recent cache to avoid repeats
        self.gen_recent = set()

        # file mode buffers
        self.file_lines = []
        self.to_delete_numbers = set()
        self.valid_buf = []

        # ---- UI
        self._build_ui()

    # =======================
    # THEME / STYLES (Win11-ish)
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
        ttk.Label(left, text="Автоматизація перевірки/реєстрації • Win11 UI", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

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
        # ---- colored badges row (clickable)
        badges_frame = ttk.Frame(self.tab_run)
        badges_frame.pack(fill="x", pady=(0, 10))

        self._c_valid_bg = "#1F9D55"
        self._c_already_bg = "#D9A400"
        self._c_skip_bg = "#6B7280"
        self._c_badge_fg = "#FFFFFF"

        self.badge_valid_var = tk.StringVar(value="VALID: 0")
        self.badge_already_var = tk.StringVar(value="ALREADY: 0")
        self.badge_skip_var = tk.StringVar(value="SKIP: 0")

        self.badge_valid = tk.Label(
            badges_frame, textvariable=self.badge_valid_var,
            bg=self._c_valid_bg, fg=self._c_badge_fg,
            font=("Segoe UI", 10, "bold"), padx=12, pady=6, cursor="hand2"
        )
        self.badge_already = tk.Label(
            badges_frame, textvariable=self.badge_already_var,
            bg=self._c_already_bg, fg=self._c_badge_fg,
            font=("Segoe UI", 10, "bold"), padx=12, pady=6, cursor="hand2"
        )
        self.badge_skip = tk.Label(
            badges_frame, textvariable=self.badge_skip_var,
            bg=self._c_skip_bg, fg=self._c_badge_fg,
            font=("Segoe UI", 10, "bold"), padx=12, pady=6, cursor="hand2"
        )

        self.badge_valid.pack(side="left")
        self.badge_already.pack(side="left", padx=10)
        self.badge_skip.pack(side="left")

        self.badge_valid.bind("<Button-1>", lambda e: self.open_valid_file())
        self.badge_already.bind("<Button-1>", lambda e: self.open_valid_file())
        self.badge_skip.bind("<Button-1>", lambda e: self.open_numbers_file())

        # ---- stat cards row
        row = ttk.Frame(self.tab_run)
        row.pack(fill="x")

        self.card_valid = self._stat_card(row, "VALID", "0")
        self.card_skip = self._stat_card(row, "Пропущено", "0")
        self.card_already = self._stat_card(row, "Вже зареєстр.", "0")
        self.card_rate = self._stat_card(row, "Середній/номер", "-")

        self.card_valid.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.card_skip.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.card_already.pack(side="left", fill="x", expand=True, padx=(0, 10))
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

        hint = (
            "Генератор працює нескінченно, поки увімкнена галочка “Генерувати номери”. "
            "Зупинка: Stop або зняти галочку."
        )
        ttk.Label(actions, text=hint, style="Muted.TLabel", wraplength=980).pack(anchor="w", pady=(10, 0))

    def _build_settings_tab(self):
        grid = ttk.Frame(self.tab_settings)
        grid.pack(fill="both", expand=True)

        left = ttk.Frame(grid)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))

        right = ttk.Frame(grid)
        right.pack(side="left", fill="both", expand=True)

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
        ttk.Spinbox(r3, from_=1, to=500, width=8, textvariable=self.save_every_n).pack(side="left", padx=8)
        ttk.Label(r3, text="(valid.txt + numbers.txt)", style="Muted.TLabel").pack(side="left")

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
    # File open actions
    # =======================
    def open_files_folder(self):
        folder = os.path.abspath(os.getcwd())
        self._log(f"📁 Відкриваю папку: {folder}")
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
            self.badge_already_var.set(f"ALREADY: {self.already_count}")
            self.badge_skip_var.set(f"SKIP: {self.skipped_count}")
        self.root.after(0, _u)

    def _update_cards(self, avg_text="-"):
        def _u():
            self.card_valid._num.configure(text=str(self.valid_count))
            self.card_skip._num.configure(text=str(self.skipped_count))
            self.card_already._num.configure(text=str(self.already_count))
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
            self.eta_caption.set(
                f"ETA: {eta_txt} | Пройшло: {fmt_duration(elapsed)} | Режим: {mode_name} ({wait_s:.1f}с)"
            )
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
            return ACCURACY_WAIT_SECONDS

    def get_pause(self):
        try:
            return max(0.0, float(self.pause_seconds.get()))
        except Exception:
            return 0.3

    def get_save_every_n(self):
        try:
            return max(1, int(self.save_every_n.get()))
        except Exception:
            return 20

    # =======================
    # SELENIUM HELPERS
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
            time.sleep(0.12)
            try:
                return self.wait_msisdn_ready(driver)
            except Exception:
                pass

        self.click_client(driver)
        time.sleep(0.12)
        return self.wait_msisdn_ready(driver)

    def wait_search_ready(self, driver, timeout=WAIT_UI_SECONDS) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.stop_event.is_set():
                return False

            self.handle_error_screen_once(driver)

            btns = driver.find_elements(By.XPATH,
                "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Пошук']]"
            )
            if btns:
                cls = (btns[0].get_attribute("class") or "")
                if "mat-button-disabled" not in cls:
                    return True
            time.sleep(POLL)
        return False

    def set_number_safe(self, driver, wait, number):
        inp = wait.until(EC.element_to_be_clickable((By.ID, "msisdn")))
        full = "380" + number
        try:
            inp.click()
            inp.send_keys(Keys.CONTROL, "a")
            inp.send_keys(Keys.BACKSPACE)
            time.sleep(0.05)
        except Exception:
            pass

        driver.execute_script(
            """
            const el = arguments[0];
            const v = arguments[1];
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
            el.focus();
            setter.call(el,'');
            el.dispatchEvent(new InputEvent('input',{bubbles:true}));
            setter.call(el,v);
            el.dispatchEvent(new InputEvent('input',{bubbles:true}));
            el.dispatchEvent(new Event('change',{bubbles:true}));
            """, inp, full
        )
        self.wait_search_ready(driver, timeout=3)

    def click_search(self, driver, wait):
        btn = wait.until(EC.element_to_be_clickable((By.XPATH,
            "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Пошук']]"
        )))
        self.js_click(driver, btn)

    # ---------- FAST JS checks ----------
    def js_has_label_text(self, driver, text_value: str) -> bool:
        return bool(driver.execute_script(
            """
            const t = arguments[0];
            const nodes = document.querySelectorAll('div.label');
            for (const n of nodes) {
              if ((n.textContent || '').trim() === t) return true;
            }
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
            """
            const h = document.querySelector('h1');
            return h && (h.textContent || '').trim() === 'Помилка';
            """
        ))

    def click_ok_anywhere(self, driver, timeout=2):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH, "//button[.//span[normalize-space(.)='Ок']]"))
        )
        self.js_click(driver, btn)

    def handle_error_screen_once(self, driver):
        if self.has_error_screen(driver):
            self._log("  ⚠ Виявлено екран «Помилка» → натискаю Ок")
            try:
                self.click_ok_anywhere(driver, timeout=2)
                time.sleep(0.12)
            except Exception:
                pass
            return True
        return False

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

    # ---------- generator ----------
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
    def checkpoint_save_filemode(self):
        if self.valid_buf:
            append_lines(VALID_FILE, self.valid_buf)
            self.valid_buf.clear()

        rewrite_numbers_file(
            original_lines=self.file_lines,
            to_delete_numbers=self.to_delete_numbers,
            keep_non_numbers=self.keep_non_numbers.get()
        )
        self._log(f"💾 Checkpoint: збережено прогрес (кожні {self.get_save_every_n()} номерів)")

    def checkpoint_save_generator(self):
        if self.valid_buf:
            append_lines(VALID_FILE, self.valid_buf)
            self.valid_buf.clear()
            self._log("💾 Checkpoint: збережено valid.txt (режим генератора)")

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
        self.already_count = 0
        self.done_count = 0
        self.total_count = 0
        self.run_started_at = time.time()

        self._update_cards("-")
        self._set_status("Працюю…")
        self._log("▶ Запуск…")

        # apply prefixes once on start (safe)
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

        # setup chrome
        options = webdriver.ChromeOptions()
        options.add_argument("--disable-notifications")
        options.add_argument("--start-maximized")

        # speed flags (без вимкнення картинок)
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-default-apps")
        options.add_argument("--disable-sync")
        options.add_argument("--metrics-recording-only")
        options.add_argument("--disable-features=Translate,BackForwardCache")

        # gpu flags
        options.add_argument("--use-gl=angle")
        options.add_argument("--use-angle=default")
        options.add_argument("--enable-gpu-rasterization")
        options.add_argument("--enable-zero-copy")
        options.add_argument("--disable-software-rasterizer")

        options.page_load_strategy = "eager"
        options.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications": 2})

        driver = None

        try:
            driver = webdriver.Chrome(options=options)
            driver.implicitly_wait(0)

            wait_login = WebDriverWait(driver, WAIT_LOGIN_SECONDS, poll_frequency=POLL)

            driver.get(URL)
            self._log("Очікую логін/2FA/капчу…")

            wait_login.until(EC.presence_of_element_located((By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            )))
            self._log("✅ Авторизація OK")

            # choose mode
            generator_mode = bool(self.use_generator.get())
            if generator_mode:
                self.total_count = 0  # infinite
                self._log("🧩 Режим генератора: нескінченно, доки не натиснеш Stop або не знімеш галочку.")
            else:
                self.file_lines, items = load_lines_with_numbers(NUMBERS_FILE)
                if not items:
                    self.root.after(0, lambda: messagebox.showerror("Помилка", "numbers.txt не містить жодного валідного номера"))
                    return

                items_iter = list(reversed(items)) if self.order.get() == "end" else list(items)
                self.total_count = len(items_iter)
                self.to_delete_numbers = set()

            # init UI progress
            self._update_progress(0, self.total_count)
            self.run_started_at = time.time()

            # main loop helper
            def process_one(number: str, line_info: str = ""):
                # stop if generator checkbox turned off mid-run
                if generator_mode and not self.use_generator.get():
                    self._log("🛑 Галочка генератора знята — зупиняю.")
                    self.stop_event.set()
                    return False

                if self.stop_event.is_set():
                    return False

                self._set_status(f"380{number}")
                if line_info:
                    self._log(f"→ 380{number} | рядок: {line_info}")
                else:
                    self._log(f"→ 380{number}")

                wait = self.back_to_home_and_open_client(driver)
                self.set_number_safe(driver, wait, number)

                # чекати активну кнопку
                if not self.wait_search_ready(driver, timeout=WAIT_UI_SECONDS):
                    self.skipped_count += 1
                    self._log("  ⚠ Пошук не активувався → пропуск")
                    self._update_eta()
                    return True

                self.click_search(driver, wait)
                self.handle_error_screen_once(driver)

                services = self.wait_services_only_fast(driver, wait_seconds)
                has_start_pack = self.has_start_pack_fast(driver)

                # 🟡 ВАРІАНТ: є стартовий пакет, але нема "Реєстрація послуг"
                if has_start_pack and not services:
                    if self.write_regsoon.get():
                        with open(REGSOON_FILE, "a", encoding="utf-8") as f:
                            f.write(number + "\n")
                        self._log("  🕒 Є «Реєстрація стартового пакету», але нема «Реєстрація послуг» → RegSoon")
                    else:
                        self._log("  🕒 Є «Реєстрація стартового пакету», але нема «Реєстрація послуг» → пропуск (RegSoon вимкнено)")
                    self.skipped_count += 1
                    self._update_eta()
                    return True

                # ❌ нема "Реєстрація послуг" і нема стартового пакету
                if (not services) and (not has_start_pack):
                    self.skipped_count += 1
                    self._log("  ⏭ пропуск (нема «Реєстрація послуг» і нема старт.пакету)")
                    self._update_eta()
                    return True

                # ✅ нормальна реєстрація
                if services and has_start_pack:
                    self.click_start_pack(driver)
                    time.sleep(0.18)
                    self.click_register(driver)

                    already = self.wait_already_error_short_fast(driver, seconds=1.1)

                    try:
                        self.click_ok_anywhere(driver, timeout=4)
                    except Exception:
                        pass

                    if already:
                        self.already_count += 1
                        self._log("  🟡 Номер вже було зареєстровано → видалити з numbers.txt")
                        if not generator_mode:
                            self.to_delete_numbers.add(number)
                    else:
                        self.valid_count += 1
                        self._log("  ✔ Зареєстровано (VALID) → видалити з numbers.txt")
                        self.valid_buf.append(number)
                        if not generator_mode:
                            self.to_delete_numbers.add(number)

                    self._update_eta()
                    return True

                # ⚠ інші комбінації
                self.skipped_count += 1
                self._log("  ⏭ незрозумілий стан → пропуск")
                self._update_eta()
                return True

            # ---- RUN (file or generator)
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

                # final save for generator
                self.checkpoint_save_generator()

            else:
                items_iter = list(reversed(load_lines_with_numbers(NUMBERS_FILE)[1])) if self.order.get() == "end" else load_lines_with_numbers(NUMBERS_FILE)[1]
                # use already loaded items (avoid re-reading): rebuild from self.file_lines to keep stable mapping
                # better: iterate using the items we loaded earlier; but we already set total_count based on items_iter above.
                # We'll re-load for safety in case file changed; if you don't want that, delete 3 lines above and keep the earlier 'items_iter'.
                # For now: keep it consistent with the file content we started with:
                _, items = load_lines_with_numbers(NUMBERS_FILE)
                items_iter = list(reversed(items)) if self.order.get() == "end" else list(items)
                self.total_count = len(items_iter)
                self._update_progress(0, self.total_count)

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

                # final save for file mode
                self.checkpoint_save_filemode()

        except Exception as e:
            self._log(f"❌ Критична помилка: {type(e).__name__}: {e}")
        finally:
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
