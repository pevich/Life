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
SERVICES_MAX_WAIT = 3.0          # максимум ждать "Реєстрація послуг"
SLEEP_BETWEEN_NUMBERS = 1.0      # пауза перед следующим номером


def parse_number_line(line: str):
    """
    ✅ Берём ТОЛЬКО:
      - 9 цифр (пример: 935180140)
      - или 12 цифр вида 380XXXXXXXXX → берём последние 9
    ❌ Всё остальное (даты 01.01.2026, 01.01, текст, короткие числа) — игнорируем.
    """
    digits = re.sub(r"\D+", "", line.strip())
    if not digits:
        return None

    # 380 + 9 digits
    if len(digits) == 12 and digits.startswith("380"):
        return digits[3:]

    # exactly 9 digits
    if len(digits) == 9:
        return digits

    # всё остальное пропускаем
    return None


def load_numbers():
    if not os.path.exists(NUMBERS_FILE):
        return []

    out = []
    with open(NUMBERS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            n = parse_number_line(line)
            if n:
                out.append(n)

    # убрать дубли, сохранить порядок
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
        self.root.geometry("860x560")

        self.status = tk.StringVar(value="Готово")
        self.progress_text = tk.StringVar(value="0 / 0")

        self.stop_event = threading.Event()
        self.worker = None

        ttk.Label(root, text="Lifecell Checker", font=("Segoe UI", 18, "bold")).pack(pady=10)

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=14, pady=4)
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        ttk.Label(bar, textvariable=self.progress_text).pack(side="right")

        # ✅ Progressbar
        self.pbar = ttk.Progressbar(root, orient="horizontal", mode="determinate", maximum=100)
        self.pbar.pack(fill="x", padx=14, pady=(6, 0))

        btns = ttk.Frame(root)
        btns.pack(fill="x", padx=14, pady=10)
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
            if total > 0:
                self.pbar["value"] = (i / total) * 100.0
            else:
                self.pbar["value"] = 0
        self.root.after(0, _upd)

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

    def wait_client_button(self, driver):
        return WebDriverWait(driver, WAIT_UI_SECONDS, poll_frequency=POLL).until(
            EC.element_to_be_clickable((
                By.XPATH,
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

    def click_back_if_exists(self, driver):
        backs = driver.find_elements(By.XPATH, "//button[.//mat-icon[normalize-space(text())='arrow_back']]")
        if backs:
            self.js_click(driver, backs[0])
            return True
        return False

    def back_to_home_and_open_client(self, driver):
        for _ in range(5):
            if driver.find_elements(By.ID, "msisdn"):
                return self.wait_msisdn_ready(driver)

            if driver.find_elements(By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"):
                self.click_client(driver)
                return self.wait_msisdn_ready(driver)

            if not self.click_back_if_exists(driver):
                break

        self.click_client(driver)
        return self.wait_msisdn_ready(driver)

    def set_number_safe(self, driver, wait, number):
        inp = wait.until(EC.element_to_be_clickable((By.ID, "msisdn")))
        full = "380" + number

        # ✅ чистим поле чтобы не склеивались номера
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
            """,
            inp, full
        )

        # ✅ микропаузка чтобы UI успел принять ввод
        time.sleep(0.15)

    def click_search(self, driver, wait):
        btn = wait.until(EC.element_to_be_clickable((
            By.XPATH, "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Пошук']]"
        )))
        self.js_click(driver, btn)

    def has_services_button(self, driver):
        return bool(driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація послуг']]"
        ))

    def wait_services_only(self, driver, current_number):
        end = time.time() + SERVICES_MAX_WAIT
        while time.time() < end:
            remaining = max(0.0, end - time.time())
            self.ui_set_status(f"380{current_number} | Перевірка… ще {remaining:.1f}с")
            if self.has_services_button(driver):
                return True
            time.sleep(POLL)
        return False

    def click_start_pack(self, driver, timeout=6):
        el = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація стартового пакету']]"
            ))
        )
        self.js_click(driver, el)

    def click_register(self, driver, timeout=6):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((
                By.XPATH, "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Зареєструвати']]"
            ))
        )
        self.js_click(driver, btn)

    def click_ok(self, driver, timeout=6):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((
                By.XPATH, "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Ок']]"
            ))
        )
        self.js_click(driver, btn)

    # ---------- MAIN ----------

    def run(self):
        numbers = load_numbers()
        if not numbers:
            self.root.after(0, lambda: messagebox.showerror(
                "Помилка",
                "numbers.txt порожній або не містить валідних номерів.\n"
                "Підходить тільки 9 цифр або 380XXXXXXXXX."
            ))
            self.root.after(0, lambda: self.btn_start.configure(state="normal"))
            self.root.after(0, lambda: self.btn_stop.configure(state="disabled"))
            return

        remaining_numbers = list(numbers)  # удаляем только успешно зарегистрированные
        valid_buf = []

        options = webdriver.ChromeOptions()
        options.add_argument("--disable-notifications")
        options.add_argument("--start-maximized")
        options.page_load_strategy = "eager"
        options.add_experimental_option("prefs", {"profile.default_content_setting_values.notifications": 2})

        driver = webdriver.Chrome(options=options)
        wait_login = WebDriverWait(driver, WAIT_LOGIN_SECONDS, poll_frequency=POLL)

        total = len(numbers)
        self.ui_set_progress(0, total)
        self.ui_set_status("Відкриваю сайт...")

        try:
            driver.get(URL)
            self.log("Очікую логін/2FA/капчу...")

            wait_login.until(EC.presence_of_element_located((
                By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            )))
            self.log("Авторизація OK")

            for i, number in enumerate(numbers, 1):
                if self.stop_event.is_set():
                    break

                self.ui_set_progress(i, total)
                self.ui_set_status(f"380{number}")
                self.log(f"→ 380{number}")

                try:
                    wait = self.back_to_home_and_open_client(driver)

                    self.set_number_safe(driver, wait, number)
                    self.click_search(driver, wait)

                    services_found = self.wait_services_only(driver, number)

                    if services_found:
                        self.log("  ✅ Є «Реєстрація послуг» → реєструю...")
                        self.click_start_pack(driver)
                        time.sleep(0.2)
                        self.click_register(driver)
                        self.click_ok(driver)

                        valid_buf.append(number)
                        if number in remaining_numbers:
                            remaining_numbers.remove(number)

                        self.log("  ✔ Зареєстровано (VALID)")
                    else:
                        self.log("  ⏭ Нема «Реєстрація послуг» (пропуск)")

                    self.back_to_home_and_open_client(driver)
                    time.sleep(SLEEP_BETWEEN_NUMBERS)

                except Exception as e:
                    self.log(f"  ⚠ Помилка: {type(e).__name__}")
                    try:
                        self.back_to_home_and_open_client(driver)
                    except Exception:
                        pass
                    time.sleep(SLEEP_BETWEEN_NUMBERS)

            append_lines(VALID_FILE, valid_buf)
            save_numbers(remaining_numbers)

        finally:
            try:
                driver.quit()
            except Exception:
                pass

            self.ui_set_status("Готово")
            self.root.after(0, lambda: self.btn_start.configure(state="normal"))
            self.root.after(0, lambda: self.btn_stop.configure(state="disabled"))
            self.log("Готово. numbers.txt оновлено (видалено тільки VALID), valid.txt дописано.")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
