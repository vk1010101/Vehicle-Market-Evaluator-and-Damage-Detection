import os
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException, ElementClickInterceptedException, TimeoutException
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urljoin

# --- Strict explicit imports from scraper_utils ---
from .scraper_utils import is_no_match_page, try_close_overlays, get_stealth_driver, human_mimic_nudge

# --- Local config (can move to constants.py later) ---
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)

# --- Verified working XPaths for Make/Model (tested 2026-04) ---
# Opens the combined Make+Model search dialog
DRIVEARABIA_OPEN_MAKE_MODEL_TRIGGER_XPATH = (
    "(//div[contains(text(),'Search Make, Model')])[2]"
)
# The actual <input> inside the dialog
DRIVEARABIA_MAKE_MODEL_INPUT_XPATH = (
    '//div[@role="dialog"]//input[@placeholder="Search Make, Model" and @type="text"]'
)
# First autocomplete result row (has <u> tag highlighting match)
DRIVEARABIA_FIRST_MAKE_MODEL_RESULT_XPATH = (
    '(//div[@role="dialog"]//li[contains(@class,"cursor-pointer") and .//u])[1]'
)

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
    headless=True  # Back to headless mode
):
    print(f"🟢 DriveArabia scraper started ({'Headless' if headless else 'Visible'} Mode)")
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
        """Click first autocomplete result (li with u tag highlight) — no dialog wrapper needed."""
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
            # The input is directly accessible — no trigger/dialog needed
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

            # Click first autocomplete suggestion (li with <u> highlight, no dialog wrapper)
            if _click_first_visible_suggestion():
                print(f"✅ Make/Model '{query}' selected")
                time.sleep(1.0)  # wait for page to re-render results
            else:
                print(f"⚠️ No visible suggestion for: {query!r}")
        except Exception as e:
            print(f"❌ Make/Model filter error: {e}")

    if body_type:
        try:
            print(f"🔎 Setting body type: {body_type}")
            # Click the Body Type dropdown button to open it
            body_type_btn = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[.//div[text()='Body Type'] or normalize-space()='Body Type']"))
            )
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", body_type_btn)
            time.sleep(0.15)
            driver.execute_script("arguments[0].click();", body_type_btn)
            time.sleep(0.8)

            # Click the specific body type button by text (SUV, Sedan, etc.)
            body_opt = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.XPATH,
                    f"//button[normalize-space()='{body_type}' or normalize-space()='{body_type.upper()}' or normalize-space()='{body_type.capitalize()}']"))
            )
            driver.execute_script("arguments[0].click();", body_opt)
            print(f"✅ Body type '{body_type}' selected")
            time.sleep(0.5)

            # Apply button
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

    # Model is now handled in the combined Make/Model section above
    # No separate model handling needed

    # try:
    #     if price_min:
    #         price_min_btn = driver.find_element(
    #             By.XPATH, '(//button[contains(@class,"bg-brand-2") and .//span[contains(text(),"flex")]])[1]'
    #         )
    #         price_min_btn.click()
    #         time.sleep(1)
    #         price_option = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((
    #             By.CSS_SELECTOR, f"div[data-option='{price_min}']"
    #         )))
    #         price_option.click()
    #         time.sleep(1)
    #     if price_max:
    #         price_max_btn = driver.find_element(
    #             By.XPATH, '(//button[contains(@class,"bg-brand-2") and .//span[contains(text(),"flex")]])[2]'
    #         )
    #         price_max_btn.click()
    #         time.sleep(1)
    #         price_max_options = driver.find_elements(
    #             By.XPATH, "//div[@id='main-content']/div[3]/div/div/div/div[2]/div[3]/div/div[2]/div/div/div"
    #         )
    #         for option in price_max_options:
    #             option_text = option.text.strip().replace(",", "")
    #             if str(price_max) == option_text:
    #                 option.click()
    #                 break
    #         time.sleep(1)
    #     price_apply = driver.find_element(
    #         By.XPATH, "//div[@id='main-content']/div[3]/div/div/div/div[2]/div[3]/div[2]/button"
    #     )
    #     price_apply.click()
    #     time.sleep(2)
    # except Exception as e:
    #     print("Price filter error:", e)

    # Year filter — lives inside "More Filters" panel
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

            # Apply year filter
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
        time.sleep(1.2)  # Reduced to speed up
        
        # Check if driver session is still valid
        try:
            driver.current_url  # This will throw an exception if session is invalid
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
        # Handle pagination if there are more pages
        if page_idx < page_num - 1:  # Not the last page
            try:
                # Check if session is still valid before pagination
                driver.current_url
                
                next_btn = driver.find_element(By.XPATH, '//a[@rel="next"]')
                if next_btn and next_btn.is_enabled():
                    print(f"🔄 Going to page {page_idx + 2}")
                    next_btn.click()
                    time.sleep(2)  # Wait for page to load
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
    print(f"✅ DriveArabia scraped {len(cars)} cars ({'Headless' if headless else 'Visible'} Mode)")
    return cars