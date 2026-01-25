import os
import re
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

URL = "https://my-ambassador.lifecell.ua"

NUMBERS_FILE = "numbers.txt"
VALID_FILE = "valid.txt"
TRASH_FILE = "trash.txt"

WAIT_LOGIN_SECONDS = 600
WAIT_UI_SECONDS = 6
POLL = 0.02

# адаптивний “після пошуку”
FAST_WAIT_1 = 0.25
FAST_WAIT_2 = 0.55


def load_numbers():
    if not os.path.exists(NUMBERS_FILE):
        return []
    out = []
    with open(NUMBERS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            s = re.sub(r"\D+", "", line.strip())
            if not s:
                continue
            if len(s) == 9:
                out.append(s)
            elif s.startswith("380") and len(s) == 12:
                out.append(s[3:])
            else:
                out.append(s)
    return list(dict.fromkeys(out))  # прибрати дублі, зберегти порядок


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
        self.root.title("Lifecell Checker FAST")
        self.root.geometry("840x540")

        self.status = tk.StringVar(value="Готово")
        self.progress = tk.StringVar(value="0 / 0")

        self.stop_event = threading.Event()
        self.worker = None

        ttk.Label(root, text="Lifecell Checker FAST", font=("Segoe UI", 18, "bold")).pack(pady=10)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=14, pady=4)
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        ttk.Label(bar, textvariable=self.progress).pack(side="right")

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=14, pady=6)
        self.btn_start = ttk.Button(btns, text="▶ Почати", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(btns, text="⏹ Стоп", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=10)

        self.log_box = tk.Text(root, height=18)
        self.log_box.pack(fill="both", expand=True, padx=14, pady=10)
        self.log_box.configure(state="disabled")

        self._log_counter = 0

    def log(self, msg, force=False):
        # менше логів = швидше
        self._log_counter += 1
        if (not force) and (self._log_counter % 3 != 0):
            return

        def _append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(0, _append)

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.status.set("Зупинка...")

    # ---------- Selenium helpers ----------

    def js_click(self, driver, el):
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        driver.execute_script("arguments[0].click();", el)

    def click_client_once(self, driver):
        el = WebDriverWait(driver, WAIT_UI_SECONDS, poll_frequency=POLL).until(
            EC.presence_of_element_located((
                By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            ))
        )
        self.js_click(driver, el)

    def ensure_msisdn(self, driver):
        """
        ✅ НЕ міняємо твоє правило:
        - якщо msisdn є — працюємо з ним
        - якщо нема — один раз тиснемо “Клієнт”
        """
        if driver.find_elements(By.ID, "msisdn"):
            return WebDriverWait(driver, WAIT_UI_SECONDS, poll_frequency=POLL)

        self.click_client_once(driver)
        w = WebDriverWait(driver, WAIT_UI_SECONDS, poll_frequency=POLL)
        w.until(EC.presence_of_element_located((By.ID, "msisdn")))
        return w

    def set_number(self, driver, wait, number):
        inp = wait.until(EC.presence_of_element_located((By.ID, "msisdn")))
        full = "380" + number
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
            """,
            inp, full
        )

    def click_search(self, driver, wait):
        btn = wait.until(EC.presence_of_element_located((
            By.XPATH, "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Пошук']]"
        )))
        self.js_click(driver, btn)

    def has_services(self, driver):
        return bool(driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація послуг']]"
        ))

    def has_start_pack(self, driver):
        return bool(driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація стартового пакету']]"
        ))

    def wait_services_adaptive(self, driver):
        # 1) дуже швидкий шанс
        end1 = time.time() + FAST_WAIT_1
        while time.time() < end1:
            if self.has_services(driver):
                return True
            time.sleep(POLL)
        # 2) ще трохи, якщо Angular запізнився
        end2 = time.time() + FAST_WAIT_2
        while time.time() < end2:
            if self.has_services(driver):
                return True
            time.sleep(POLL)
        return False

    def click_start_pack(self, driver, timeout=4):
        el = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.presence_of_element_located((
                By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація стартового пакету']]"
            ))
        )
        self.js_click(driver, el)

    def click_register(self, driver, timeout=5):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.presence_of_element_located((
                By.XPATH,
                "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Зареєструвати']]"
            ))
        )
        self.js_click(driver, btn)

    def click_ok(self, driver, timeout=5):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.presence_of_element_located((
                By.XPATH,
                "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Ок']]"
            ))
        )
        self.js_click(driver, btn)

    # ---------- MAIN ----------

    def run(self):
        numbers = load_numbers()
        if not numbers:
            self.root.after(0, lambda: messagebox.showerror("Помилка", "numbers.txt порожній або не знайдено."))
            self.root.after(0, lambda: self.btn_start.configure(state="normal"))
            self.root.after(0, lambda: self.btn_stop.configure(state="disabled"))
            return

        remaining_retry = []
        valid_buf = []
        trash_buf = []

        options = webdriver.ChromeOptions()
        options.add_argument("--disable-notifications")
        options.add_argument("--start-maximized")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.page_load_strategy = "eager"

        # вимкнути картинки (реально прискорює)
        prefs = {
            "profile.managed_default_content_settings.images": 2,
            "profile.default_content_setting_values.notifications": 2
        }
        options.add_experimental_option("prefs", prefs)

        driver = webdriver.Chrome(options=options)
        wait_login = WebDriverWait(driver, WAIT_LOGIN_SECONDS, poll_frequency=POLL)

        total = len(numbers)
        self.root.after(0, lambda: self.progress.set(f"0 / {total}"))
        self.root.after(0, lambda: self.status.set("Відкриваю сайт..."))

        try:
            driver.get(URL)
            self.log("Очікую логін/2FA...", force=True)

            wait_login.until(EC.presence_of_element_located((
                By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            )))
            self.log("Авторизація OK", force=True)

            for i, number in enumerate(numbers, 1):
                if self.stop_event.is_set():
                    remaining_retry.extend(numbers[i-1:])
                    break

                self.root.after(0, lambda i=i, total=total: self.progress.set(f"{i} / {total}"))
                self.root.after(0, lambda n=number: self.status.set(f"380{n}"))

                try:
                    wait = self.ensure_msisdn(driver)

                    self.set_number(driver, wait, number)
                    self.click_search(driver, wait)

                    if not self.wait_services_adaptive(driver):
                        # нема "Реєстрація послуг"
                        trash_buf.append(number)
                        self.log(f"→ 380{number} : TRASH")
                        continue

                    # є "Реєстрація послуг"
                    if not self.has_start_pack(driver):
                        # є послуги, але нема стартового пакету — одразу наступний
                        trash_buf.append(number)
                        self.log(f"→ 380{number} : SERVICES без START PACK (skip)")
                        continue

                    # є послуги + стартовий пакет → реєструємо
                    self.click_start_pack(driver)
                    self.click_register(driver)
                    self.click_ok(driver)

                    valid_buf.append(number)
                    self.log(f"→ 380{number} : VALID (registered)")

                except Exception as e:
                    remaining_retry.append(number)
                    self.log(f"→ 380{number} : RETRY ({type(e).__name__})", force=True)

            # Пишемо файли 1 раз — це швидко
            append_lines(VALID_FILE, valid_buf)
            append_lines(TRASH_FILE, trash_buf)
            save_numbers(remaining_retry)

        finally:
            try:
                driver.quit()
            except Exception:
                pass

            self.root.after(0, lambda: self.status.set("Готово"))
            self.root.after(0, lambda: self.btn_start.configure(state="normal"))
            self.root.after(0, lambda: self.btn_stop.configure(state="disabled"))
            self.log("Готово. valid/trash дописані. numbers.txt = тільки retry.", force=True)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
