import os
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import ElementClickInterceptedException, TimeoutException
from PIL import Image

# Strict explicit imports from scraper_utils!
from .scraper_utils import (
    USER_AGENT,
    DOWNLOAD_PATH,
    enable_verbatim_via_url,
    extract_search_results,
    is_known_captcha_site,
    filter_high_res,
    download_images,
    get_stealth_driver,
    extract_bidfax_gallery,
    extract_statvin_gallery,
    extract_plc_gallery,
    extract_autohelperbot_gallery,
    extract_carcheckby_gallery,
)

def google_chasis_image_search(chasis_no, headless=True, max_sites=10):
    """
    DEPRECATED: Google search is disabled due to CAPTCHA issues.
    This function now redirects to DuckDuckGo search.
    """
    print("⚠️ Google search is disabled. Redirecting to DuckDuckGo...")
    
    try:
        from .duckduckgo_search import duckduckgo_image_search
        return duckduckgo_image_search(chasis_no, headless=headless, max_sites=max_sites)
    except ImportError as e:
        print(f"❌ Failed to import DuckDuckGo search: {e}")
        return {
            "found_images": [],
            "gallery_source": [],
            "first_gallery_page": None,
            "downloaded_images": [],
            "error": "DuckDuckGo search module not available"
        }


def google_chasis_image_search_original(chasis_no, headless=True, max_sites=10):
    from urllib.parse import urlparse
    import random

    os.makedirs(DOWNLOAD_PATH, exist_ok=True)
    downloaded, found, sources = [], [], []
    first_page = None
    driver = None

    try:
        # Always use get_stealth_driver for browser
        driver = get_stealth_driver(headless=headless, user_agent=USER_AGENT)
        wait = WebDriverWait(driver, 15)
        
        # Add random delays to appear more human
        def human_delay(min_sec=0.5, max_sec=2.0):
            time.sleep(random.uniform(min_sec, max_sec))
        
        # 1) Google homepage with random delay
        human_delay(0.5, 1.5)
        driver.get("https://www.google.com/")
        human_delay(1.0, 2.5)
        
        # Handle consent more naturally
        try:
            consent = wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(text(),'I agree') or contains(text(),'Accept all')]")
            ))
            human_delay(0.3, 0.8)
            consent.click()
            human_delay(0.5, 1.2)
        except Exception:
            pass

        # Check if we hit the sorry page
        if "/sorry/" in driver.current_url:
            print("⚠️ GOOGLE IMAGE: Detected CAPTCHA page, taking screenshot...")
            screenshot_path = os.path.join(DOWNLOAD_PATH, f"captcha_{chasis_no}.png")
            driver.save_screenshot(screenshot_path)
            return {
                "found_images": [],
                "gallery_source": ["google.com"],
                "first_gallery_page": driver.current_url,
                "downloaded_images": [screenshot_path],
                "error": "Google CAPTCHA detected"
            }

        # ===== usage =====
        print("🔍 GOOGLE IMAGE: Searching for chassis number...")
        fast_dismiss_overlays(driver)
        human_delay(0.5, 1.0)

        q = get_searchbox(driver, timeout=6)
        try:
            clear_type_submit(q, f'"{chasis_no}"')
        except ElementClickInterceptedException:
            fast_dismiss_overlays(driver)
            human_delay(0.3, 0.7)
            q = get_searchbox(driver, timeout=6)
            clear_type_submit(q, f'"{chasis_no}"')

        # Random wait for navigation
        human_delay(1.5, 3.0)
        print("🔍 GOOGLE IMAGE: Search query submitted")

        # Check again for CAPTCHA after search
        if "/sorry/" in driver.current_url:
            print("⚠️ GOOGLE IMAGE: Hit CAPTCHA after search, attempting fallback search...")
            # Try a direct search URL approach
            search_url = f"https://www.google.com/search?q=%22{chasis_no}%22&hl=en&gl=us"
            human_delay(1.0, 2.0)
            driver.get(search_url)
            human_delay(1.5, 2.5)
            
            # If still blocked, try alternative search engines
            if "/sorry/" in driver.current_url:
                print("⚠️ GOOGLE IMAGE: Blocked by Google, trying alternative search engines...")
                
                # Take screenshot and return with error
                print("⚠️ GOOGLE IMAGE: Google CAPTCHA detected, please use DuckDuckGo search instead")
                screenshot_jpg = os.path.join(DOWNLOAD_PATH, f"screenshot_{chasis_no}.jpg")
                driver.save_screenshot(screenshot_jpg)
                return {
                    "found_images": [],
                    "gallery_source": ["google.com"],
                    "first_gallery_page": driver.current_url,
                    "downloaded_images": [screenshot_jpg],
                    "error": "Google CAPTCHA detected - please use DuckDuckGo search"
                }

        # 3) Verbatim mode via URL toggle
        print("🔍 GOOGLE IMAGE: Enabling verbatim mode via URL parameter...")
        reloaded = enable_verbatim_via_url(driver)
        if reloaded:
            fast_dismiss_overlays(driver)
            human_delay(0.5, 1.5)

        # 4) Harvest links with retry mechanism
        print("🔍 GOOGLE IMAGE: Extracting search results...")
        links = extract_search_results(driver)
        
        # If no links found, try scrolling to trigger lazy loading
        if not links:
            print("🔍 GOOGLE IMAGE: No links found, attempting scroll...")
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
            human_delay(1.0, 2.0)
            links = extract_search_results(driver)
        
        print(f"🔍 GOOGLE IMAGE: Found {len(links)} search result links")

        # 5) Visit results normally
        for url in links[:max_sites]:
            if is_known_captcha_site(url):
                continue
            try:
                driver.set_page_load_timeout(20)
                driver.get(url)
                time.sleep(3)
            except Exception:
                continue

            curr = driver.current_url
            # Do not skip known auction/gallery domains; we have explicit extractors below
            if chasis_no.lower() not in driver.page_source.lower():
                continue

            first_page = curr

            # Delegate to the proper gallery extraction
            if "bidfax.info" in curr:
                raw = extract_bidfax_gallery(driver, wait)
            elif "stat.vin" in curr:
                raw = extract_statvin_gallery(driver, wait)
            elif "plc.auction" in curr or "plc.ua" in curr:
                raw = extract_plc_gallery(driver, wait)
            elif "autohelperbot.com" in curr:
                raw = extract_autohelperbot_gallery(driver, wait, chasis_no)
            elif "carcheck.by" in curr:
                raw = extract_carcheckby_gallery(driver, wait, chasis_no)
                first_page = driver.current_url
            else:
                continue

            highres = filter_high_res(raw)
            if highres:
                saved = download_images(highres, DOWNLOAD_PATH, curr)
                if saved:
                    downloaded, found = saved, highres
                    sources.append(urlparse(curr).netloc)
                    break

        # 6) Fallback: if no images found OR no links found, take a screenshot
        if not downloaded or len(links) == 0:
            print(f"🔍 GOOGLE IMAGE: No images found from {len(links)} links, taking screenshot fallback")

            # If we have no links at all, screenshot the search results page itself (likely CAPTCHA)
            if len(links) == 0:
                print("🔍 GOOGLE IMAGE: No search results found, screenshotting search page (likely CAPTCHA)")
                screenshot_png = os.path.join(DOWNLOAD_PATH, f"screenshot_{chasis_no}.png")
                driver.save_screenshot(screenshot_png)

                # Convert PNG → JPG
                screenshot_jpg = os.path.join(DOWNLOAD_PATH, f"screenshot_{chasis_no}.jpg")
                Image.open(screenshot_png).convert("RGB").save(screenshot_jpg, "JPEG")

                # Remove PNG
                os.remove(screenshot_png)

                first_page = driver.current_url
                sources = ["google.com"]
                downloaded = [screenshot_jpg]

                print(f"🔍 GOOGLE IMAGE: Screenshot saved as fallback")
                return {
                    "found_images": [],
                    "gallery_source": sources,
                    "first_gallery_page": first_page,
                    "downloaded_images": downloaded,
                    "error": None
                }

            # Otherwise, screenshot the first valid page
            for url in links[:max_sites]:
                try:
                    driver.set_page_load_timeout(20)
                    driver.get(url)
                    time.sleep(3)
                except Exception:
                    continue

                # Even if domain is captcha-prone, capture a screenshot as fallback
                print(f"🔍 GOOGLE IMAGE: Taking screenshot of {url}")

                # Take screenshot
                screenshot_png = os.path.join(DOWNLOAD_PATH, f"screenshot_{chasis_no}.png")
                driver.save_screenshot(screenshot_png)

                # Convert PNG → JPG
                screenshot_jpg = os.path.join(DOWNLOAD_PATH, f"screenshot_{chasis_no}.jpg")
                Image.open(screenshot_png).convert("RGB").save(screenshot_jpg, "JPEG")

                # Optionally remove the PNG file
                os.remove(screenshot_png)

                first_page = driver.current_url
                sources = [urlparse(first_page).netloc]

                # Exit the whole function immediately
                downloaded = [screenshot_jpg]  # ✅ Add to downloaded list
                found = [screenshot_jpg]       # Optional, if you want to treat it as "found"

                print(f"🔍 GOOGLE IMAGE: Screenshot saved as fallback")
                return {
                    "found_images": found,
                    "gallery_source": sources,
                    "first_gallery_page": first_page,
                    "downloaded_images": downloaded,
                    "error": None
                }

        return {
            "found_images":       found,
            "gallery_source":     sources,
            "first_gallery_page": first_page,
            "downloaded_images":  downloaded,
            "error":              None if downloaded else "No images found"
        }
    except Exception as e:
        print(f"❌ GOOGLE IMAGE ERROR: {str(e)}")
        return {
            "found_images":       [],
            "gallery_source":     [],
            "first_gallery_page": None,
            "downloaded_images":  [],
            "error":              str(e)
        }
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


SEARCH_CSS = "form[role='search'] textarea[name='q'][role='combobox']"
SEARCH_CSS_FALLBACK = "textarea[name='q'][role='combobox']"
SEARCH_CSS_MIN = "textarea[name='q']"

def fast_dismiss_overlays(driver):
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys(Keys.ESCAPE)
        body.send_keys(Keys.ESCAPE)
    except Exception:
        pass
    # Blur any sticky focus
    try:
        driver.execute_script("if (document.activeElement) document.activeElement.blur();")
    except Exception:
        pass
    # Try a couple of common consent buttons once (no deep scans)
    for sel in [
        "button:contains('Accept all')", "button:contains('I agree')",
        "button:contains('Accept')", "button:contains('Got it')",
        "[role='button']:contains('Accept all')"
    ]:
        try:
            # :contains isn’t standard in CSS; emulate cheaply with XPath one-shots
            btns = driver.find_elements(By.XPATH, f"//button[normalize-space()={repr('Accept all')}]"
                                                  f"|//div[@role='button' and normalize-space()={repr('Accept all')}]")
            if btns:
                btns[0].click()
                break
        except Exception:
            pass

def get_searchbox(driver, timeout=6):
    wait = WebDriverWait(driver, timeout)
    for css in (SEARCH_CSS, SEARCH_CSS_FALLBACK, SEARCH_CSS_MIN):
        try:
            el = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, css)))
            return el
        except TimeoutException:
            continue
    # Last resort: quick XPath attempt (still shallow)
    try:
        return wait.until(EC.visibility_of_element_located(
            (By.XPATH, "//form[@role='search']//textarea[@name='q' and @role='combobox']")))
    except Exception as e:
        raise e

def clear_type_submit(el, text):
    import random
    
    # Prefer JS focus (no layout thrash) + keyboard clear
    try:
        el._parent.execute_script("arguments[0].focus();", el)
    except Exception:
        pass
    
    # Clear existing text more naturally
    try:
        el.click()
        time.sleep(random.uniform(0.1, 0.3))
        el.send_keys(Keys.CONTROL, "a")
        time.sleep(random.uniform(0.1, 0.2))
        el.send_keys(Keys.DELETE)
    except Exception:
        pass
    
    # Type with human-like delays
    for char in text:
        el.send_keys(char)
        time.sleep(random.uniform(0.05, 0.15))  # Random typing speed
    
    # Pause before submit
    time.sleep(random.uniform(0.3, 0.7))
    el.send_keys(Keys.ENTER)