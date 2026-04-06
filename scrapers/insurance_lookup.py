import time
from datetime import datetime
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import ElementClickInterceptedException, StaleElementReferenceException


def _retry_click(driver, selector, by=By.ID, retries=3, delay=1.0):
    last_err = None
    for i in range(retries):
        try:
            wait = WebDriverWait(driver, 10)
            el = wait.until(EC.element_to_be_clickable((by, selector)))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            time.sleep(0.5)
            try:
                el.click()
            except (ElementClickInterceptedException, StaleElementReferenceException):
                # Try JS click as fallback if normal click is blocked or element went stale
                driver.execute_script("arguments[0].click();", el)
            return el
        except StaleElementReferenceException:
            print(f"  ⚠️ Stale element during retry {i+1}/{retries} for {selector}, re-finding...")
            time.sleep(0.5)
            continue
        except Exception as e:
            last_err = e
            print(f"  ⚠️ Retry {i+1}/{retries} failed for {selector}: {str(e)[:100]}")
            time.sleep(delay)
    raise RuntimeError(f"Failed to click {selector} after {retries} retries: {last_err}")

def lookup_insurance_claim(chasis_no, driver_path='chromedriver.exe', headless=True):
    import undetected_chromedriver as uc
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-US")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-renderer-backgrounding")
    # Faster load strategy
    try:
        options.page_load_strategy = 'eager'
    except Exception:
        pass
    options.add_argument("--disable-password-manager-reauthentication")
    options.add_argument("--disable-save-password-bubble")
    options.add_argument("--incognito")
    options.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        # Block images to speed up
        "profile.managed_default_content_settings.images": 2
    })
    if headless:
        options.add_argument("--headless=new")
        print("[INFO] Launching browser in HEADLESS mode.")
    else:
        print("[INFO] Launching browser in VISIBLE mode.")

    # Use subprocess mode to avoid destructor warnings on interpreter shutdown
    # Use the physical chromedriver file as requested
    driver = uc.Chrome(driver_executable_path=driver_path, options=options, use_subprocess=True, version_main=146)
    # Page load timeout
    try:
        driver.set_page_load_timeout(15)
    except Exception:
        pass
    # Block heavy resources via CDP (best-effort)
    try:
        driver.execute_cdp_cmd('Network.enable', {})
        driver.execute_cdp_cmd('Network.setBlockedURLs', {
            'urls': ['*.png','*.jpg','*.jpeg','*.gif','*.webp','*.svg','*.mp4','*.woff','*.woff2','*.ttf']
        })
    except Exception:
        pass

    wait = WebDriverWait(driver, 20)  # Increased from 12s — site can be slow
    result = {
        "claim_exists": False,
        "excess_paid": None,
        "popup_html": None,
        "error": None
    }

    try:
        print("🌍 Loading Oman Insurance login page...")
        driver.get("https://www.oman-insurance.com/eInsurance/login.aspx")
        # Login — triple-click to select all existing text then overwrite
        print("🔑 Entering credentials...")
        from selenium.webdriver.common.keys import Keys
        user_field = wait.until(EC.element_to_be_clickable((By.ID, "txtUserName")))
        user_field.click()
        user_field.send_keys(Keys.CONTROL, "a")
        user_field.send_keys(Keys.DELETE)
        user_field.send_keys("callcenter")

        pass_field = wait.until(EC.element_to_be_clickable((By.ID, "txtPassword")))
        pass_field.click()
        pass_field.send_keys(Keys.CONTROL, "a")
        pass_field.send_keys(Keys.DELETE)
        pass_field.send_keys("Ops@2027$$")

        wait.until(EC.element_to_be_clickable((By.ID, "btnLogin"))).click()
        
        # Wait for page to load after login
        time.sleep(2)
        print(f"📄 Current URL after login: {driver.current_url}")
        
        # Check if login was successful by looking for menu elements
        try:
            # Look for any menu element to confirm we're logged in
            menu_elements = driver.find_elements(By.XPATH, "//a[contains(@id,'ctl00_ctl')]")
            print(f"📋 Found {len(menu_elements)} menu elements")
            
            # Try multiple possible IDs for the Enquiry menu
            enquiry_menu = None
            possible_ids = ["ctl00_ctl06_400", "ctl00_ctl05_400", "ctl00_ctl07_400"]
            
            for menu_id in possible_ids:
                try:
                    enquiry_menu = driver.find_element(By.ID, menu_id)
                    print(f"✅ Found Enquiry menu with ID: {menu_id}")
                    break
                except:
                    continue
            
            if not enquiry_menu:
                # Try finding by text
                enquiry_menu = driver.find_element(By.XPATH, "//a[contains(text(),'Enquiry')]")
                # Try search 2 times
                search_clicked = False
                for attempt in range(2):
                    try:
                        search_btn = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "ctl00_ContentPlaceHolder1_btnSearch")))
                        search_btn.click()
                        search_clicked = True
                        break
                    except:
                        time.sleep(1)
                
                if not search_clicked:
                    print("❌ Final fallback click for Search...")
                    driver.execute_script("javascript:__doPostBack('ctl00$ContentPlaceHolder1$btnSearch','')")
                print("✅ Found Enquiry menu by text")
            
            # Open Enquiry menu
            print("📋 Opening Enquiry menu...")
            _retry_click(driver, enquiry_menu.get_attribute("id"))
            print("✅ Opened Enquiry menu")

        except Exception as e:
            print(f"❌ Error finding Enquiry menu: {str(e)}")
            driver.save_screenshot("static/converted_images/insurance_login_error.png")
            raise

        # Vehicle Search Claim tab
        print("🔎 Opening Vehicle Search Claim tab...")
        time.sleep(2)  # Wait for tab list to be stable
        _retry_click(driver, "ctl00_ContentPlaceHolder1_tabQuickSearch_vehicleSerachClaim_lblvehicleSearchClaim")
        time.sleep(1.5)  # Wait for tab content to load


        # Vehicle search radio button
        print("🎯 Selecting Vehicle search radio button...")
        time.sleep(1)  # Wait for radio buttons to appear correctly
        _retry_click(driver, "ctl00_ContentPlaceHolder1_tabQuickSearch_vehicleSerachClaim_rdvehicleSearchVehicleClaim")
        time.sleep(1)  # Wait for form to stabilize after radio button click


        # Enter chasis number
        print(f"✍️ Entering chasis number: {chasis_no}")
        chasis_box = wait.until(EC.element_to_be_clickable(
            (By.ID, "ctl00_ContentPlaceHolder1_tabQuickSearch_vehicleSerachClaim_txtVehcileSearchChassisNoInputClaim")
        ))
        chasis_box.clear()
        time.sleep(0.3)
        chasis_box.send_keys(chasis_no)
        time.sleep(0.5)

        # Search
        print("🔍 Clicking Search...")
        _retry_click(driver, "ctl00_ContentPlaceHolder1_tabQuickSearch_vehicleSerachClaim_btnVheicleSearchButtonClaim")
        time.sleep(3)  # Wait for search results (increased from 2s)

        # --- 1. Check for Total Record(s) row to determine existence ---
        print("📑 Checking if claim results table appears...")
        # Use a short, explicit timeout for the table check
        table_wait = WebDriverWait(driver, 10)  # Increased from 4s — results can load slowly
        try:
            total_record_td = table_wait.until(EC.presence_of_element_located(
                (By.XPATH, "//table[contains(@id,'grdClaimDraftSp')]//td[contains(text(),'Total Record(s)')]")
            ))
            print(f"✅ Table exists! Text: {total_record_td.text}")
            result["claim_exists"] = True
        except Exception:
            print("❌ No claim found for this chasis number (no 'Total Record(s)' row).")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = f"static/converted_images/screenshotproof_{chasis_no}_{timestamp}.png"    
            driver.save_screenshot(screenshot_path)
            result["popup_screenshot"] = screenshot_path
            return result  # No claim: exit early!

        # --- 2. Click the Details button (input image in row) ---
        print("🔗 Clicking Details button...")
        _retry_click(driver, "//table[contains(@id,'grdClaimDraftSp')]//tr[not(@class='gridHeader')][1]//input[@type='image']", by=By.XPATH)
        print("✅ Clicked Details button.")


        # --- 3. Wait for the Popup Table to appear ---
        print("💬 Waiting for popup table to appear...")
        time.sleep(2.5)  # Let the popup loading spinner complete before querying DOM
        popup_table = wait.until(EC.visibility_of_element_located(
            (By.XPATH, "//table[contains(@id,'grdPopupSP')]")
        ))
        popup_html = popup_table.get_attribute("outerHTML")
        result["popup_html"] = popup_html
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = f"static/converted_images/screenshotproof_{chasis_no}_{timestamp}.png"    
        driver.save_screenshot(screenshot_path)
        result["popup_screenshot"] = screenshot_path

        # --- 4. Parse the "Excess Paid" column value ---
        print("🔎 Extracting Excess Paid value...")
        rows = popup_table.find_elements(By.TAG_NAME, "tr")
        if len(rows) > 1:
            headers = [th.text.strip().lower() for th in rows[0].find_elements(By.TAG_NAME, "th")]
            print("Headers found in popup:", headers)
            excess_idx = next((i for i, h in enumerate(headers) if "excess" in h), None)
            if excess_idx is not None:
                cells = rows[1].find_elements(By.TAG_NAME, "td")
                if len(cells) > excess_idx:
                    result["excess_paid"] = cells[excess_idx].text.strip()
                    print("💰 Extracted Excess Paid:", result["excess_paid"])
                else:
                    print("⚠️ Excess Paid cell not found.")
            else:
                print("⚠️ 'Excess Paid' column not found.")
        else:
            print("⚠️ Popup table has no data rows.")

    except Exception as e:
        print("🔥 ERROR during insurance claim lookup:", e)
        result["error"] = "Insurance lookup failed - please try again"
    finally:
        print("🛑 Quitting Chrome browser.")
        try:
            driver.quit()
        except Exception:
            pass
        # Help GC so uc.Chrome.__del__ doesn't run at shutdown
        try:
            del driver
        except Exception:
            pass
    print("🔚 Lookup complete. Result:", result)
    return result
