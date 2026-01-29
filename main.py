import os
import re
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import random
import subprocess
import sys
from queue import Queue, Empty

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

SPEED_WAIT_SECONDS = 1.7
ACCURACY_WAIT_SECONDS = 1.8

DEFAULT_PREFIXES = ["67", "96", "98", "63", "93"]

PROFILES_ROOT = "chrome_profiles"


# =======================
# HELPERS
# =======================
def append_lines(path, lines):
    if not lines:
        return
    with open(path, "a", encoding="utf-8") as f:
        for x in lines:
            f.write(x + "\n")


def open_file_in_default_app(filepath: str):
    filepath = os.path.abspath(filepath)
    if not os.path.exists(filepath):
        with open(filepath, "a", encoding="utf-8"):
            pass
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


# =======================
# APP
# =======================
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Firk — 10 Browsers (Multi)")
        self.root.geometry("1180x860")
        self.root.minsize(1080, 760)

        self.style = ttk.Style(self.root)
        if sys.platform.startswith("win") and "vista" in self.style.theme_names():
            self.style.theme_use("vista")
        elif "clam" in self.style.theme_names():
            self.style.theme_use("clam")
        self.root.option_add("*Font", ("Segoe UI", 10))

        # ---- Defaults as you asked
        self.mode = tk.StringVar(value="custom")
        self.custom_seconds = tk.DoubleVar(value=2.0)

        self.pause_seconds = tk.DoubleVar(value=0.3)

        self.save_every_n = tk.IntVar(value=500)
        self.ui_every_n = tk.IntVar(value=20)
        self.log_every_n = tk.IntVar(value=50)

        self.use_generator = tk.BooleanVar(value=True)

        # multi
        self.workers = tk.IntVar(value=10)          # ✅ 10 browsers in one program
        self.profile_base = tk.IntVar(value=1)      # profiles: base..base+N-1
        self.use_chrome_profile = tk.BooleanVar(value=True)

        # prefixes
        self.prefixes_text = tk.StringVar(value=", ".join(DEFAULT_PREFIXES))
        self.prefixes_list = list(DEFAULT_PREFIXES)

        # runtime state
        self.stop_event = threading.Event()
        self.threads = []
        self.run_started_at = None

        self.valid_count = 0
        self.regsoon_count = 0
        self.skipped_count = 0
        self.already_count = 0
        self.done_count = 0

        # shared generator (no duplicates across workers)
        self.gen_recent = set()
        self.gen_lock = threading.Lock()

        # per-worker small buffers
        self.valid_buf = []
        self.regsoon_buf = []
        self.buf_lock = threading.Lock()

        # UI/log queue (thread-safe)
        self.q = Queue()

        self._build_ui()
        self._tick_ui()

    # =======================
    # UI
    # =======================
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill="x")

        ttk.Label(top, text="Multi-browsers: 1 програма → N Chrome").pack(side="left")
        self.status = tk.StringVar(value="Готово")
        ttk.Label(top, textvariable=self.status).pack(side="right")

        cfg = ttk.LabelFrame(self.root, text="Налаштування", padding=12)
        cfg.pack(fill="x", padx=12, pady=10)

        r1 = ttk.Frame(cfg)
        r1.pack(fill="x")
        ttk.Checkbutton(r1, text="Генерувати номери нескінченно", variable=self.use_generator).pack(side="left")

        r2 = ttk.Frame(cfg)
        r2.pack(fill="x", pady=(10, 0))
        ttk.Label(r2, text="Workers (браузерів):").pack(side="left")
        ttk.Spinbox(r2, from_=1, to=30, width=6, textvariable=self.workers).pack(side="left", padx=8)
        ttk.Label(r2, text="Profile base:").pack(side="left", padx=(18, 0))
        ttk.Spinbox(r2, from_=1, to=999, width=6, textvariable=self.profile_base).pack(side="left", padx=8)
        ttk.Checkbutton(r2, text="Окремі Chrome профілі", variable=self.use_chrome_profile).pack(side="left", padx=18)

        r3 = ttk.Frame(cfg)
        r3.pack(fill="x", pady=(10, 0))
        ttk.Label(r3, text="Префікси:").pack(side="left")
        ttk.Entry(r3, textvariable=self.prefixes_text).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(r3, text="Застосувати", command=self.apply_prefixes).pack(side="left")

        r4 = ttk.Frame(cfg)
        r4.pack(fill="x", pady=(10, 0))
        ttk.Label(r4, text="Wait services (custom):").pack(side="left")
        ttk.Entry(r4, width=6, textvariable=self.custom_seconds).pack(side="left", padx=8)
        ttk.Label(r4, text="сек").pack(side="left")
        ttk.Label(r4, text="Pause:").pack(side="left", padx=(18, 0))
        ttk.Entry(r4, width=6, textvariable=self.pause_seconds).pack(side="left", padx=8)

        btns = ttk.Frame(self.root, padding=(12, 0, 12, 0))
        btns.pack(fill="x")
        ttk.Button(btns, text="▶ Start", command=self.start).pack(side="left")
        ttk.Button(btns, text="⏹ Stop", command=self.stop).pack(side="left", padx=10)
        ttk.Button(btns, text="valid.txt", command=lambda: open_file_in_default_app(VALID_FILE)).pack(side="left", padx=10)
        ttk.Button(btns, text="regsoon.txt", command=lambda: open_file_in_default_app(REGSOON_FILE)).pack(side="left")

        stats = ttk.LabelFrame(self.root, text="Статистика", padding=12)
        stats.pack(fill="x", padx=12, pady=10)
        self.stat_var = tk.StringVar(value="VALID: 0 | REGSOON: 0 | SKIP: 0 | DONE: 0")
        ttk.Label(stats, textvariable=self.stat_var).pack(anchor="w")

        logs = ttk.LabelFrame(self.root, text="Логи", padding=12)
        logs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log_box = tk.Text(logs, height=18, wrap="word")
        self.log_box.pack(fill="both", expand=True)

    def _tick_ui(self):
        # consume queue
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log_box.insert("end", payload + "\n")
                    self.log_box.see("end")
                elif kind == "stat":
                    self.stat_var.set(payload)
                elif kind == "status":
                    self.status.set(payload)
        except Empty:
            pass
        self.root.after(80, self._tick_ui)

    def _ui_stat(self):
        self.q.put(("stat", f"VALID: {self.valid_count} | REGSOON: {self.regsoon_count} | SKIP: {self.skipped_count} | DONE: {self.done_count}"))

    def _log(self, msg):
        self.q.put(("log", msg))

    def _set_status(self, msg):
        self.q.put(("status", msg))

    # =======================
    # Settings
    # =======================
    def apply_prefixes(self):
        parsed = parse_prefixes(self.prefixes_text.get())
        if not parsed:
            self.prefixes_list = list(DEFAULT_PREFIXES)
            self.prefixes_text.set(", ".join(self.prefixes_list))
        else:
            self.prefixes_list = parsed
        self._log(f"🔧 Префікси: {', '.join(self.prefixes_list)}")

    def get_services_wait(self):
        try:
            return max(0.3, float(self.custom_seconds.get()))
        except Exception:
            return 2.0

    def get_pause(self):
        try:
            return max(0.0, float(self.pause_seconds.get()))
        except Exception:
            return 0.3

    # =======================
    # Shared generator (no repeats across workers)
    # =======================
    def gen_next_number_shared(self) -> str:
        # one lock = guarantee unique between threads
        with self.gen_lock:
            if len(self.gen_recent) > 200_000:
                self.gen_recent.clear()

            prefs = self.prefixes_list or DEFAULT_PREFIXES
            while True:
                pref = random.choice(prefs)
                tail = f"{random.randint(0, 9_999_999):07d}"
                num = pref + tail
                if num not in self.gen_recent:
                    self.gen_recent.add(num)
                    return num

    # =======================
    # Selenium helpers (same logic as your code)
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
    # Multi-worker driver loop
    # =======================
    def build_driver(self, profile_id: int):
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
            prof_dir = os.path.abspath(os.path.join(PROFILES_ROOT, f"profile_{profile_id:02d}"))
            os.makedirs(prof_dir, exist_ok=True)
            options.add_argument(f"--user-data-dir={prof_dir}")
            options.add_argument("--profile-directory=Default")

        driver = webdriver.Chrome(options=options)
        driver.implicitly_wait(0)

        # block trackers/video only (NOT images)
        try:
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd("Network.setBlockedURLs", {
                "urls": ["*doubleclick*", "*googletagmanager*", "*google-analytics*", "*.mp4", "*.webm", "*.avi"]
            })
        except Exception:
            pass

        return driver

    def worker_loop(self, wid: int, profile_id: int):
        driver = None
        local_valid = []
        local_regsoon = []
        local_count = 0

        wait_seconds = self.get_services_wait()
        pause = self.get_pause()

        try:
            driver = self.build_driver(profile_id)
            driver.get(URL)

            self._log(f"[W{wid}] Очікую логін/2FA/капчу… профіль={profile_id}")
            wait_login = WebDriverWait(driver, WAIT_LOGIN_SECONDS, poll_frequency=POLL)
            wait_login.until(EC.presence_of_element_located((By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            )))
            self._log(f"[W{wid}] ✅ Авторизація OK")

            while not self.stop_event.is_set():
                number = self.gen_next_number_shared()
                local_count += 1

                # light status update
                if (local_count % 10) == 0:
                    self._set_status(f"W{wid}: 380{number}")

                try:
                    wait = self.back_to_home_and_open_client(driver)
                    self.set_number_safe(driver, wait, number)

                    if not self.wait_search_ready(driver, timeout=WAIT_UI_SECONDS):
                        self.skipped_count += 1
                        continue

                    self.click_search(driver, wait)
                    self.handle_error_screen_once(driver)

                    services = self.wait_services_only_fast(driver, wait_seconds)
                    has_start_pack = self.has_start_pack_fast(driver)

                    # regsoon
                    if has_start_pack and not services:
                        if True:
                            local_regsoon.append(number)
                            self.regsoon_count += 1
                        self.skipped_count += 1
                        continue

                    if (not services) and (not has_start_pack):
                        self.skipped_count += 1
                        continue

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
                        else:
                            self.valid_count += 1
                            local_valid.append(number)

                    else:
                        self.skipped_count += 1

                except (TimeoutException, StaleElementReferenceException, WebDriverException):
                    self.skipped_count += 1

                # shared counters
                self.done_count += 1

                # periodic flush per worker
                if local_count % self.save_every_n.get() == 0:
                    with self.buf_lock:
                        if local_valid:
                            append_lines(VALID_FILE, local_valid)
                            local_valid.clear()
                        if local_regsoon:
                            append_lines(REGSOON_FILE, local_regsoon)
                            local_regsoon.clear()
                    self._log(f"[W{wid}] 💾 checkpoint")

                # UI/stat throttling from main loop timer
                if self.done_count % self.ui_every_n.get() == 0:
                    self._ui_stat()

                if pause:
                    time.sleep(pause)

        finally:
            # final flush
            with self.buf_lock:
                if local_valid:
                    append_lines(VALID_FILE, local_valid)
                if local_regsoon:
                    append_lines(REGSOON_FILE, local_regsoon)

            try:
                if driver:
                    driver.quit()
            except Exception:
                pass
            self._log(f"[W{wid}] 🧹 closed")

    # =======================
    # START/STOP
    # =======================
    def start(self):
        if self.threads:
            return

        if not self.use_generator.get():
            messagebox.showwarning("Режим", "Цей multi-варіант зроблено під генератор (нескінченно).")
            return

        self.apply_prefixes()

        n = max(1, int(self.workers.get()))
        base = max(1, int(self.profile_base.get()))

        self.stop_event.clear()
        self.valid_count = self.regsoon_count = self.skipped_count = self.already_count = self.done_count = 0
        self.gen_recent.clear()

        self._set_status("Працюю…")
        self._log(f"▶ Start: workers={n}, profiles={base}..{base+n-1}")

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
        self._log("⏹ Stop натиснуто — зупиняю…")

        # join quickly in background thread (don’t freeze UI)
        def _join():
            for t in self.threads:
                try:
                    t.join(timeout=10)
                except Exception:
                    pass
            self.threads = []
            self._set_status("Готово")
            self._ui_stat()
            self._log("✅ Зупинено. Все збережено.")

        threading.Thread(target=_join, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
