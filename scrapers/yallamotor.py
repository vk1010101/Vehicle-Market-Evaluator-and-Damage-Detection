import re
import time
import json
import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException, StaleElementReferenceException

from .scraper_utils import get_stealth_driver, human_mimic_nudge, USER_AGENT

# ─────────────────────────────────────────────────────────
#  URL SLUG HELPERS
# ─────────────────────────────────────────────────────────
def _slug(text):
    if not text: return ""
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")

BODY_TYPE_MAP = {
    "suv": "suv-crossover", "crossover": "suv-crossover", "sedan": "sedan",
    "hatchback": "hatchback", "pickup": "pickup-truck", "coupe": "coupe",
    "convertible": "convertible", "van": "van-minivan", "minivan": "van-minivan",
}

def _build_url(make, model_value, year_min, year_max, body_type, price_min, price_max):
    base = "https://oman.yallamotor.com/used-cars"
    parts = []
    if make: parts.append(_slug(make))
    if model_value: parts.append(_slug(model_value))
    if year_min or year_max:
        parts.append(f"yr_{year_min or '1990'}_{year_max or '2099'}")
    body_sl = BODY_TYPE_MAP.get(str(body_type).lower(), _slug(body_type)) if body_type else ""
    if body_sl: parts.append(f"bs_{body_sl}")
    url = base + ("/" + "/".join(parts) if parts else "")
    qs = []
    if price_min: qs.append(f"price_min={price_min}")
    if price_max: qs.append(f"price_max={price_max}")
    return url + ("?" + "&".join(qs) if qs else "")

# ─────────────────────────────────────────────────────────
#  FASTER: CLOUDSCRAPER (BEAUTIFUL SOUP) 
# ─────────────────────────────────────────────────────────
def scrape_yallamotor_soup(url, page_num=1):
    """Primary high-speed scraper."""
    print("☁️ YallaMotor: Fetching with Cloudscraper (Primary)...")
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
    all_cars = []
    
    try:
        resp = scraper.get(url, timeout=12)
        if resp.status_code != 200: 
            print(f"⚠️ YallaMotor Soup: Status {resp.status_code}")
            return []
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        next_data = soup.find('script', id='__NEXT_DATA__')
        if next_data:
            data = json.loads(next_data.string)
            search_state = data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('search', {})
            listings = search_state.get('listings', [])
            
            for item in listings:
                all_cars.append({
                    "Car Name": item.get('title') or f"{item.get('make')} {item.get('model')} {item.get('year')}",
                    "Price": f"OMR {item.get('price')}",
                    "Body Type": item.get('body_type'),
                    "Kilometers": f"{item.get('mileage')} KM",
                    "Year": item.get('year'),
                    "Location": item.get('location'),
                    "Source": "YallaMotor",
                    "link": f"https://oman.yallamotor.com{item.get('url')}"
                })
        
        if not all_cars:
            for card in soup.select("section[aria-label*='listing'], div.singleSearchCard"):
                title_el = card.select_one("h2 a")
                if not title_el: continue
                price_el = card.select_one(".text-2xl, span.font24, .price")
                all_cars.append({
                    "Car Name": title_el.text.strip(),
                    "Price": price_el.text.strip() if price_el else "N/A",
                    "Source": "YallaMotor",
                    "link": urljoin("https://oman.yallamotor.com", title_el['href'])
                })
                
        return all_cars
    except Exception:
        return []

# ─────────────────────────────────────────────────────────
#  BACKUP: SELENIUM
# ─────────────────────────────────────────────────────────
def scrape_yallamotor_browser(url, driver_path=None, headless=True):
    print("🚜 YallaMotor: Using Heavy Browser Backup...")
    driver = None
    try:
        driver = get_stealth_driver(driver_path, headless=headless)
        driver.get(url)
        time.sleep(3)
        
        if "backend fetch failed" in driver.page_source.lower():
            human_mimic_nudge(driver)
            time.sleep(1)
            driver.refresh()
            time.sleep(3)

        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.XPATH, "//h2/a[@href]")))
        
        cars = []
        cards = driver.find_elements(By.XPATH, "//section[contains(@aria-label,'listing')] | //div[contains(@class,'singleSearchCard')]")
        for card in cards[:10]:
            try:
                title_el = card.find_element(By.TAG_NAME, "h2")
                link_el = title_el.find_element(By.TAG_NAME, "a")
                cars.append({
                    "Car Name": title_el.text.strip(),
                    "Price": card.find_element(By.XPATH, ".//*[contains(@class,'price') or contains(@class,'font24')]").text.strip(),
                    "Source": "YallaMotor",
                    "link": link_el.get_attribute("href")
                })
            except: continue
        return cars
    except:
        return []
    finally:
        if driver: driver.quit()

# ─────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────
def scrape_yallamotor(make=None, model_value=None, body_type=None, price_min=None, price_max=None, year_min=None, year_max=None, page_num=1, driver_path=None, headless=True):
    url = _build_url(make, model_value, year_min, year_max, body_type, price_min, price_max)
    
    # 1. PRIMARY: FAST SOUP
    all_cars = scrape_yallamotor_soup(url, page_num)
    
    # 2. BACKUP: HEAVY BROWSER (if soup found 0 results)
    if not all_cars:
        print("🔄 YallaMotor: Soup empty, activating Browser backup...")
        all_cars = scrape_yallamotor_browser(url, driver_path, headless)
        
    # 3. FINAL FALLBACK: PROFESSIONAL ERROR MESSAGE
    if not all_cars:
        print("❌ YallaMotor: Both methods failed. Returning professional error entry.")
        return [{
            "Car Name": "⚠️ YallaMotor Search Unavailable",
            "Price": "N/A",
            "Body Type": "Manual Review Required",
            "Source": "YallaMotor",
            "link": url,
            "Error": "YallaMotors is currently unavailable to our servers. Please verify manually or try again later."
        }]
                
    return all_cars