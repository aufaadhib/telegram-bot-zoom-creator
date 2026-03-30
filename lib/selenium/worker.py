from pathlib import Path
import logging
import random
import re
import string
import time
import time
from typing import Any, Callable, Dict
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


logger = logging.getLogger("telegram-selenium-bot.selenium")


def _build_options(
    profile_root: Path,
    profile_key: str,
    headless: bool,
    chrome_binary: str,
    locale: str = "id-ID",
) -> Options:
    profile_path = profile_root / f"user_{profile_key}"
    profile_path.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={profile_path.resolve()}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(f"--lang={locale}")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option(
        "prefs",
        {
            "intl.accept_languages": f"{locale},id,en-US,en",
        },
    )

    if headless:
        options.add_argument("--headless=new")
    if chrome_binary:
        options.binary_location = chrome_binary

    return options


def _build_service(chromedriver_path: str) -> Service:
    if chromedriver_path:
        return Service(chromedriver_path)
    return Service(ChromeDriverManager().install())


def _apply_browser_region(driver: webdriver.Chrome, locale: str, timezone: str) -> None:
    try:
        driver.execute_cdp_cmd(
            "Emulation.setTimezoneOverride",
            {"timezoneId": timezone},
        )
    except Exception:
        logger.warning("Gagal set timezone override: %s", timezone)
    try:
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": driver.execute_script("return navigator.userAgent"),
                "acceptLanguage": locale,
                "platform": "Windows",
            },
        )
    except Exception:
        logger.warning("Gagal set accept language override: %s", locale)


def _normalize_email_domain(raw_domain: str) -> str:
    value = (raw_domain or "").strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = value.split("/", 1)[0].strip()
    if value.startswith("@"):
        value = value[1:]
    if "." not in value:
        raise ValueError("Domain email tidak valid untuk generate email random.")
    return value


def _generate_random_email(domain: str) -> str:
    local = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return f"{local}@{domain}"


def _extract_otp_from_html(html: str) -> str | None:
    patterns = [
        re.compile(r'<div[^>]*class="e7m subj_div_45g45gg"[^>]*>\s*(\d{6})\s+ada kode verifikasi Zoom Anda', re.IGNORECASE),
        re.compile(r'<div[^>]*class="code-div"[^>]*>\s*(\d{6})\s*</div>', re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(html or "")
        if match:
            return match.group(1)
    return None


def _fetch_otp_from_generator_email_via_browser(
    driver: webdriver.Chrome,
    email: str,
    page_wait_timeout: int,
    poll_seconds: int = 10,
) -> str | None:
    target_email = (email or "").strip()
    if not target_email or "@" not in target_email:
        return None
    otp_url = f"https://generator.email/{quote(target_email, safe='@')}"
    main_handle = driver.current_window_handle
    logger.info("OTP check start via browser | email=%s | url=%s", target_email, otp_url)

    driver.execute_script("window.open('about:blank', '_blank');")
    new_handles = [handle for handle in driver.window_handles if handle != main_handle]
    if not new_handles:
        return None
    otp_handle = new_handles[-1]

    try:
        driver.switch_to.window(otp_handle)
        driver.get(otp_url)

        poll_wait = WebDriverWait(driver, max(1, int(poll_seconds)), poll_frequency=1)

        def _otp_ready(drv: webdriver.Chrome) -> str | bool:
            try:
                WebDriverWait(drv, max(1, int(page_wait_timeout))).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                otp = _extract_otp_from_html(drv.page_source or "")
                if otp:
                    logger.info("OTP found in generator.email | email=%s", target_email)
                    return otp
                drv.refresh()
                return False
            except Exception:
                return False

        otp_value = str(poll_wait.until(_otp_ready))
        return otp_value
    except TimeoutException:
        logger.warning("OTP not found in generator.email within %ss | email=%s", poll_seconds, target_email)
        return None
    finally:
        try:
            driver.close()
        except Exception:
            pass
        driver.switch_to.window(main_handle)
        logger.info("OTP check end via browser | email=%s", target_email)


def _run_zoom_signup_initial(
    profile_root: Path,
    profile_key: str,
    url: str,
    wait_timeout: int,
    email_domain: str,
    signup_password: str = "",
    trial_days: int = 14,
    payment_vcc: str = "",
    otp_resolver: Callable[[str], str | None] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    headless: bool = False,
    auto_close: bool = True,
    locale: str = "id-ID",
    timezone: str = "Asia/Jakarta",
    chromedriver_path: str = "",
    chrome_binary: str = "",
) -> Dict[str, str]:
    driver = webdriver.Chrome(
        service=_build_service(chromedriver_path),
        options=_build_options(profile_root, profile_key, headless, chrome_binary, locale),
    )
    wait = WebDriverWait(driver, wait_timeout)

    try:
        _emit_progress(progress_callback, "Membuka halaman signup...")
        _apply_browser_region(driver, locale=locale, timezone=timezone)
        domain = _normalize_email_domain(email_domain)
        birth_year = str(random.randint(1990, 2001))
        email = _generate_random_email(domain)
        log_ctx = f"profile={profile_key} email={email}"
        logger.info("Zoom signup start | %s", log_ctx)

        driver.get(url)
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        logger.info("Page loaded | %s | url=%s", log_ctx, driver.current_url)

        year_input = wait.until(EC.visibility_of_element_located((By.ID, "year")))
        year_input.clear()
        year_input.send_keys(birth_year)
        logger.info("Birth year filled | %s | year=%s", log_ctx, birth_year)

        continue_btn_birth = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-testid='continue-btn']"))
        )
        continue_btn_birth.click()
        logger.info("Continue clicked after birth year | %s", log_ctx)
        _emit_progress(progress_callback, "Tahun lahir diisi.")

        email_input = wait.until(EC.visibility_of_element_located((By.ID, "email")))
        email_input.clear()
        email_input.send_keys(email)
        logger.info("Email filled | %s", log_ctx)

        continue_btn_email = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[data-testid='continue-btn']"))
        )
        continue_btn_email.click()
        logger.info("Continue clicked after email | %s", log_ctx)
        _emit_progress(progress_callback, f"Email dibuat: {email}")

        otp_source = "skipped"
        post_email_state = _wait_for_post_email_state_adaptive(driver, max_attempts=4, per_attempt_timeout=5)
        logger.info("Post-email state detected | %s | state=%s", log_ctx, post_email_state)
        _emit_progress(progress_callback, f"State setelah email: {post_email_state}")

        if post_email_state == "otp":
            logger.info("OTP container detected after adaptive check | %s", log_ctx)
            logger.info("OTP container visible | %s", log_ctx)
            otp_source = "generator.email"
            _emit_progress(progress_callback, "Mencari OTP...")
            otp_code = _fetch_otp_from_generator_email_via_browser(
                driver=driver,
                email=email,
                page_wait_timeout=wait_timeout,
                poll_seconds=10,
            )
            if not otp_code and otp_resolver:
                otp_source = "manual"
                logger.info("Fallback to manual OTP | %s", log_ctx)
                _emit_progress(progress_callback, "OTP otomatis tidak ditemukan, menunggu input manual...")
                otp_code = otp_resolver(email)
            if not otp_code or len(otp_code) != 6 or not otp_code.isdigit():
                logger.error("OTP missing/invalid | %s", log_ctx)
                raise TimeoutError("OTP tidak ditemukan dari generator.email dan tidak ada input manual.")
            logger.info("OTP acquired | %s | source=%s", log_ctx, otp_source)
            _emit_progress(progress_callback, f"OTP didapat dari {otp_source}.")

            pin_container = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='pin-code']")))
            for idx, digit in enumerate(otp_code):
                digit_box = wait.until(
                    EC.element_to_be_clickable(
                        (
                            By.CSS_SELECTOR,
                            f"[data-testid='pin-code'] [role='textbox'][data-order='{idx}']",
                        )
                    )
                )
                digit_box.click()
                digit_box.send_keys(digit)
            logger.info("OTP digits filled (6) | %s", log_ctx)

            verify_button = wait.until(lambda d: _find_active_verify_button(d))
            verify_button.click()
            logger.info("Verify button clicked | %s", log_ctx)
            _emit_progress(progress_callback, "OTP diverifikasi.")

            wait.until(
                lambda d: bool(d.find_elements(By.ID, "firstName"))
                or not pin_container.is_displayed()
                or "signup#/signup/new" in d.current_url
            )
            logger.info("OTP verification transition success | %s | url=%s", log_ctx, driver.current_url)
        elif post_email_state == "name":
            logger.info("OTP step skipped; firstName already visible | %s", log_ctx)
        else:
            raise TimeoutError(
                "Tidak menemukan state OTP maupun form nama setelah submit email. "
                "Kemungkinan ada step perantara baru (continue-btn) yang belum tertangani."
            )

        first_name_input = wait.until(EC.visibility_of_element_located((By.ID, "firstName")))
        last_name_input = wait.until(EC.visibility_of_element_located((By.ID, "lastName")))
        password_input = wait.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )

        first_name_input.clear()
        first_name_input.send_keys("Yuks")
        last_name_input.clear()
        last_name_input.send_keys("AppStore")
        password_input.clear()
        password_input.send_keys(signup_password)
        logger.info("Name + password filled | %s", log_ctx)
        _emit_progress(progress_callback, "Nama & password diisi.")

        continue_after_name = wait.until(
            lambda d: _find_active_continue_button(d, include_labels=("lanjutkan", "continue"))
        )
        continue_after_name.click()
        logger.info("Continue clicked after name/password | %s", log_ctx)
        _emit_progress(progress_callback, "Lanjut dari form nama.")

        start_trial_btn = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button.start-free-trial[aria-disabled='false']"))
        )
        start_trial_btn.click()
        logger.info("Start free trial clicked | %s", log_ctx)
        _emit_progress(progress_callback, "Mulai uji coba gratis.")

        plan_option = wait.until(lambda d: _find_trial_plan_option(d, trial_days))
        _click_element(driver, plan_option)
        logger.info("Plan selected | %s | trial_days=%s", log_ctx, trial_days)
        _emit_progress(progress_callback, f"Paket {trial_days} hari dipilih.")

        checkout_continue = wait.until(
            lambda d: _find_active_checkout_continue_button(d)
        )
        checkout_continue.click()
        logger.info("Checkout continue clicked | %s", log_ctx)
        _emit_progress(progress_callback, "Lanjut ke checkout.")

        wait.until(
            lambda d: "checkout" in d.current_url.lower()
            or bool(d.find_elements(By.CSS_SELECTOR, "input[aria-label='Nama Kartu']"))
            or bool(d.find_elements(By.CSS_SELECTOR, "button[data-testid='checkout-button-place-order']"))
        )
        logger.info("Checkout page reached | %s | url=%s", log_ctx, driver.current_url)

        # Address form
        street_input = wait.until(
            lambda d: _find_visible_input(
                d,
                By.CSS_SELECTOR,
                "input[role='combobox'].zbo-input__inner.is-leaded[type='text']",
            )
        )
        _fill_input(street_input, "Kabupaten Cirebon")
        street_input.send_keys(Keys.ENTER)
        logger.info("Street filled + enter | %s", log_ctx)
        _emit_progress(progress_callback, "Alamat jalan diisi.")

        zip_input = wait.until(EC.visibility_of_element_located((By.ID, "addr-zip")))
        city_input = wait.until(EC.visibility_of_element_located((By.ID, "addr-city")))
        state_input = wait.until(EC.visibility_of_element_located((By.ID, "addr-state")))
        _fill_input(zip_input, "12325")
        _fill_input(city_input, "Kedawung")
        _fill_input(state_input, "Jawa Barat")
        logger.info("Zip/City/State filled | %s", log_ctx)
        _emit_progress(progress_callback, "Zip, kota, provinsi diisi.")

        country_input = wait.until(lambda d: _find_country_dropdown_input(d))
        _select_dropdown_value(driver, country_input, "Indonesia")
        logger.info("Country selected | %s", log_ctx)
        _emit_progress(progress_callback, "Negara dipilih.")

        account_type_input = wait.until(lambda d: _find_account_type_dropdown_input(d))
        _select_dropdown_value_any(
            driver,
            account_type_input,
            ["Akun pribadi", "Personal account", "Personal"],
        )
        logger.info("Account type selected | %s", log_ctx)
        _emit_progress(progress_callback, "Jenis akun dipilih.")

        payment_continue_btn = wait.until(
            lambda d: _find_active_payment_continue_button(d)
        )
        _click_element_human(driver, payment_continue_btn)
        logger.info("Continue to payment clicked | %s", log_ctx)
        _emit_progress(progress_callback, "Lanjut ke pembayaran.")

        wait.until(
            lambda d: bool(d.find_elements(By.CSS_SELECTOR, "button[data-testid='checkout-button-place-order']"))
            or "payment" in d.current_url.lower()
        )
        logger.info("Payment page reached after address submit | %s | url=%s", log_ctx, driver.current_url)

        card_data = _parse_payment_vcc(payment_vcc)
        if not card_data:
            raise RuntimeError("CARD_MISSING: Data kartu tidak tersedia.")

        _fill_input_any_context(
            driver,
            wait,
            "Yuks AppStore",
            [
                (By.ID, "input-creditCardHolderName"),
                (By.NAME, "field_creditCardHolderName"),
            ],
            human_like=True,
        )
        _fill_input_any_context(
            driver,
            wait,
            card_data["number"],
            [
                (By.ID, "input-creditCardNumber"),
                (By.NAME, "field_creditCardNumber"),
                (By.CSS_SELECTOR, "input.text-card-number"),
            ],
            human_like=True,
        )
        _fill_input_any_context(
            driver,
            wait,
            card_data["cvv"],
            [
                (By.ID, "input-cardSecurityCode"),
                (By.NAME, "field_cardSecurityCode"),
                (By.CSS_SELECTOR, "input.text-input-cvv"),
            ],
            human_like=True,
        )
        month_select_el = _wait_element_any_context(
            driver,
            wait,
            [
                (By.ID, "input-creditCardExpirationMonth"),
                (By.NAME, "field_creditCardExpirationMonth"),
            ],
        )
        year_select_el = _wait_element_any_context(
            driver,
            wait,
            [
                (By.ID, "input-creditCardExpirationYear"),
                (By.NAME, "field_creditCardExpirationYear"),
            ],
        )
        Select(month_select_el).select_by_value(card_data["month"])
        Select(year_select_el).select_by_value(card_data["year"])
        driver.switch_to.default_content()
        logger.info("Payment form filled | %s | card=****%s", log_ctx, card_data["number"][-4:])
        _emit_progress(progress_callback, f"Form pembayaran diisi (kartu ****{card_data['number'][-4:]}).")

        place_order_btn = wait.until(
            lambda d: _find_active_place_order_button(d)
        )
        _click_element_human(driver, place_order_btn)
        logger.info("Place order clicked | %s", log_ctx)
        _emit_progress(progress_callback, "Buat pesanan diklik.")

        payment_status, payment_message = _wait_payment_result(driver, timeout_sec=20)
        if payment_status == "error":
            if "nomor kartu kredit yang benar" in _normalize_text(payment_message):
                raise RuntimeError(f"CARD_INVALID: {payment_message}")
            raise RuntimeError(f"PAYMENT_ERROR: {payment_message}")
        if payment_status == "success":
            logger.info("Payment success marker detected | %s", log_ctx)
            _emit_progress(progress_callback, "Pembayaran berhasil.")
        else:
            logger.warning("Payment result unknown, treated as failure | %s", log_ctx)
            raise RuntimeError("PAYMENT_UNCONFIRMED: Tidak menemukan indikator sukses pembayaran.")

        return {
            "title": driver.title.strip() or "(No title)",
            "current_url": driver.current_url,
            "body_preview": f"Zoom signup step done: year+email+otp({otp_source})+plan {trial_days}d+address+payment.",
            "birth_year": birth_year,
            "generated_email": email,
            "otp_source": otp_source,
        }
    except Exception:
        logger.exception("Zoom signup initial failed | profile=%s", profile_key)
        raise
    finally:
        if auto_close:
            driver.quit()
        else:
            logger.warning("Browser tetap terbuka (auto close OFF) | profile=%s", profile_key)


def _find_active_verify_button(driver: webdriver.Chrome):
    buttons = driver.find_elements(By.CSS_SELECTOR, "button[data-testid='continue-btn']")
    for button in buttons:
        label = (button.text or "").strip().lower()
        if "verifikasi" not in label and "verify" not in label:
            continue
        aria_disabled = (button.get_attribute("aria-disabled") or "").strip().lower()
        disabled_attr = button.get_attribute("disabled")
        if aria_disabled == "false" and button.is_enabled() and disabled_attr is None:
            return button
    return False


def _emit_progress(callback: Callable[[str], None] | None, message: str) -> None:
    if not callback:
        return
    try:
        callback(message)
    except Exception:
        pass


def _wait_for_post_email_state_adaptive(
    driver: webdriver.Chrome,
    max_attempts: int = 3,
    per_attempt_timeout: int = 5,
) -> str:
    deadline = time.monotonic() + max_attempts * per_attempt_timeout

    for attempt in range(max_attempts):
        if driver.find_elements(By.CSS_SELECTOR, "[data-testid='pin-code']"):
            return "otp"
        if driver.find_elements(By.ID, "firstName"):
            return "name"
        try:
            continue_after_email = WebDriverWait(driver, per_attempt_timeout).until(
                lambda d: _find_active_continue_button(d, include_labels=("lanjutkan", "continue"))
            )
            _click_element(driver, continue_after_email)
            logger.info("Adaptive continue click before OTP | attempt=%s", attempt + 1)
        except TimeoutException:
            pass

        if driver.find_elements(By.CSS_SELECTOR, "[data-testid='pin-code']"):
            return "otp"
        if driver.find_elements(By.ID, "firstName"):
            return "name"

    while time.monotonic() < deadline:
        if driver.find_elements(By.CSS_SELECTOR, "[data-testid='pin-code']"):
            return "otp"
        if driver.find_elements(By.ID, "firstName"):
            return "name"
        try:
            btn = _find_active_continue_button(driver, include_labels=("lanjutkan", "continue"))
            if btn:
                _click_element(driver, btn)
        except Exception:
            pass

    return "unknown"


def _find_active_continue_button(driver: webdriver.Chrome, include_labels: tuple[str, ...]):
    buttons = driver.find_elements(By.CSS_SELECTOR, "button[data-testid='continue-btn']")
    labels = tuple(item.lower() for item in include_labels)
    for button in buttons:
        label = (button.text or "").strip().lower()
        if labels and not any(key in label for key in labels):
            continue
        aria_disabled = (button.get_attribute("aria-disabled") or "").strip().lower()
        disabled_attr = button.get_attribute("disabled")
        if aria_disabled == "false" and button.is_enabled() and disabled_attr is None:
            return button
    return False


def _find_active_checkout_continue_button(driver: webdriver.Chrome):
    selectors = [
        "button.opc-btn-continue",
        "button[data-testid^='checkout-button-']",
    ]
    for selector in selectors:
        buttons = driver.find_elements(By.CSS_SELECTOR, selector)
        for button in buttons:
            label = (button.text or "").strip().lower()
            if "lanjutkan ke checkout" not in label and "continue to checkout" not in label:
                continue
            aria_disabled = (button.get_attribute("aria-disabled") or "").strip().lower()
            disabled_attr = button.get_attribute("disabled")
            if aria_disabled == "false" and button.is_enabled() and disabled_attr is None:
                return button
    return False


def _click_element(driver: webdriver.Chrome, element) -> None:
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def _scroll_into_view(driver: webdriver.Chrome, element) -> None:
    driver.execute_script(
        "arguments[0].scrollIntoView({behavior:'instant', block:'center', inline:'center'});",
        element,
    )


def _click_element_human(driver: webdriver.Chrome, element) -> None:
    try:
        _scroll_into_view(driver, element)
        ActionChains(driver).move_to_element(element).click().perform()
    except Exception:
        _click_element(driver, element)


def _fill_input(element, value: str) -> None:
    element.click()
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(Keys.BACKSPACE)
    element.send_keys(value)


def _fill_input_human(driver: webdriver.Chrome, element, value: str) -> None:
    _scroll_into_view(driver, element)
    _click_element_human(driver, element)
    _force_clear_input(driver, element)
    for char in value:
        element.send_keys(char)


def _find_visible_input(driver: webdriver.Chrome, by: By, selector: str):
    elements = driver.find_elements(by, selector)
    for element in elements:
        if element.is_displayed() and element.is_enabled():
            return element
    return False


def _find_country_dropdown_input(driver: webdriver.Chrome):
    elements = driver.find_elements(By.CSS_SELECTOR, "input.zbo-virtual-filter-select-input__inner[role='combobox']")
    for element in elements:
        if not (element.is_displayed() and element.is_enabled()):
            continue
        aria_label = _normalize_text(element.get_attribute("aria-label") or "")
        if "negara" in aria_label or "country" in aria_label:
            return element

    # fallback: first visible combobox that is not account type
    for element in elements:
        if not (element.is_displayed() and element.is_enabled()):
            continue
        aria_label = _normalize_text(element.get_attribute("aria-label") or "")
        if "jenis akun" in aria_label or "account type" in aria_label:
            continue
        return element
    return False


def _find_account_type_dropdown_input(driver: webdriver.Chrome):
    elements = driver.find_elements(By.CSS_SELECTOR, "input.zbo-virtual-filter-select-input__inner[role='combobox']")
    for element in elements:
        if not (element.is_displayed() and element.is_enabled()):
            continue
        aria_label = _normalize_text(element.get_attribute("aria-label") or "")
        placeholder = _normalize_text(element.get_attribute("placeholder") or "")
        if "jenis akun" in aria_label or "account type" in aria_label:
            return element
        if "jenis akun" in placeholder or "account type" in placeholder:
            return element

    # fallback: second visible combobox
    visible = [e for e in elements if e.is_displayed() and e.is_enabled()]
    if len(visible) >= 2:
        return visible[1]
    return False


def _select_dropdown_value(driver: webdriver.Chrome, input_element, value: str) -> None:
    _click_element(driver, input_element)
    _fill_input(input_element, value)

    try:
        option = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    f"//*[self::li or self::div or self::span][contains(normalize-space(.), '{value}')]",
                )
            )
        )
        _click_element(driver, option)
    except Exception:
        input_element.send_keys(Keys.ENTER)

    current = _normalize_text(input_element.get_attribute("value") or "")
    if value.lower() not in current:
        input_element.send_keys(Keys.ENTER)


def _select_dropdown_value_any(driver: webdriver.Chrome, input_element, values: list[str]) -> None:
    for candidate in values:
        _select_dropdown_value(driver, input_element, candidate)
        current = _normalize_text(input_element.get_attribute("value") or "")
        if _normalize_text(candidate) in current:
            return
    # fallback terakhir: pakai kandidat pertama
    _select_dropdown_value(driver, input_element, values[0])


def _find_active_payment_continue_button(driver: webdriver.Chrome):
    buttons = driver.find_elements(By.CSS_SELECTOR, "button.address-submit-btn, button[data-testid^='checkout-button-']")
    for button in buttons:
        if not (button.is_displayed() and button.is_enabled()):
            continue
        label = _normalize_text(button.text or "")
        if "lanjutkan ke pembayaran" not in label and "continue to payment" not in label:
            continue
        aria_disabled = _normalize_text(button.get_attribute("aria-disabled") or "")
        disabled_attr = button.get_attribute("disabled")
        if aria_disabled == "false" and disabled_attr is None:
            return button
    return False


def _find_element_any_context(
    driver: webdriver.Chrome,
    locators: list[tuple[str, str]],
):
    def _find_in_current_context():
        for by, selector in locators:
            elements = driver.find_elements(by, selector)
            for element in elements:
                if element.is_displayed() and element.is_enabled():
                    return element
        return None

    driver.switch_to.default_content()
    element = _find_in_current_context()
    if element:
        return element

    frames = driver.find_elements(By.TAG_NAME, "iframe")
    for frame in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
            element = _find_in_current_context()
            if element:
                return element
        except Exception:
            continue

    driver.switch_to.default_content()
    return None


def _wait_element_any_context(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    locators: list[tuple[str, str]],
):
    element = wait.until(lambda d: _find_element_any_context(driver, locators))
    if not element:
        raise TimeoutException(f"Element tidak ditemukan pada context manapun: {locators}")
    return element


def _fill_input_any_context(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    value: str,
    locators: list[tuple[str, str]],
    human_like: bool = False,
) -> None:
    element = _wait_element_any_context(driver, wait, locators)
    if human_like:
        _fill_input_human(driver, element, value)
    else:
        _fill_input(element, value)


def _force_clear_input(driver: webdriver.Chrome, element) -> None:
    try:
        driver.execute_script(
            "arguments[0].value=''; arguments[0].dispatchEvent(new Event('input', {bubbles:true}));",
            element,
        )
    except Exception:
        pass
    try:
        element.clear()
    except Exception:
        pass


def _parse_payment_vcc(vcc: str) -> dict[str, str] | None:
    raw = (vcc or "").strip()
    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != 4:
        return None
    number, month, year, cvv = parts
    if not (number.isdigit() and cvv.isdigit()):
        return None
    if not month.isdigit():
        return None
    month_value = int(month)
    if month_value < 1 or month_value > 12:
        return None
    if year.isdigit() and len(year) == 2:
        year = f"20{year}"
    if not (year.isdigit() and len(year) == 4):
        return None
    return {
        "number": number,
        "month": f"{month_value:02d}",
        "year": year,
        "cvv": cvv,
    }


def _find_active_place_order_button(driver: webdriver.Chrome):
    buttons = driver.find_elements(By.CSS_SELECTOR, "button[data-testid='checkout-button-place-order']")
    for button in buttons:
        if not (button.is_displayed() and button.is_enabled()):
            continue
        aria_disabled = _normalize_text(button.get_attribute("aria-disabled") or "")
        disabled_attr = button.get_attribute("disabled")
        if aria_disabled == "false" and disabled_attr is None:
            return button
    return False


def _wait_payment_result(driver: webdriver.Chrome, timeout_sec: int = 12) -> tuple[str, str]:
    deadline = time.monotonic() + max(1, int(timeout_sec))
    while time.monotonic() < deadline:
        success_titles = driver.find_elements(By.CSS_SELECTOR, "h1.opc-succezz-thx__title")
        for element in success_titles:
            text = (element.text or "").strip()
            if text:
                return "success", text

        errors = driver.find_elements(By.CSS_SELECTOR, "p.opc-payment-credit__error--desc")
        for element in errors:
            text = (element.text or "").strip()
            if text:
                return "error", text

        # Jika sudah berpindah page tanpa error lokal, anggap submit diterima.
        current_url = (driver.current_url or "").lower()
        if "payment" not in current_url:
            return "success", "Payment page changed"
    return "unknown", ""


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _find_trial_plan_option(driver: webdriver.Chrome, trial_days: int):
    options = driver.find_elements(By.CSS_SELECTOR, "span.opc-zmone__radio")
    if not options:
        return False

    wanted = int(trial_days)
    primary_keywords = (
        ("7hari gratis", "7 hari gratis", "7-day free", "7 days free", "7 day free")
        if wanted == 7
        else ("14hari gratis", "14 hari gratis", "14-day free", "14 days free", "14 day free")
    )
    fallback_keywords = (
        ("bulanan", "monthly", "month")
        if wanted == 7
        else ("tahunan", "annual", "yearly", "year")
    )

    for option in options:
        text = _normalize_text(option.text)
        if any(token in text for token in primary_keywords):
            return option

    for option in options:
        text = _normalize_text(option.text)
        if any(token in text for token in fallback_keywords):
            return option

    # Fallback terakhir: pilih opsi pertama untuk trial 14, kedua untuk trial 7 jika tersedia.
    if wanted == 7 and len(options) >= 2:
        return options[1]
    return options[0]


def _run_single_visit(
    profile_root: Path,
    profile_key: str,
    url: str,
    wait_timeout: int,
    headless: bool = False,
    auto_close: bool = True,
    locale: str = "id-ID",
    timezone: str = "Asia/Jakarta",
    chromedriver_path: str = "",
    chrome_binary: str = "",
) -> Dict[str, str]:
    driver = webdriver.Chrome(
        service=_build_service(chromedriver_path),
        options=_build_options(profile_root, profile_key, headless, chrome_binary, locale),
    )
    wait = WebDriverWait(driver, wait_timeout)

    try:
        _apply_browser_region(driver, locale=locale, timezone=timezone)
        driver.get(url)
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
        body = wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))

        preview = (body.text or "").strip().replace("\n", " ")
        if len(preview) > 180:
            preview = preview[:180] + "..."

        return {
            "title": driver.title.strip() or "(No title)",
            "current_url": driver.current_url,
            "body_preview": preview or "(Empty body)",
        }
    finally:
        if auto_close:
            driver.quit()
        else:
            logger.warning("Browser tetap terbuka (auto close OFF) | profile=%s", profile_key)


def run_visit_job(
    profile_root: Path,
    profile_key: str,
    url: str,
    wait_timeout: int,
    headless: bool = False,
    auto_close: bool = True,
    locale: str = "id-ID",
    timezone: str = "Asia/Jakarta",
    chromedriver_path: str = "",
    chrome_binary: str = "",
) -> Dict[str, str]:
    return _run_single_visit(
        profile_root=profile_root,
        profile_key=profile_key,
        url=url,
        wait_timeout=wait_timeout,
        headless=headless,
        auto_close=auto_close,
        locale=locale,
        timezone=timezone,
        chromedriver_path=chromedriver_path,
        chrome_binary=chrome_binary,
    )


def run_zoom_signup_initial_job(
    profile_root: Path,
    profile_key: str,
    url: str,
    wait_timeout: int,
    email_domain: str,
    signup_password: str = "",
    trial_days: int = 14,
    payment_vcc: str = "",
    otp_resolver: Callable[[str], str | None] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    headless: bool = False,
    auto_close: bool = True,
    locale: str = "id-ID",
    timezone: str = "Asia/Jakarta",
    chromedriver_path: str = "",
    chrome_binary: str = "",
) -> Dict[str, str]:
    return _run_zoom_signup_initial(
        profile_root=profile_root,
        profile_key=profile_key,
        url=url,
        wait_timeout=wait_timeout,
        email_domain=email_domain,
        signup_password=signup_password,
        trial_days=trial_days,
        payment_vcc=payment_vcc,
        otp_resolver=otp_resolver,
        progress_callback=progress_callback,
        headless=headless,
        auto_close=auto_close,
        locale=locale,
        timezone=timezone,
        chromedriver_path=chromedriver_path,
        chrome_binary=chrome_binary,
    )


def run_batch_visit_job(
    profile_root: Path,
    profile_key: str,
    url: str,
    batch_count: int,
    wait_timeout: int,
    headless: bool = False,
    auto_close: bool = True,
    locale: str = "id-ID",
    timezone: str = "Asia/Jakarta",
    chromedriver_path: str = "",
    chrome_binary: str = "",
) -> Dict[str, Any]:
    total = max(1, int(batch_count))
    runs: list[dict[str, Any]] = []
    success_count = 0

    for index in range(1, total + 1):
        try:
            result = _run_single_visit(
                profile_root=profile_root,
                profile_key=profile_key,
                url=url,
                wait_timeout=wait_timeout,
                headless=headless,
                auto_close=auto_close,
                locale=locale,
                timezone=timezone,
                chromedriver_path=chromedriver_path,
                chrome_binary=chrome_binary,
            )
            success_count += 1
            runs.append(
                {
                    "index": index,
                    "status": "success",
                    "title": result["title"],
                    "current_url": result["current_url"],
                    "body_preview": result["body_preview"],
                }
            )
        except Exception as exc:
            runs.append(
                {
                    "index": index,
                    "status": "failed",
                    "error": str(exc),
                }
            )

    return {
        "requested_count": total,
        "success_count": success_count,
        "failed_count": total - success_count,
        "runs": runs,
    }
