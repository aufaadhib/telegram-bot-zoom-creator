from pathlib import Path
from typing import Any, Dict

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


def _build_options(profile_root: Path, profile_key: str, headless: bool, chrome_binary: str) -> Options:
    profile_path = profile_root / f"user_{profile_key}"
    profile_path.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={profile_path.resolve()}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    if headless:
        options.add_argument("--headless=new")
    if chrome_binary:
        options.binary_location = chrome_binary

    return options


def _build_service(chromedriver_path: str) -> Service:
    if chromedriver_path:
        return Service(chromedriver_path)
    return Service(ChromeDriverManager().install())


def _run_single_visit(
    profile_root: Path,
    profile_key: str,
    url: str,
    wait_timeout: int,
    headless: bool = False,
    chromedriver_path: str = "",
    chrome_binary: str = "",
) -> Dict[str, str]:
    driver = webdriver.Chrome(
        service=_build_service(chromedriver_path),
        options=_build_options(profile_root, profile_key, headless, chrome_binary),
    )
    wait = WebDriverWait(driver, wait_timeout)

    try:
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
        driver.quit()


def run_visit_job(
    profile_root: Path,
    profile_key: str,
    url: str,
    wait_timeout: int,
    headless: bool = False,
    chromedriver_path: str = "",
    chrome_binary: str = "",
) -> Dict[str, str]:
    return _run_single_visit(
        profile_root=profile_root,
        profile_key=profile_key,
        url=url,
        wait_timeout=wait_timeout,
        headless=headless,
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
