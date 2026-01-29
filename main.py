import os
import re
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import random
import sys

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException

URL = "https://my-ambassador.lifecell.ua"
VALID_FILE = "valid.txt"
REGSOON_FILE = "regsoon.txt"

PROFILE_BASE = "chrome_profiles"
DEFAULT_PREFIXES = ["67","68","77","96","97","98","39","50","66","95","99","75","63","73","93"]

WAIT_LOGIN_SECONDS = 600
WAIT_UI_SECONDS = 12
POLL = 0.05

class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Firk Stable Generator")

        self.browser_count = tk.IntVar(value=3)
        self.restart_every = tk.IntVar(value=400)
        self.prefixes_text = tk.StringVar(value=", ".join(DEFAULT_PREFIXES))

        ttk.Label(root, text="Кількість браузерів:").pack()
        ttk.Entry(root, textvariable=self.browser_count, width=5).pack()

        ttk.Label(root, text="Перезапуск кожні N номерів:").pack()
        ttk.Entry(root, textvariable=self.restart_every, width=6).pack()

        ttk.Label(root, text="Префікси:").pack()
        ttk.Entry(root, textvariable=self.prefixes_text, width=40).pack()

        ttk.Button(root, text="▶ Старт", command=self.start).pack(pady=5)
        ttk.Button(root, text="⏹ Стоп", command=self.stop).pack()

        self.status = ttk.Label(root, text="Готово")
        self.status.pack()

        self.stop_event = threading.Event()

    def parse_prefixes(self):
        raw = self.prefixes_text.get()
        parts = re.split(r"[,\s;|/]+", raw)
        out=[]
        for p in parts:
            p=re.sub(r"\D+","",p)
            if len(p)==2:
                out.append(p)
        return out or DEFAULT_PREFIXES

    def create_driver(self, worker_id):
        os.makedirs(PROFILE_BASE, exist_ok=True)
        profile_path = os.path.join(PROFILE_BASE, f"profile_{worker_id}")

        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument(f"--user-data-dir={profile_path}")
        options.page_load_strategy = "eager"

        return webdriver.Chrome(options=options)

    def start(self):
        try:
            n = int(self.browser_count.get())
            restart_n = int(self.restart_every.get())
            if n < 1 or restart_n < 10:
                raise ValueError
        except:
            messagebox.showerror("Помилка","Перевір числа")
            return

        self.stop_event.clear()
        prefixes = self.parse_prefixes()
        self.status.config(text="Працює...")

        for i in range(n):
            t = threading.Thread(target=self.worker, args=(i+1, prefixes, restart_n), daemon=True)
            t.start()

    def stop(self):
        self.stop_event.set()
        self.status.config(text="Зупинка...")

    def worker(self, worker_id, prefixes, restart_n):
        used=set()
        driver = None
        processed = 0

        while not self.stop_event.is_set():
            try:
                if driver is None:
                    driver = self.create_driver(worker_id)
                    wait_login = WebDriverWait(driver, WAIT_LOGIN_SECONDS, poll_frequency=POLL)
                    driver.get(URL)
                    print(f"[{worker_id}] Логінься")
                    wait_login.until(EC.presence_of_element_located((By.XPATH,"//div[contains(.,'Клієнт')]")))
                    print(f"[{worker_id}] OK")

                number = self.gen_number(prefixes, used)
                self.process_one(driver, number, worker_id)
                processed += 1

                if processed % restart_n == 0:
                    print(f"[{worker_id}] ♻ Перезапуск браузера")
                    driver.quit()
                    driver = None

            except WebDriverException as e:
                print(f"[{worker_id}] 💥 Браузер впав, відновлюю: {e}")
                try:
                    driver.quit()
                except:
                    pass
                driver = None
                time.sleep(3)

    def gen_number(self, prefixes, used):
        if len(used) > 200_000:
            used.clear()
        while True:
            pref = random.choice(prefixes)
            tail = f"{random.randint(0, 9_999_999):07d}"
            num = pref + tail
            if num not in used:
                used.add(num)
                return num

    def process_one(self, driver, number, worker_id):
        print(f"[{worker_id}] → 380{number}")

        driver.find_element(By.XPATH,"//div[contains(.,'Клієнт')]").click()
        inp = WebDriverWait(driver,10).until(EC.element_to_be_clickable((By.ID,"msisdn")))
        inp.clear()
        inp.send_keys("380"+number)

        btn = WebDriverWait(driver,10).until(EC.element_to_be_clickable((By.XPATH,"//button[contains(.,'Пошук')]")))
        btn.click()

        time.sleep(1.5)
        src = driver.page_source

        if "Реєстрація послуг" in src and "Реєстрація стартового пакету" in src:
            driver.find_element(By.XPATH,"//div[contains(.,'Реєстрація стартового пакету')]").click()
            time.sleep(0.2)
            driver.find_element(By.XPATH,"//button[contains(.,'Зареєструвати')]").click()
            with open(VALID_FILE,"a") as f: f.write(number+"\n")
            print(f"[{worker_id}] ✔ VALID")

        elif "Реєстрація стартового пакету" in src:
            with open(REGSOON_FILE,"a") as f: f.write(number+"\n")
            print(f"[{worker_id}] 🕒 REGSOON")

        else:
            print(f"[{worker_id}] ⏭ SKIP")

if __name__=="__main__":
    root=tk.Tk()
    App(root)
    root.mainloop()
