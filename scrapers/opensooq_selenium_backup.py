import re
import time
from urllib.parse import urljoin
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    TimeoutException,
)

from .scraper_utils import (
    get_stealth_driver_opensooq,
    safe_click,
)

# --- Overlay Killer (call before each input) ---
def close_overlays(driver):
    selectors = [
        "span.pointer",                 # OpenSooq ad close
        ".cookie-banner button",        # Common cookie button
        ".modal-close",                 # Generic modal close
        "button[aria-label='close']",   # Common aria close
        "button.close",                 # Bootstrap modal
    ]
    for sel in selectors:
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    driver.execute_script("arguments[0].click();", btn)
                except Exception:
                    pass
        except Exception:
            pass

def robust_input(driver, by, value, input_text):
    elem = WebDriverWait(driver, 15).until(EC.element_to_be_clickable((by, value)))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
    time.sleep(0.15)
    close_overlays(driver)
    try:
        elem.click()
    except (ElementClickInterceptedException, ElementNotInteractableException):
        driver.execute_script("arguments[0].click();", elem)
    time.sleep(0.15)
    try:
        elem.clear()
    except Exception:
        pass
    time.sleep(0.08)
    elem.send_keys(str(input_text))
    time.sleep(0.3)
    return elem

def robust_click(driver, by, value):
    elem = WebDriverWait(driver, 15).until(EC.element_to_be_clickable((by, value)))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", elem)
    time.sleep(0.15)
    close_overlays(driver)
    try:
        elem.click()
    except (ElementClickInterceptedException, ElementNotInteractableException):
        driver.execute_script("arguments[0].click();", elem)
    time.sleep(0.3)
    return elem

def scrape_opensooq(
    make=None,
    model_value=None,
    body_type=None,
    price_min=None,
    price_max=None,
    year_min=None,
    year_max=None,
    page_num=1,
    driver_path='chromedriver.exe',
    headless=True  # Back to headless mode
):
    print("🟢 OpenSooq scraper started")

    # ---------------------------
    # LISTINGS
    # ---------------------------
    def scrape_car_listings(driver):
        wait = WebDriverWait(driver, 15)
        cars = []

        CARD_XP = ("//a[@data-post-index and normalize-space(@data-is-recommended)='false' "
           "and contains(concat(' ', normalize-space(@class), ' '), ' postListItemData ')]")



        NAME_REL  = ".//h2[contains(@class,'breakWord')]"
        META_REL  = ".//p"
        PRICE_REL = ".//div[contains(@class,'priceColor')]"
        NEXT_BTNS = [
            "//a[@data-id='nextPageArrow']",
            "//a[@title='Go to next page']"
        ]

        def get_next_button():
            for xp in NEXT_BTNS:
                try:
                    btn = WebDriverWait(driver, 6).until(
                        EC.element_to_be_clickable((By.XPATH, xp))
                    )
                    if btn:
                        return btn
                except Exception:
                    continue
            return None

        for page in range(max(1, page_num)):
            time.sleep(1.2)
            try:
                wait.until(EC.presence_of_all_elements_located((By.XPATH, CARD_XP)))
            except TimeoutException:
                print(f"⚠️ No cards found on page {page+1}; stopping.")
                break

            cards_on_page = driver.find_elements(By.XPATH, CARD_XP)
            first_href = cards_on_page[0].get_attribute("href") if cards_on_page else None

            for card in cards_on_page:
                try:
                    car_name, meta_text, price, year, kms, body, link = "", "", "", "", "", "", ""

                    try:
                        car_name = card.find_element(By.XPATH, NAME_REL).text.strip()
                    except:
                        pass

                    try:
                        meta_text = card.find_element(By.XPATH, META_REL).text.strip()
                        parts = [p.strip() for p in meta_text.split(",") if p.strip()]

                        year = ""
                        for p in parts:
                            if re.search(r"\b(19|20)\d{2}\b", p):
                                year = re.search(r"\b(19|20)\d{2}\b", p).group(0)
                                break

                        km_matches = re.findall(
                            r'[+]?[\d]{1,3}(?:,\d{3})*(?:\s*-\s*[+]?[\d]{1,3}(?:,\d{3})*)?\s*km',
                            meta_text, flags=re.IGNORECASE
                        )
                        kms = max(km_matches, key=len).strip() if km_matches else ""

                        body = ""
                        for p in parts:
                            m = re.search(r"(sedan|suv|hatchback|pickup|coupe|convertible|truck|bus\s*-\s*van)",
                                        p, re.IGNORECASE)
                            if m:
                                body = m.group(0).strip().upper()
                                break

                    except Exception as e:
                        print("⚠️ Meta parse failed:", e)

                    try:
                        price = card.find_element(By.XPATH, PRICE_REL).text.strip()
                    except:
                        pass

                    try:
                        link = urljoin("https://om.opensooq.com", card.get_attribute("href"))
                    except Exception:
                        link = ""

                    if not car_name and not price:
                        continue

                    cars.append({
                        "Car Name": car_name,
                        "Price": price,
                        "Year": year,
                        "Kilometers": kms,
                        "Body Type": body,
                        "Meta": meta_text,
                        "Source": "OpenSooq",
                        "link": link
                    })

                except StaleElementReferenceException:
                    print("⚠️ Stale card element — skipping")
                    continue

            if page == page_num - 1 or not cards_on_page:
                break

            next_btn = get_next_button()
            if not next_btn:
                print("ℹ️ No next button; stopping.")
                break

            url_before = driver.current_url
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", next_btn)
                time.sleep(0.2)
                try:
                    next_btn.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", next_btn)
            except Exception as e:
                print(f"⚠️ Next page click failed: {e}")
                break

            # wait for change
            changed = False
            try:
                WebDriverWait(driver, 6).until(EC.staleness_of(cards_on_page[0]))
                changed = True
            except Exception:
                pass
            if not changed and first_href:
                try:
                    WebDriverWait(driver, 10).until(
                        lambda d: (
                            d.find_elements(By.XPATH, CARD_XP)
                            and d.find_elements(By.XPATH, CARD_XP)[0].get_attribute("href") != first_href
                        )
                    )
                    changed = True
                except Exception:
                    pass
            if not changed:
                try:
                    WebDriverWait(driver, 6).until(lambda d: d.current_url != url_before)
                    changed = True
                except Exception:
                    pass
            if not changed:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.8)
                driver.execute_script("window.scrollTo(0, 0);")

        print(f"✅ OpenSooq collected {len(cars)} rows across ≤{page_num} page(s).")
        return cars

    # ---------------------------
    # MAIN DRIVER
    # ---------------------------
    driver = get_stealth_driver_opensooq(driver_path, headless=headless)
    try:
        driver.get("https://om.opensooq.com/en/cars/cars-for-sale")
        wait = WebDriverWait(driver, 15)
        print("🌍 OpenSooq page loaded")
        # ── Give OpenSooq time to finish its internal reload cycle ──
        time.sleep(5.0)
        close_overlays(driver)

        def _wait_stable(seconds=7, label="page"):
            """Wait for OpenSooq to stop reloading/re-rendering after a filter change."""
            print(f"⏳ Stabilizing after {label} ({seconds}s)...")
            time.sleep(seconds)


        # --- Filters ---
        if make:
            print(f"🔎 Setting make: {make}")
            robust_input(driver, By.XPATH, "//div[@data-id='car_make']//input[@placeholder='Select Car Make']", make)

            time.sleep(1.0)
            robust_click(driver, By.XPATH, "//div[@data-id='car_make']//li[@data-id='car_make_0']/label")
            _wait_stable(7, "make selection")

        if model_value:
            print(f"🔎 Setting model: {model_value}")
            # Open the model dropdown first (it's CLOSED by default, unlike make which is pre-open)
            try:
                hdr = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//div[@data-id='car_model']//div[contains(@class,'dropDownHeader')]"))
                )
                driver.execute_script("arguments[0].click();", hdr)
                time.sleep(0.5)
            except Exception:
                pass
            # Type to filter
            robust_input(driver, By.XPATH, "//div[@data-id='car_model']//input[@placeholder='Select Model']", model_value)
            time.sleep(1.5)  # give dropdown time to populate
            # Click the first filtered result
            try:
                first_option = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, "//div[@data-id='car_model']//li[@data-id='car_model_0']/label"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", first_option)
                time.sleep(0.3)
                driver.execute_script("arguments[0].click();", first_option)
                print("✅ Model selected")
            except Exception as e:
                print("⚠️ Could not select model:", e)
            _wait_stable(6, "model selection")


        # Year
        if year_min or year_max:
            try:
                print("🔎 Opening Year dropdown...")
                robust_click(driver, By.XPATH, "//button[.//h3[normalize-space(text())='Year']]")
                time.sleep(1.5)
            except Exception as e:
                print(f"⚠️ Could not open Year dropdown: {e}")

        if year_min:
            print(f"🔎 Setting min year: {year_min}")
            robust_input(driver, By.XPATH, "//input[@data-id='range_from_Car_Year' or @placeholder='From']", year_min)
            time.sleep(1.0)
            try:
                year_option = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.XPATH, f"//ul[contains(@class,'dropdownContent')]//li[normalize-space(text())='{year_min}']"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", year_option)
                year_option.click()
                print("✅ Min year selected")
            except Exception:
                print("⚠️ Min year option not found, typed only")

        if year_max:
            print(f"🔎 Setting max year: {year_max}")
            robust_input(driver, By.XPATH, "//input[@data-id='range_to_Car_Year' or @placeholder='To']", year_max)
            time.sleep(1.0)
            try:
                year_option = WebDriverWait(driver, 8).until(
                    EC.element_to_be_clickable((By.XPATH, f"//ul[contains(@class,'dropdownContent')]//li[normalize-space(text())='{year_max}']"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", year_option)
                year_option.click()
                print("✅ Max year selected")
            except Exception:
                print("⚠️ Max year option not found, typed only")

        if year_min or year_max:
            try:
                print("📌 Applying Year filter...")
                filter_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(@class,'whiteBtn') and contains(@class,'blueBtnOutside') and normalize-space(text())='Filter']"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", filter_btn)
                time.sleep(0.5)
                driver.execute_script("arguments[0].click();", filter_btn)
                print("✅ Year filter applied — waiting for page to stabilize...")
                _wait_stable(7, "year filter")
            except Exception as e:
                print(f"⚠️ Could not click Year filter button: {e}")

        # Body type
        if body_type:
            print(f"🔎 Setting body type: {body_type}")
            try:
                robust_click(driver, By.XPATH, "//div[@id='advance_filter']//button[.//div[normalize-space(text())='More Options']]")
                time.sleep(2.0)
            except Exception:
                pass

            robust_input(driver, By.XPATH, "//div[@data-id='Cars_body_types']//input[@placeholder='Select Body Type']", body_type)
            time.sleep(1.0)
            try:
                body_opt = WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.XPATH, "//div[@data-id='Cars_body_types']//ul[contains(@class,'dropdownContent')]//li"))
                )
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", body_opt)
                driver.execute_script("arguments[0].click();", body_opt)
                print("✅ Body type selected")
            except Exception as e:
                print(f"⚠️ Could not select body type option: {e}")
            _wait_stable(6, "body type selection")

        # Final Filter button
        try:
            filter_btn3 = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(@class,'blueBtn') and contains(text(),'Filter')]"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", filter_btn3)
            time.sleep(0.6)
            driver.execute_script("arguments[0].click();", filter_btn3)
            print("✅ Final Filter clicked — waiting for results...")
            _wait_stable(7, "final filter")
        except Exception as e:
            print("⚠️ Could not click Filter button:", e)

        return scrape_car_listings(driver)

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        try:
            del driver
        except Exception:
            pass
