import os
import re
import time
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException, ElementClickInterceptedException, TimeoutException
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urljoin, quote

# --- Strict explicit imports from scraper_utils ---
from .scraper_utils import is_no_match_page, try_close_overlays, get_stealth_driver, human_mimic_nudge

# --- Local config ---
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

# ─────────────────────────────────────────────────────────
#  HELPER: Build a direct DriveArabia URL from filters
# ─────────────────────────────────────────────────────────
def _build_drivearabia_url(make=None, model_value=None, body_type=None,
                           year_min=None, year_max=None, page=1):
    """
    Build the direct URL for DriveArabia based on discovered URL pattern:
      /carprices/oman/{make-slug}/{make-model-slug}/?bodyType=suv&minYear=2020&maxYear=2020&page=2
    """
    base = "https://www.drivearabia.com/carprices/oman/"

    # Build path slug for make/model
    if make:
        make_slug = make.strip().lower().replace(" ", "-")
        base += f"{make_slug}/"
        if model_value:
            model_slug = model_value.strip().lower().replace(" ", "-")
            # DriveArabia model slug = "{make}-{model}" (e.g. toyota-land-cruiser)
            full_model_slug = f"{make_slug}-{model_slug}"
            base += f"{full_model_slug}/"

    # Build query parameters
    params = []
    if body_type:
        params.append(f"bodyType={body_type.strip().lower()}")
    if year_min:
        params.append(f"minYear={year_min}")
    if year_max:
        params.append(f"maxYear={year_max}")
    if page and page > 1:
        params.append(f"page={page}")

    if params:
        base += "?" + "&".join(params)

    return base


# ─────────────────────────────────────────────────────────
#  PRIMARY: Fast URL-based scraper (no dropdown clicking)
# ─────────────────────────────────────────────────────────
def _scrape_drivearabia_fast(make=None, model_value=None, body_type=None,
                              year_min=None, year_max=None, page_num=1,
                              driver_path='chromedriver.exe', headless=True):
    """
    PRIMARY scraper: Constructs the URL directly with all filters baked in,
    loads it in Selenium (needed because site blocks raw HTTP requests with 403),
    then parses the rendered HTML with BeautifulSoup. No dropdown clicking needed!
    """
    print(f"🟢 DriveArabia FAST scraper started ({'Headless' if headless else 'Visible'} Mode)")
    dp = driver_path if driver_path and os.path.isfile(str(driver_path)) else None
    driver = get_stealth_driver(dp, headless=headless, user_agent=USER_AGENT)

    cars = []
    try:
        for page_idx in range(page_num):
            url = _build_drivearabia_url(
                make=make, model_value=model_value, body_type=body_type,
                year_min=year_min, year_max=year_max, page=page_idx + 1
            )
            print(f"🌍 Loading DriveArabia URL: {url}")
            driver.get(url)
            time.sleep(3)  # Let JS render the car cards

            # Grab rendered HTML and parse with BeautifulSoup
            soup = BeautifulSoup(driver.page_source, "html.parser")

            # Find all car cards (div with class rounded-10)
            card_elements = soup.find_all("div", class_="rounded-10")
            print(f"📊 Found {len(card_elements)} car cards on page {page_idx + 1}")

            if not card_elements:
                # If no cards found on first page, this URL pattern may not work
                if page_idx == 0:
                    print("⚠️ No car cards found - URL pattern may be wrong")
                    return None  # Signal to fall back to legacy scraper
                else:
                    print("📄 No more pages available")
                    break

            for card in card_elements:
                # Car Name from h2
                h2 = card.find("h2")
                car_name = h2.get_text(strip=True).replace('\n', ' ') if h2 else ""

                # Price from span.text-black-1
                price_span = card.find("span", class_="text-black-1")
                price = price_span.get_text(strip=True) if price_span else ""

                # Body Type
                bt_span = card.find("span", string=re.compile(r"Body Type", re.I))
                bt_val = ""
                if bt_span:
                    bt_p = bt_span.find_next_sibling("p")
                    if bt_p:
                        bt_val = bt_p.get_text(strip=True)

                # Fuel Efficiency
                fuel_span = card.find("span", string=re.compile(r"Fuel Efficiency", re.I))
                fuel_val = ""
                if fuel_span:
                    fuel_p = fuel_span.find_next_sibling("p")
                    if fuel_p:
                        fuel_val = fuel_p.get_text(strip=True)

                # Link
                link = "N/A"
                if h2:
                    parent_a = h2.find_parent("a")
                    if parent_a and parent_a.get("href"):
                        link = urljoin("https://www.drivearabia.com", parent_a["href"])

                if car_name or price:
                    cars.append({
                        "Car Name": car_name,
                        "Price": price,
                        "Body Type": bt_val,
                        "Fuel Efficiency": fuel_val,
                        "Source": "DriveArabia",
                        "link": link
                    })

    except Exception as e:
        print(f"❌ DriveArabia FAST scraper error: {e}")
        if not cars:
            return None  # Signal fallback
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        try:
            del driver
        except Exception:
            pass

    print(f"✅ DriveArabia FAST scraped {len(cars)} cars")
    return cars


# ─────────────────────────────────────────────────────────
#  FALLBACK: Legacy Selenium click-based scraper
# ─────────────────────────────────────────────────────────
def _scrape_drivearabia_legacy(country="Oman", make=None, model_value=None,
                                body_type=None, price_min=None, price_max=None,
                                year_min=None, year_max=None, page_num=1,
                                driver_path='chromedriver.exe', headless=True):
    """
    FALLBACK scraper: The original Selenium-based approach that clicks through
    dropdowns. Used only when the fast URL-based approach fails.
    """
    print(f"🟡 DriveArabia LEGACY scraper started ({'Headless' if headless else 'Visible'} Mode)")
    dp = driver_path if driver_path and os.path.isfile(str(driver_path)) else None
    driver = get_stealth_driver(dp, headless=headless, user_agent=USER_AGENT)
    driver.get('https://www.drivearabia.com/carprices/oman/')
    print("🌍 DriveArabia page loaded")
    time.sleep(2)
    human_mimic_nudge(driver)
    try_close_overlays(driver)

    def _make_model_query():
        parts = []
        if make:
            parts.append(str(make).strip())
        if model_value:
            parts.append(str(model_value).strip())
        return " ".join(parts).strip()

    def _click_first_visible_suggestion():
        """Click first autocomplete result (li with u tag highlight)."""
        SUGGESTION_XP = "(//li[contains(@class,'cursor-pointer') and .//u])[1]"
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                for el in driver.find_elements(By.XPATH, SUGGESTION_XP):
                    try:
                        if el.is_displayed():
                            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                            time.sleep(0.1)
                            try:
                                el.click()
                            except (ElementClickInterceptedException, StaleElementReferenceException):
                                driver.execute_script("arguments[0].click();", el)
                            return True
                    except StaleElementReferenceException:
                        continue
            except StaleElementReferenceException:
                pass
            time.sleep(0.15)
        # Fallback: any visible div[@data-option]
        deadline2 = time.time() + 5
        while time.time() < deadline2:
            for el in driver.find_elements(By.XPATH, "//div[@data-option]"):
                try:
                    if el.is_displayed() and el.is_enabled():
                        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                        time.sleep(0.1)
                        try:
                            el.click()
                        except (ElementClickInterceptedException, StaleElementReferenceException):
                            driver.execute_script("arguments[0].click();", el)
                        return True
                except StaleElementReferenceException:
                    continue
            time.sleep(0.15)
        return False

    query = _make_model_query()
    if query:
        try:
            print(f"🔎 Setting make/model: {query}")
            make_model_box = WebDriverWait(driver, 12).until(
                EC.element_to_be_clickable((By.XPATH, "//input[@placeholder='Search Make, Model']"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", make_model_box)
            time.sleep(0.15)
            make_model_box.click()
            time.sleep(0.1)
            make_model_box.clear()
            make_model_box.send_keys(query)
            time.sleep(0.6)

            if _click_first_visible_suggestion():
                print(f"✅ Make/Model '{query}' selected")
                time.sleep(1.0)
            else:
                print(f"⚠️ No visible suggestion for: {query!r}")
        except Exception as e:
            print(f"❌ Make/Model filter error: {e}")

    if body_type:
        try:
            print(f"🔎 Setting body type: {body_type}")
            body_type_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[.//div[text()='Body Type'] or normalize-space()='Body Type']"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", body_type_btn)
            time.sleep(0.15)
            driver.execute_script("arguments[0].click();", body_type_btn)
            time.sleep(0.8)

            body_opt = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH,
                    f"//button[normalize-space()='{body_type}' or normalize-space()='{body_type.upper()}' or normalize-space()='{body_type.capitalize()}']"))
            )
            driver.execute_script("arguments[0].click();", body_opt)
            print(f"✅ Body type '{body_type}' selected")
            time.sleep(0.5)

            try:
                apply_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH,
                        "//button[normalize-space(text())='Apply' and (contains(@class,'bg-brand') or contains(@class,'brand'))]"))
                )
                driver.execute_script("arguments[0].click();", apply_btn)
                print("✅ Body type filter applied")
                time.sleep(2)
            except TimeoutException:
                print("⚠️ No Apply button (auto-applied)")
        except Exception as e:
            print(f"❌ Body type filter error: {e}")

    # Year filter
    try:
        if year_min or year_max:
            print("🔎 Opening More Filters for Year...")
            more_filters_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH,
                    "//button[contains(.,'More Filters') or .//span[contains(text(),'More Filters')]]"))
            )
            driver.execute_script("arguments[0].click();", more_filters_btn)
            time.sleep(0.6)

            if year_min:
                print(f"🔎 Opening Year From dropdown...")
                min_dropdown = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH,
                        "//div[h3[normalize-space()='Year'] or div[normalize-space()='Year']]"
                        "//div[contains(@class,'flex-col')][.//div[text()='From']]//button"
                        "| //div[.//div[text()='From'] and .//div[text()='Year']]//button[1]"
                    ))
                )
                driver.execute_script("arguments[0].click();", min_dropdown)
                time.sleep(0.3)
                year_option = WebDriverWait(driver, 10).until(
                    EC.visibility_of_element_located((By.XPATH, f"//div[@data-option='{year_min}']"))
                )
                driver.execute_script("arguments[0].click();", year_option)
                print(f"✅ Min year selected: {year_min}")

            if year_max:
                print(f"🔎 Opening Year To dropdown...")
                max_dropdown = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH,
                        "//div[h3[normalize-space()='Year'] or div[normalize-space()='Year']]"
                        "//div[contains(@class,'flex-col')][.//div[text()='Up to']]//button"
                        "| //div[.//div[text()='Up to'] and .//div[text()='Year']]//button[1]"
                    ))
                )
                driver.execute_script("arguments[0].click();", max_dropdown)
                time.sleep(0.3)
                year_option = WebDriverWait(driver, 10).until(
                    EC.visibility_of_element_located((By.XPATH, f"//div[@data-option='{year_max}']"))
                )
                driver.execute_script("arguments[0].click();", year_option)
                print(f"✅ Max year selected: {year_max}")

            try:
                apply_btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH,
                        "//button[normalize-space(text())='Apply' and (contains(@class,'bg-brand') or contains(@class,'brand'))]"))
                )
                driver.execute_script("arguments[0].click();", apply_btn)
                print("✅ Year filter applied")
                time.sleep(2)
            except TimeoutException:
                print("⚠️ No Apply button for year (auto-applied)")
    except Exception as e:
        print(f"❌ Year filter error: {e}")

    cars = []
    for page_idx in range(page_num):
        time.sleep(1.2)

        try:
            driver.current_url
        except Exception as e:
            print(f"❌ Browser session lost: {e}")
            break

        try:
            card_links = driver.find_elements(By.CLASS_NAME, "rounded-10")
            print(f"📊 Found {len(card_links)} car cards on page {page_idx + 1}")
        except Exception as e:
            print(f"❌ Error finding car cards: {e}")
            break

        for card in card_links:
            try:
                car_name = card.find_element(By.TAG_NAME, "h2").text.replace('\n', ' ').strip()
            except Exception:
                car_name = ""
            try:
                price = card.find_element(By.CSS_SELECTOR, "span.text-black-1").text.strip()
            except Exception:
                price = ""
            try:
                body_type = card.find_element(
                    By.XPATH, ".//span[contains(text(),'Body Type')]/following-sibling::p"
                ).text.strip()
            except Exception:
                body_type = ""
            try:
                fuel_eff = card.find_element(
                    By.XPATH, ".//span[contains(text(),'Fuel Efficiency')]/following-sibling::p"
                ).text.strip()
            except Exception:
                fuel_eff = ""

            try:
                link_elem = card.find_element(By.XPATH, ".//h2/ancestor::a[1]")
                href = link_elem.get_attribute("href")
                link = urljoin("https://www.drivearabia.com", href)
            except:
                link = "N/A"

            if car_name or price:
                cars.append({
                    "Car Name": car_name,
                    "Price": price,
                    "Body Type": body_type,
                    "Fuel Efficiency": fuel_eff,
                    "Source": "DriveArabia",
                    "link": link
                })
        # Pagination
        if page_idx < page_num - 1:
            try:
                driver.current_url
                next_btn = driver.find_element(By.XPATH, '//a[@rel="next"]')
                if next_btn and next_btn.is_enabled():
                    print(f"🔄 Going to page {page_idx + 2}")
                    next_btn.click()
                    time.sleep(2)
                else:
                    print("📄 No more pages available")
                    break
            except Exception as e:
                print(f"❌ Pagination error: {e}")
                break

    try:
        driver.quit()
    except Exception:
        pass
    try:
        del driver
    except Exception:
        pass
    print(f"✅ DriveArabia LEGACY scraped {len(cars)} cars")
    return cars


# ─────────────────────────────────────────────────────────
#  PUBLIC ENTRY POINT: Try fast first, fall back to legacy
# ─────────────────────────────────────────────────────────
def scrape_drivearabia(
    country="Oman",
    make=None,
    model_value=None,
    body_type=None,
    price_min=None,
    price_max=None,
    year_min=None,
    year_max=None,
    page_num=1,
    driver_path='chromedriver.exe',
    headless=True
):
    """
    Main entry point. Tries the FAST URL-based approach first.
    If it returns None (meaning URL pattern failed), falls back to LEGACY click-based scraper.
    """
    # ── Attempt 1: Fast URL-based scraper ──
    print("🚀 Attempting DriveArabia FAST (URL-based) scraper...")
    try:
        result = _scrape_drivearabia_fast(
            make=make, model_value=model_value, body_type=body_type,
            year_min=year_min, year_max=year_max, page_num=page_num,
            driver_path=driver_path, headless=headless
        )
        if result is not None:
            print(f"✅ DriveArabia FAST scraper succeeded with {len(result)} results")
            return result
        else:
            print("⚠️ DriveArabia FAST scraper returned None — falling back to LEGACY")
    except Exception as e:
        print(f"❌ DriveArabia FAST scraper crashed: {e} — falling back to LEGACY")

    # ── Attempt 2: Legacy click-based scraper ──
    print("🔄 Starting DriveArabia LEGACY (click-based) scraper...")
    try:
        return _scrape_drivearabia_legacy(
            country=country, make=make, model_value=model_value,
            body_type=body_type, price_min=price_min, price_max=price_max,
            year_min=year_min, year_max=year_max, page_num=page_num,
            driver_path=driver_path, headless=headless
        )
    except Exception as e:
        print(f"❌ DriveArabia LEGACY scraper also failed: {e}")
        return []