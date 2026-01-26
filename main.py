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


def fmt_seconds(sec: float) -> str:
    sec = max(0, int(round(sec)))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}г {m}хв"
    if m > 0:
        return f"{m}хв {s}с"
    return f"{s}с"


def parse_number_line(line: str):
    m = re.search(r"(380\d{9}|\b\d{9}\b)", line)
    if not m:
        return None
    digits = m.group(1)
    return digits[3:] if digits.startswith("380") else digits


def load_numbers():
    if not os.path.exists(NUMBERS_FILE):
        return []
    out = []
    with open(NUMBERS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            n = parse_number_line(line)
            if n:
                out.append(n)
    return list(dict.fromkeys(out))


def save_numbers(numbers):
    with open(NUMBERS_FILE, "w", encoding="utf-8") as f:
        for n in numbers:
            f.write(n + "\n")


def append_lines(path, lines):
    if not lines:
        return
    with open(path, "a", encoding="utf-8") as f:
        for x in lines:
            f.write(x + "\n")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Lifecell Checker")
        self.root.geometry("980x670")

        self.status = tk.StringVar(value="Готово")
        self.progress_text = tk.StringVar(value="0 / 0")
        self.speed_text = tk.StringVar(value="Середній: - | Залишилось: -")
        self.count_text = tk.StringVar(value="Зареєстровано: 0 | Пропущено: 0")

        self.mode = tk.StringVar(value="speed")   # speed / accuracy / custom
        self.wait_seconds = tk.DoubleVar(value=2.0)
        self.pause_seconds = tk.DoubleVar(value=1.0)
        self.test_only = tk.BooleanVar(value=False)
        self.beep_on_valid = tk.BooleanVar(value=False)
        self.autosave_every = tk.IntVar(value=10)

        self.stop_event = threading.Event()
        self.worker = None

        self.valid_count = 0
        self.skipped_count = 0
        self.run_started_at = None

        ttk.Label(root, text="Lifecell Checker", font=("Segoe UI", 18, "bold")).pack(pady=10)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=14, pady=4)
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        ttk.Label(bar, textvariable=self.progress_text).pack(side="right")

        info = ttk.Frame(root)
        info.pack(fill="x", padx=14, pady=(0, 2))
        ttk.Label(info, textvariable=self.speed_text).pack(side="left")

        cnt = ttk.Frame(root)
        cnt.pack(fill="x", padx=14, pady=(0, 6))
        ttk.Label(cnt, textvariable=self.count_text).pack(side="left")

        self.pbar = ttk.Progressbar(root, orient="horizontal", mode="determinate", maximum=100)
        self.pbar.pack(fill="x", padx=14, pady=(0, 10))

        opt = ttk.LabelFrame(root, text="Налаштування")
        opt.pack(fill="x", padx=14, pady=(0, 10))

        row1 = ttk.Frame(opt)
        row1.pack(fill="x", padx=10, pady=6)
        ttk.Label(row1, text="Режим:").pack(side="left")
        ttk.Radiobutton(row1, text="Швидкість (2с)", value="speed", variable=self.mode).pack(side="left", padx=8)
        ttk.Radiobutton(row1, text="Точність (5с)", value="accuracy", variable=self.mode).pack(side="left", padx=8)
        ttk.Radiobutton(row1, text="Кастом", value="custom", variable=self.mode).pack(side="left", padx=8)
        ttk.Label(row1, text="Чекати (сек):").pack(side="left", padx=(18, 4))
        ttk.Entry(row1, width=6, textvariable=self.wait_seconds).pack(side="left")

        row2 = ttk.Frame(opt)
        row2.pack(fill="x", padx=10, pady=6)
        ttk.Label(row2, text="Пауза між номерами (сек):").pack(side="left")
        ttk.Entry(row2, width=6, textvariable=self.pause_seconds).pack(side="left", padx=6)
        ttk.Checkbutton(row2, text="Тільки перевірка", variable=self.test_only).pack(side="left", padx=14)
        ttk.Checkbutton(row2, text="Beep на VALID", variable=self.beep_on_valid).pack(side="left", padx=14)

        row3 = ttk.Frame(opt)
        row3.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(row3, text="Автозбереження кожні N номерів:").pack(side="left")
        ttk.Entry(row3, width=6, textvariable=self.autosave_every).pack(side="left", padx=6)

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

    def ui_set_status(self, text):
        self.root.after(0, lambda: self.status.set(text))

    def ui_set_progress(self, i, total):
        def _upd():
            self.progress_text.set(f"{i} / {total}")
            self.pbar["value"] = (i / total) * 100.0 if total else 0
        self.root.after(0, _upd)

    def ui_set_speed_eta(self, avg_sec, eta_sec):
        def _upd():
            self.speed_text.set(f"Середній: {avg_sec:.2f}с/номер | Залишилось: ~{fmt_seconds(eta_sec)}")
        self.root.after(0, _upd)

    def ui_set_counts(self):
        self.root.after(0, lambda: self.count_text.set(
            f"Зареєстровано: {self.valid_count} | Пропущено: {self.skipped_count}"
        ))

    def get_services_wait(self) -> float:
        if self.mode.get() == "speed":
            return 2.0
        if self.mode.get() == "accuracy":
            return 5.0
        try:
            return max(0.3, float(self.wait_seconds.get()))
        except Exception:
            return 3.0

    def get_pause(self) -> float:
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
        self.run_started_at = time.time()
        self.valid_count = 0
        self.skipped_count = 0
        self.ui_set_counts()
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
        for _ in range(5):
            if driver.find_elements(By.ID, "msisdn"):
                return self.wait_msisdn_ready(driver)
            self.click_client(driver)
            return self.wait_msisdn_ready(driver)
        self.click_client(driver)
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

    def wait_services_only(self, driver, current_number, wait_seconds):
        end = time.time() + wait_seconds
        while time.time() < end:
            remaining = max(0.0, end - time.time())
            self.ui_set_status(f"380{current_number} | Перевірка… ще {remaining:.1f}с")
            if self.has_services_button(driver):
                return True
            time.sleep(POLL)
        return False

    def run(self):
        numbers = load_numbers()
        if not numbers:
            messagebox.showerror("Помилка", "numbers.txt не містить валідних номерів")
            return

        remaining_numbers = list(numbers)
        valid_buf = []

        wait_seconds = self.get_services_wait()
        pause = self.get_pause()

        driver = webdriver.Chrome()
        wait_login = WebDriverWait(driver, WAIT_LOGIN_SECONDS, poll_frequency=POLL)

        total = len(numbers)
        self.ui_set_progress(0, total)

        try:
            driver.get(URL)
            wait_login.until(EC.presence_of_element_located((By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            )))

            for i, number in enumerate(numbers, 1):
                if self.stop_event.is_set():
                    break

                self.ui_set_progress(i, total)
                self.log(f"→ 380{number}")

                wait = self.back_to_home_and_open_client(driver)
                self.set_number_safe(driver, wait, number)
                self.click_search(driver, wait)

                if self.wait_services_only(driver, number, wait_seconds):
                    self.valid_count += 1
                    self.ui_set_counts()
                    valid_buf.append(number)
                    remaining_numbers.remove(number)
                else:
                    self.skipped_count += 1
                    self.ui_set_counts()

                time.sleep(pause)

            append_lines(VALID_FILE, valid_buf)
            save_numbers(remaining_numbers)

        finally:
            driver.quit()
            self.ui_set_status("Готово")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.log("Готово.")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
