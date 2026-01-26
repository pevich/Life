import os
import re
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

URL = "https://my-ambassador.lifecell.ua"

NUMBERS_FILE = "numbers.txt"
VALID_FILE = "valid.txt"

WAIT_LOGIN_SECONDS = 600
WAIT_UI_SECONDS = 12
POLL = 0.03


# ---------- parsing helpers ----------

def normalize_to_9_digits(raw_digits: str):
    """
    Повертає 9 цифр у форматі lifecell: 9XXXXXXXX (без 380).
    Приймає:
      - 935180140
      - 0935180140 -> 935180140
      - 380935180140 -> 935180140
    """
    d = re.sub(r"\D+", "", raw_digits or "")
    if not d:
        return None

    if d.startswith("380") and len(d) == 12:
        return d[3:]

    # 0 + 9 цифр (наприклад 0935180140)
    if d.startswith("0") and len(d) == 10:
        return d[1:]

    # вже 9 цифр
    if len(d) == 9:
        return d

    return None


def extract_number_from_line(line: str):
    """
    Дістає номер з рядка (різні формати) і повертає 9 цифр або None.
    """
    digits = re.sub(r"\D+", "", line)

    # 380XXXXXXXXX (12)
    m = re.search(r"380\d{9}", digits)
    if m:
        return normalize_to_9_digits(m.group(0))

    # 0XXXXXXXXX (10)
    m = re.search(r"0\d{9}", digits)
    if m:
        return normalize_to_9_digits(m.group(0))

    # 9 цифр
    m = re.search(r"\b\d{9}\b", line)
    if m:
        return normalize_to_9_digits(m.group(0))

    return None


def load_lines_with_numbers(path: str):
    """
    lines: список рядків (без \n) як у файлі
    items: список dict: {idx, line, number} лише для рядків з валідним номером
    (дублі номерів прибираємо, беремо першу появу)
    """
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


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}г {m}хв {s}с"
    if m > 0:
        return f"{m}хв {s}с"
    return f"{s}с"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Lifecell Checker")
        self.root.geometry("1080x800")

        self.status = tk.StringVar(value="Готово")
        self.progress_text = tk.StringVar(value="0 / 0")
        self.count_text = tk.StringVar(value="VALID: 0 | Пропущено: 0 | Уже зареєстровано: 0")
        self.eta_text = tk.StringVar(value="Середній: - | ETA: - | Пройшло: -")

        # порядок
        self.order = tk.StringVar(value="start")  # start / end

        # зберігати рядки без номерів?
        self.keep_non_numbers = tk.BooleanVar(value=True)

        # режими очікування "Реєстрація послуг"
        self.mode = tk.StringVar(value="speed")
        self.speed_seconds = tk.DoubleVar(value=2.0)     # ✅ швидко = 2с
        self.accuracy_seconds = tk.DoubleVar(value=4.0)  # ✅ надійно = 4с
        self.custom_seconds = tk.DoubleVar(value=2.0)

        # пауза між номерами
        self.pause_seconds = tk.DoubleVar(value=1.0)

        self.stop_event = threading.Event()
        self.worker = None

        self.valid_count = 0
        self.skipped_count = 0
        self.already_count = 0

        # для ETA
        self.run_started_at = None
        self.done_count = 0
        self.total_count = 0

        ttk.Label(root, text="Lifecell Checker", font=("Segoe UI", 18, "bold")).pack(pady=10)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=14, pady=4)
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        ttk.Label(bar, textvariable=self.progress_text).pack(side="right")

        cnt = ttk.Frame(root)
        cnt.pack(fill="x", padx=14, pady=(0, 4))
        ttk.Label(cnt, textvariable=self.count_text).pack(side="left")

        eta = ttk.Frame(root)
        eta.pack(fill="x", padx=14, pady=(0, 8))
        ttk.Label(eta, textvariable=self.eta_text).pack(side="left")

        self.pbar = ttk.Progressbar(root, orient="horizontal", mode="determinate", maximum=100)
        self.pbar.pack(fill="x", padx=14, pady=(0, 10))

        opt = ttk.LabelFrame(root, text="Налаштування")
        opt.pack(fill="x", padx=14, pady=(0, 10))

        row0 = ttk.Frame(opt)
        row0.pack(fill="x", padx=10, pady=6)
        ttk.Label(row0, text="Перевіряти:").pack(side="left")
        ttk.Radiobutton(row0, text="З початку", variable=self.order, value="start").pack(side="left", padx=10)
        ttk.Radiobutton(row0, text="З кінця", variable=self.order, value="end").pack(side="left", padx=10)

        row00 = ttk.Frame(opt)
        row00.pack(fill="x", padx=10, pady=6)
        ttk.Checkbutton(
            row00,
            text="Зберігати рядки без номерів (текст/дати) у numbers.txt",
            variable=self.keep_non_numbers
        ).pack(side="left")

        row1 = ttk.Frame(opt)
        row1.pack(fill="x", padx=10, pady=6)
        ttk.Label(row1, text="Очікування «Реєстрація послуг»:").pack(side="left", padx=(0, 10))

        ttk.Radiobutton(row1, text="Швидко", variable=self.mode, value="speed").pack(side="left")
        ttk.Entry(row1, width=6, textvariable=self.speed_seconds).pack(side="left", padx=5)
        ttk.Label(row1, text="сек").pack(side="left", padx=(0, 12))

        ttk.Radiobutton(row1, text="Надійно", variable=self.mode, value="accuracy").pack(side="left")
        ttk.Entry(row1, width=6, textvariable=self.accuracy_seconds).pack(side="left", padx=5)
        ttk.Label(row1, text="сек").pack(side="left", padx=(0, 12))

        ttk.Radiobutton(row1, text="Кастом", variable=self.mode, value="custom").pack(side="left")
        ttk.Entry(row1, width=6, textvariable=self.custom_seconds).pack(side="left", padx=5)
        ttk.Label(row1, text="сек").pack(side="left", padx=(0, 12))

        row2 = ttk.Frame(opt)
        row2.pack(fill="x", padx=10, pady=6)
        ttk.Label(row2, text="Пауза між номерами (сек):").pack(side="left")
        ttk.Entry(row2, width=6, textvariable=self.pause_seconds).pack(side="left", padx=6)

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=14, pady=6)
        self.btn_start = ttk.Button(btns, text="▶ Почати", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="⏹ Стоп", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=10)

        self.log_box = tk.Text(root, height=18)
        self.log_box.pack(fill="both", expand=True, padx=14, pady=10)
        self.log_box.configure(state="disabled")

    def log(self, msg):
        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(0, _append)

    def ui_set_progress(self, i, total):
        def _upd():
            self.progress_text.set(f"{i} / {total}")
            self.pbar["value"] = (i / total) * 100.0 if total else 0
        self.root.after(0, _upd)

    def ui_set_counts(self):
        self.root.after(0, lambda: self.count_text.set(
            f"VALID: {self.valid_count} | Пропущено: {self.skipped_count} | Уже зареєстровано: {self.already_count}"
        ))

    def ui_update_eta(self):
        if not self.run_started_at or self.total_count <= 0:
            return
        elapsed = time.time() - self.run_started_at
        done = max(0, self.done_count)
        left = max(0, self.total_count - done)

        if done <= 0:
            avg = None
            eta = None
        else:
            avg = elapsed / done
            eta = avg * left

        wait_s = self.get_services_wait()
        mode = self.mode.get()
        mode_name = "Швидко" if mode == "speed" else ("Надійно" if mode == "accuracy" else "Кастом")

        avg_txt = "-" if avg is None else fmt_duration(avg)
        eta_txt = "-" if eta is None else fmt_duration(eta)
        el_txt = fmt_duration(elapsed)

        self.root.after(0, lambda: self.eta_text.set(
            f"Середній/номер: {avg_txt} | ETA: {eta_txt} | Пройшло: {el_txt} | Режим: {mode_name} ({wait_s:.1f}с)"
        ))

    def get_services_wait(self):
        try:
            if self.mode.get() == "speed":
                return max(0.3, float(self.speed_seconds.get()))
            if self.mode.get() == "accuracy":
                return max(0.3, float(self.accuracy_seconds.get()))
            return max(0.3, float(self.custom_seconds.get()))
        except Exception:
            return 2.0

    def get_pause(self):
        try:
            return max(0.0, float(self.pause_seconds.get()))
        except Exception:
            return 1.0

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        self.valid_count = 0
        self.skipped_count = 0
        self.already_count = 0
        self.ui_set_counts()

        self.run_started_at = time.time()
        self.done_count = 0
        self.total_count = 0
        self.eta_text.set("Середній: - | ETA: - | Пройшло: 0с")

        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.status.set("Зупинка...")

    # ---------- Selenium helpers ----------

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
            time.sleep(0.4)
            try:
                return self.wait_msisdn_ready(driver)
            except Exception:
                pass

        self.click_client(driver)
        time.sleep(0.4)
        return self.wait_msisdn_ready(driver)

    def set_number_safe(self, driver, wait, number):
        inp = wait.until(EC.element_to_be_clickable((By.ID, "msisdn")))
        full = "380" + number
        try:
            inp.click()
            inp.send_keys(Keys.CONTROL, "a")
            inp.send_keys(Keys.BACKSPACE)
            time.sleep(0.1)
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
        time.sleep(0.15)

    def click_search(self, driver, wait):
        btn = wait.until(EC.element_to_be_clickable((By.XPATH,
            "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Пошук']]"
        )))
        self.js_click(driver, btn)

    def has_services_button(self, driver):
        return bool(driver.find_elements(By.XPATH,
            "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація послуг']]"
        ))

    def has_start_pack_button(self, driver):
        return bool(driver.find_elements(By.XPATH,
            "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація стартового пакету']]"
        ))

    def wait_services_only(self, driver, wait_seconds):
        end = time.time() + wait_seconds
        while time.time() < end:
            if self.has_services_button(driver):
                return True
            time.sleep(POLL)
        return False

    def click_start_pack(self, driver, timeout=8):
        el = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація стартового пакету']]"
            ))
        )
        self.js_click(driver, el)

    def click_register(self, driver, timeout=8):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH,
                "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Зареєструвати']]"
            ))
        )
        self.js_click(driver, btn)

    def click_ok(self, driver, timeout=8):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((By.XPATH,
                "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Ок']]"
            ))
        )
        self.js_click(driver, btn)

    def has_already_registered_error(self, driver):
        return bool(driver.find_elements(By.XPATH,
            "//div[contains(@class,'error-text') and contains(normalize-space(.),'Номер вже було зареєстровано')]"
        ))

    def wait_already_error_short(self, driver, seconds=1.3):
        end = time.time() + seconds
        while time.time() < end:
            if self.has_already_registered_error(driver):
                return True
            time.sleep(POLL)
        return False

    # ---------- MAIN ----------

    def run(self):
        lines, items = load_lines_with_numbers(NUMBERS_FILE)
        if not items:
            messagebox.showerror("Помилка", "numbers.txt не містить жодного валідного номера (9 цифр)")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            return

        items_iter = list(reversed(items)) if self.order.get() == "end" else list(items)

        to_delete_numbers = set()  # VALID + already registered
        valid_buf = []

        wait_seconds = self.get_services_wait()
        pause = self.get_pause()

        self.total_count = len(items_iter)
        self.done_count = 0
        self.ui_update_eta()

        options = webdriver.ChromeOptions()
        options.add_argument("--disable-notifications")
        options.add_argument("--start-maximized")
        options.page_load_strategy = "eager"
        options.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications": 2})

        driver = webdriver.Chrome(options=options)
        wait_login = WebDriverWait(driver, WAIT_LOGIN_SECONDS, poll_frequency=POLL)

        self.ui_set_progress(0, self.total_count)

        try:
            driver.get(URL)
            self.log("Очікую логін/2FA/капчу...")

            wait_login.until(EC.presence_of_element_located((By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            )))
            self.log("Авторизація OK")

            for i, it in enumerate(items_iter, 1):
                if self.stop_event.is_set():
                    break

                number = it["number"]
                self.ui_set_progress(i, self.total_count)
                self.status.set(f"380{number}")
                self.log(f"→ 380{number} | рядок: {it['line']}")

                try:
                    wait = self.back_to_home_and_open_client(driver)
                    self.set_number_safe(driver, wait, number)
                    self.click_search(driver, wait)

                    services = self.wait_services_only(driver, wait_seconds)

                    if not services:
                        self.skipped_count += 1
                        self.ui_set_counts()
                        self.log("  ⏭ пропуск (нема «Реєстрація послуг») — рядок лишається")
                    else:
                        # ✅ якщо є "Реєстрація послуг", але НЕМА "Реєстрація стартового пакету" -> пропуск
                        if not self.has_start_pack_button(driver):
                            self.skipped_count += 1
                            self.ui_set_counts()
                            self.log("  ⏭ Є «Реєстрація послуг», але нема «Реєстрація стартового пакету» → пропуск")
                        else:
                            self.log("  ✅ Є «Реєстрація послуг» + «Стартовий пакет» → Зареєструвати")
                            self.click_start_pack(driver)
                            time.sleep(0.2)
                            self.click_register(driver)

                            already = self.wait_already_error_short(driver, seconds=1.3)
                            self.click_ok(driver)

                            if already:
                                self.already_count += 1
                                self.ui_set_counts()
                                to_delete_numbers.add(number)
                                self.log("  🟡 Номер вже було зареєстровано → видалити номер з numbers.txt")
                            else:
                                self.valid_count += 1
                                self.ui_set_counts()
                                valid_buf.append(number)
                                to_delete_numbers.add(number)
                                self.log("  ✔ Зареєстровано (VALID) → видалити номер з numbers.txt")

                except Exception as e:
                    self.skipped_count += 1
                    self.ui_set_counts()
                    self.log(f"  ⚠ Помилка: {type(e).__name__} (рядок лишається)")

                self.done_count += 1
                self.ui_update_eta()
                time.sleep(pause)

            append_lines(VALID_FILE, valid_buf)

            # перезаписуємо numbers.txt:
            new_lines = []
            for ln in lines:
                num = extract_number_from_line(ln)
                if not num:
                    if self.keep_non_numbers.get():
                        new_lines.append(ln)
                    continue

                if num in to_delete_numbers:
                    continue

                new_lines.append(ln)

            with open(NUMBERS_FILE, "w", encoding="utf-8") as f:
                for ln in new_lines:
                    f.write(ln + "\n")

        finally:
            try:
                driver.quit()
            except Exception:
                pass
            self.status.set("Готово")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.ui_update_eta()
            self.log("Готово. numbers.txt оновлено (видалено VALID + 'вже зареєстровано').")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
