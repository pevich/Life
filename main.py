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
TRASH_FILE = "trash.txt"

WAIT_LOGIN_SECONDS = 600
WAIT_UI_SECONDS = 12

# ✅ Ускорение проверки после "Пошук"
POLL = 0.02
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
        self.root.geometry("820x520")

        self.status = tk.StringVar(value="Готово")
        self.progress = tk.StringVar(value="0 / 0")

        self.stop_event = threading.Event()
        self.worker = None

        ttk.Label(root, text="Lifecell Checker", font=("Segoe UI", 18, "bold")).pack(pady=10)

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

    def log(self, msg):
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
        """
        НЕ міняємо: твоя логіка повернення.
        """
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

    def set_number(self, driver, wait, number):
        inp = wait.until(EC.element_to_be_clickable((By.ID, "msisdn")))
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
        return inp  # вернём элемент, чтобы можно было нажать Enter

    def click_search_or_enter(self, driver, wait, msisdn_input):
        """
        ✅ Быстрее: сначала Enter в поле, если вдруг не сработало — жмём кнопку "Пошук".
        """
        try:
            msisdn_input.send_keys(Keys.ENTER)
            return
        except Exception:
            pass

        btn = wait.until(EC.element_to_be_clickable((
            By.XPATH, "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Пошук']]"
        )))
        self.js_click(driver, btn)

    def has_services_button(self, driver):
        return len(driver.find_elements(
            By.XPATH,
            "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація послуг']]"
        )) > 0

    def wait_services_adaptive(self, driver):
        """
        ✅ Самое главное ускорение:
        максимум ~0.8 сек вместо 9 сек.
        """
        end1 = time.time() + FAST_WAIT_1
        while time.time() < end1:
            if self.has_services_button(driver):
                return True
            time.sleep(POLL)

        end2 = time.time() + FAST_WAIT_2
        while time.time() < end2:
            if self.has_services_button(driver):
                return True
            time.sleep(POLL)

        return False

    def click_start_pack(self, driver, timeout=8):
        el = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Реєстрація стартового пакету']]"
            ))
        )
        self.js_click(driver, el)

    def click_register(self, driver, timeout=8):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((
                By.XPATH,
                "//button[.//span[contains(@class,'mat-button-wrapper') and normalize-space(.)='Зареєструвати']]"
            ))
        )
        self.js_click(driver, btn)

    def click_ok(self, driver, timeout=8):
        btn = WebDriverWait(driver, timeout, poll_frequency=POLL).until(
            EC.element_to_be_clickable((
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
        options.page_load_strategy = "eager"

        # ✅ CAPTCHA OK: картинки НЕ отключаем
        prefs = {
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
            self.log("Очікую логін/2FA/капчу...")

            wait_login.until(EC.presence_of_element_located((
                By.XPATH,
                "//div[contains(@class,'content')][.//div[contains(@class,'label') and normalize-space(.)='Клієнт']]"
            )))
            self.log("Авторизація OK")

            for i, number in enumerate(numbers, 1):
                if self.stop_event.is_set():
                    remaining_retry.extend(numbers[i-1:])
                    break

                self.root.after(0, lambda i=i, total=total: self.progress.set(f"{i} / {total}"))
                self.root.after(0, lambda n=number: self.status.set(f"380{n}"))
                self.log(f"→ 380{number}")

                try:
                    wait = self.back_to_home_and_open_client(driver)

                    msisdn_el = self.set_number(driver, wait, number)
                    self.click_search_or_enter(driver, wait, msisdn_el)

                    services_found = self.wait_services_adaptive(driver)

                    if services_found:
                        self.log("  ✅ Є «Реєстрація послуг» → Старт.пакет → Зареєструвати → Ок")

                        self.click_start_pack(driver)
                        time.sleep(0.2)
                        self.click_register(driver)
                        self.click_ok(driver)

                        valid_buf.append(number)
                        self.log("  ✔ Зареєстровано (VALID)")
                    else:
                        trash_buf.append(number)

                    self.back_to_home_and_open_client(driver)

                except Exception as e:
                    self.log(f"  ⚠ Помилка: {type(e).__name__}")
                    remaining_retry.append(number)
                    try:
                        self.back_to_home_and_open_client(driver)
                    except Exception:
                        pass

            # ✅ Быстро: пишем 1 раз в конце
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
            self.log("Готово. Файли: valid.txt / trash.txt / numbers.txt (тільки retry).")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
