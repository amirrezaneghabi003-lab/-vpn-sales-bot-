import multiprocessing
import os
import subprocess
import sys
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, WebDriverException


TEXT_TO_SEND = "  عکس میدم توی بات خواستی بیا https://ble.ir/chat_gpt_free6bot?start=98208512"
WAIT_TIME = 20
RETRY_DELAY = 5
CHROMEDRIVER_PATH = os.path.join(os.path.dirname(__file__), "chromedriver.exe")

# مسیر Chrome را اگر لازم بود تغییر بده
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

# دو پروفایل کاملاً جدا
ACCOUNT_1 = {
    "label": "ACC-1",
    "port": 9222,
    "user_data_dir": r"C:\chrome-bale-acc1",
}

ACCOUNT_2 = {
    "label": "ACC-2",
    "port": 9223,
    "user_data_dir": r"C:\chrome-bale-acc2",
}

WORKER_LABEL = "MAIN"


def log(message, status="info"):
    icons = {
        "info": "ℹ️",
        "success": "✅",
        "error": "❌",
        "search": "🔍",
        "send": "✉️",
        "wait": "⏳",
        "end": "🔚",
    }
    print(f"[{time.strftime('%H:%M:%S')}] {icons.get(status, '➡️')} [{WORKER_LABEL}] {message}")


def setup_driver(debug_addr):
    if not os.path.exists(CHROMEDRIVER_PATH):
        raise FileNotFoundError(
            f"chromedriver.exe پیدا نشد:\n{CHROMEDRIVER_PATH}"
        )

    chrome_options = Options()
    chrome_options.add_experimental_option("debuggerAddress", debug_addr)

    service = Service(CHROMEDRIVER_PATH)
    return webdriver.Chrome(service=service, options=chrome_options)


def click_first(driver, locators, timeout=WAIT_TIME):
    last_error = None

    for by, selector in locators:
        try:
            element = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, selector))
            )
            driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});",
                element
            )
            time.sleep(0.2)
            try:
                element.click()
            except WebDriverException:
                driver.execute_script("arguments[0].click();", element)
            return True
        except Exception as e:
            last_error = e

    raise last_error or Exception("element not found")


def get_first(driver, locators, timeout=WAIT_TIME):
    last_error = None

    for by, selector in locators:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((by, selector))
            )
        except Exception as e:
            last_error = e

    raise last_error or Exception("element not found")


def get_message_count(driver):
    selectors = [
        "[class*='message']",
        "[data-testid*='message']",
        "div[role='listitem']",
    ]

    max_count = 0
    for selector in selectors:
        try:
            count = len(driver.find_elements(By.CSS_SELECTOR, selector))
            max_count = max(max_count, count)
        except:
            pass

    return max_count


def wait_for_new_message(driver, old_count):
    log("منتظر پیام جدید...", "wait")
    end_time = time.time() + WAIT_TIME

    while time.time() < end_time:
        if get_message_count(driver) > old_count:
            log("پیام جدید دریافت شد", "success")
            return True
        time.sleep(0.5)

    raise TimeoutException("پیام جدید دریافت نشد")


def click_search_again(driver):
    log("کلیک روی «جستجوی مجدد»", "search")

    locators = [
        (By.XPATH, "//*[self::button or @role='button'][contains(., 'جستجوی مجدد')]"),
        (By.XPATH, "//*[self::button or @role='button'][contains(., 'جست و جوی مجدد')]"),
        (By.XPATH, "//*[self::button or @role='button'][contains(., 'جستجو')]"),
        (By.XPATH, "//*[contains(@aria-label,'جست')]"),
        (By.XPATH, "//*[contains(@title,'جست')]"),
    ]

    click_first(driver, locators)
    time.sleep(2)


def click_send_icon(driver):
    log("کلیک روی آیکون ارسال", "send")

    locators = [
        (By.CSS_SELECTOR, "div.ZOlxIi.Kt6p3g"),
        (By.CSS_SELECTOR, "div.ZOlxIi.Kt6p3g div.ZGzps0"),
        (By.CSS_SELECTOR, "div.ZGzps0"),
        (By.XPATH, "//div[contains(@class,'ZOlxIi') and contains(@class,'Kt6p3g')]"),
        (By.XPATH, "//div[contains(@class,'ZGzps0')]"),
    ]

    click_first(driver, locators)
    time.sleep(1)


def send_message_with_enter(driver, text):
    log("تایپ و ارسال پیام با Enter", "send")

    box = get_first(driver, [
        (By.CSS_SELECTOR, "[contenteditable='true']"),
        (By.CSS_SELECTOR, "textarea"),
        (By.CSS_SELECTOR, "input[type='text']"),
    ])

    try:
        box.click()
    except:
        driver.execute_script("arguments[0].focus();", box)

    time.sleep(0.3)

    try:
        box.send_keys(Keys.CONTROL, "a")
        box.send_keys(Keys.BACKSPACE)
    except:
        pass

    box.send_keys(text)
    time.sleep(0.5)
    box.send_keys(Keys.ENTER)

    log("پیام با Enter ارسال شد", "success")
    time.sleep(1)


def end_chat(driver):
    log("کلیک روی «پایان چت»", "end")

    locators = [
        (By.XPATH, "//*[self::button or @role='button'][contains(., 'پایان چت')]"),
        (By.XPATH, "//*[self::button or @role='button'][contains(., 'پایان')]"),
        (By.XPATH, "//*[contains(@aria-label,'پایان')]"),
        (By.XPATH, "//*[contains(@title,'پایان')]"),
    ]

    click_first(driver, locators)
    time.sleep(2)


def confirm_disconnect(driver):
    log("تایید «بله، قطع کن»", "end")

    locators = [
        (By.XPATH, "//*[self::button or @role='button'][contains(., 'بله، قطع کن')]"),
        (By.XPATH, "//*[self::button or @role='button'][contains(., 'قطع کن')]"),
        (By.XPATH, "//*[self::button or @role='button'][contains(., 'بله')]"),
        (By.XPATH, "//*[contains(@aria-label,'قطع')]"),
        (By.XPATH, "//*[contains(@title,'قطع')]"),
    ]

    click_first(driver, locators)
    time.sleep(2)


def run_cycle(driver, cycle_number):
    log(f"شروع چرخه {cycle_number}", "info")

    old_count = get_message_count(driver)

    click_search_again(driver)
    wait_for_new_message(driver, old_count)

    send_message_with_enter(driver, TEXT_TO_SEND)
    click_send_icon(driver)

    end_chat(driver)
    confirm_disconnect(driver)

    log(f"چرخه {cycle_number} کامل شد", "success")


def build_chrome_command(port, user_data_dir):
    return [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def launch_chrome_instance(port, user_data_dir, label):
    if not os.path.exists(CHROME_PATH):
        raise FileNotFoundError(f"Chrome پیدا نشد:\n{CHROME_PATH}")

    os.makedirs(user_data_dir, exist_ok=True)

    cmd = build_chrome_command(port, user_data_dir)
    log(f"راه‌اندازی Chrome برای {label} ...", "info")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)


def wait_and_connect(debug_addr, timeout=30):
    end_time = time.time() + timeout
    last_error = None

    while time.time() < end_time:
        try:
            return setup_driver(debug_addr)
        except Exception as e:
            last_error = e
            time.sleep(1)

    raise last_error or TimeoutException(f"اتصال به {debug_addr} برقرار نشد")


def worker(account):
    global WORKER_LABEL
    WORKER_LABEL = account["label"]

    debug_addr = f"127.0.0.1:{account['port']}"
    driver = None

    try:
        log("شروع فرایند مستقل", "info")
        driver = wait_and_connect(debug_addr)
        log("اتصال به کروم موفق بود", "success")

        cycle = 1
        while True:
            try:
                run_cycle(driver, cycle)
                cycle += 1
            except Exception as e:
                log(f"خطا: {e}", "error")
                time.sleep(RETRY_DELAY)

    except KeyboardInterrupt:
        log("فرایند متوقف شد", "info")
    except Exception as e:
        log(f"خطای فاجعه: {e}", "error")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass


def main():
    global WORKER_LABEL
    WORKER_LABEL = "MAIN"

    log("🚀 اسکریپت شروع شد", "info")
    log("Chromeها به‌صورت خودکار با دو پروفایل جدا بالا می‌آیند.", "info")
    time.sleep(1)

    launch_chrome_instance(ACCOUNT_1["port"], ACCOUNT_1["user_data_dir"], ACCOUNT_1["label"])
    launch_chrome_instance(ACCOUNT_2["port"], ACCOUNT_2["user_data_dir"], ACCOUNT_2["label"])

    log("لطفاً داخل هر پنجره، حساب Bale مربوط به همان پروفایل را لاگین کن.", "info")
    time.sleep(8)

    p1 = multiprocessing.Process(target=worker, args=(ACCOUNT_1,), daemon=False)
    p2 = multiprocessing.Process(target=worker, args=(ACCOUNT_2,), daemon=False)

    p1.start()
    p2.start()

    try:
        p1.join()
        p2.join()
    except KeyboardInterrupt:
        log("اسکریپت متوقف شد", "info")
    finally:
        for p in (p1, p2):
            if p.is_alive():
                p.terminate()
                p.join(timeout=3)

        sys.exit(0)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()