import os
import time
import re
import json
import requests
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import ElementClickInterceptedException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

# ─── CONFIG ──────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
DOWNLOAD_PATH = os.path.join("static", "downloaded_photos")
CAPTCHA_DOMAINS = [
    "copart.com", "vininspect.com", "vincheck.info", "autocheck.com",
    "autodna.com", "auto.ru", "avtocod.ru", "carvertical.com",
    "autofastvin.com", "vindecoder.eu", "bid.cars", "bidfax.info","stat.vin","plc.ua","plc.auction"
]
# ────────────────────────────────────────────────────────────────

def enable_verbatim_via_url(driver):
    """Ensure Google search results are in verbatim mode via tbs=li:1 parameter."""
    try:
        url = driver.current_url
    except Exception:
        return False

    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=True)

        tbs_vals = [v for v in query.get("tbs", []) if v]
        if tbs_vals:
            joined = tbs_vals[0]
            if "li:1" not in joined.split(","):
                joined = f"{joined},li:1"
            query["tbs"] = [joined]
        else:
            query["tbs"] = ["li:1"]

        new_query = urlencode(query, doseq=True)
        new_url = urlunparse(parsed._replace(query=new_query))
        if new_url != url:
            driver.get(new_url)
            return True
    except Exception:
        return False

    return False

def extract_search_results(driver, timeout=8, max_sets=1):
    """Return unique organic result URLs from Google SERP (robust to AV/extension DOM)."""
    urls = []
    seen = set()

    # Wait for any results container to exist
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#search, #rso"))
        )
    except Exception:
        pass

    # Light nudge to ensure first batch is rendered
    try:
        driver.execute_script("window.scrollBy(0, 200);")
        time.sleep(0.1)
        driver.execute_script("window.scrollBy(0, -150);")
    except Exception:
        pass

    # === Candidate locators (fast → broad) ===
    # 1) Canonical organic: container '.yuRUbf > a'
    fast_sets = [
        (By.CSS_SELECTOR,   "#search .yuRUbf > a[href], #rso .yuRUbf > a[href]"),
        # 2) Common fallback: anchors Google wraps with jsname
        (By.CSS_SELECTOR,   "a[jsname='UWckNb'][href]"),
        # 3) Generic: any <a> that has an <h3> (AV/extension-safe)
        #    use XPath because CSS :has() isn't universally supported in Selenium
        (By.XPATH,          "//a[.//h3][@href]"),
        # 4) Safety net: known older class used on anchors
        (By.CSS_SELECTOR,   "a.zReHs[href]"),
    ]

    # Exclusion helpers
    def is_bad(href: str) -> bool:
        if not href or not href.startswith(("http://", "https://")):
            return True
        bad_bits = (
            "google.", "webcache.googleusercontent.", "/policies", "/preferences",
            "support.google.com", "accounts.google.", "/settings/", "/advanced_search",
        )
        if any(b in href for b in bad_bits):
            return True
        # Skip ads/shopping/news carousels etc by container lineage where possible
        return False

    # Quick ad/container guards: skip items inside known ad/shopping blocks
    def in_bad_container(el) -> bool:
        try:
            anc = el.find_element(By.XPATH, "ancestor-or-self::*[@id='tads' or @id='tvcap' or @id='bottomads']")
            if anc:
                return True
        except Exception:
            pass
        try:
            anc = el.find_element(By.XPATH, "ancestor-or-self::*[contains(@class,'commercial-unit-mobile-top')]")
            if anc:
                return True
        except Exception:
            pass
        return False

    # Collect in order, stop early once we have some
    sets_scanned = 0
    for by, sel in fast_sets:
        try:
            anchors = driver.find_elements(by, sel)
        except Exception:
            anchors = []

        # If we matched h3 nodes (the XPath returns anchors directly, so ok)
        # For CSS cases that may return <h3>, normalize to <a>
        normed = []
        for el in anchors:
            try:
                tag = el.tag_name.lower()
            except Exception:
                continue
            if tag == "h3":
                # climb to nearest anchor
                try:
                    a = el.find_element(By.XPATH, "./ancestor::a[1]")
                    normed.append(a)
                except Exception:
                    continue
            elif tag == "a":
                normed.append(el)

        # De-dup anchors by href
        for a in normed:
            try:
                if in_bad_container(a):
                    continue
                href = a.get_attribute("href")
                if is_bad(href):
                    continue
                if href not in seen:
                    seen.add(href)
                    urls.append(href)
            except Exception:
                continue

        sets_scanned += 1
        if urls and sets_scanned >= max_sets:
            break

    # If still empty, try the "anchor from h3" sweep explicitly (covers AV-injected <div>s in <h3>)
    if not urls:
        try:
            h3s = driver.find_elements(By.CSS_SELECTOR, "#search h3, #rso h3")
            for h in h3s:
                try:
                    a = h.find_element(By.XPATH, "./ancestor::a[1]")
                    if in_bad_container(a):
                        continue
                    href = a.get_attribute("href")
                    if not is_bad(href) and href not in seen:
                        seen.add(href); urls.append(href)
                except Exception:
                    continue
        except Exception:
            pass

    print(f"📊 Extracted {len(urls)} result links")
    return urls

    
def is_known_captcha_site(url):
    return any(dom in url for dom in CAPTCHA_DOMAINS)

def filter_high_res(urls, max_images=4):
    """
    Filter high resolution images and limit to max_images (default 4)
    Prioritizes images without low-res indicators
    """
    out = []
    bad = ["thumb","/small/","/thumbnail/","_tn.","_small.", "-140", "-150", "-200", "-320", "-400", "-240", "-360", "-480"]
    
    # Additional indicators of non-car images to avoid
    skip_indicators = ["logo", "icon", "banner", "flag", "badge", "sprite", "avatar", "placeholder", "default"]
    
    for u in urls:
        if not u: continue
        low = u.lower()
        
        # Skip if contains bad resolution indicators
        if any(b in low for b in bad): continue
        
        # Skip if contains non-car image indicators
        if any(skip in low for skip in skip_indicators): continue
        
        # Check width parameter if exists
        m = re.search(r"width=(\d+)", u)
        if m and int(m.group(1)) < 600: continue
        
        out.append(u)
    
    # Remove duplicates while preserving order
    unique_urls = list(dict.fromkeys(out))
    
    # Return only the first max_images
    return unique_urls[:max_images]

def download_images(urls, folder, referer, max_downloads=4):
    """
    Download images with a limit on number of downloads (default 4)
    """
    os.makedirs(folder, exist_ok=True)
    saved = []
    hdrs = {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept": "image/jpeg,image/png,*/*",
    }
    
    # Limit URLs to max_downloads
    urls_to_download = urls[:max_downloads]
    
    for i, u in enumerate(urls_to_download):
        try:
            ext = u.split(".")[-1].split("?")[0]
            if ext not in ("jpg","jpeg","png","gif"): ext = "jpg"
            path = os.path.join(folder, f"photo_{i}.{ext}")
            r = requests.get(u, headers=hdrs, timeout=20); r.raise_for_status()
            with open(path, "wb") as f: f.write(r.content)
            saved.append(path)
            print(f"✅ Downloaded image {i+1}/{len(urls_to_download)}")
        except Exception as e:
            print(f"❌ Failed to download image {i+1}: {str(e)[:50]}...")
            continue
    
    print(f"📊 Successfully downloaded {len(saved)} out of {len(urls_to_download)} images")
    return saved

CHROME_VERSION = 146  # Pin to match installed Chrome version

def get_stealth_driver_opensooq(driver_path=None, headless=True, user_agent=None):
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-images")
    if headless:
        options.add_argument("--headless=new")
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")
    driver = uc.Chrome(
        driver_executable_path=driver_path,
        options=options,
        use_subprocess=True,
        version_main=CHROME_VERSION,
    )
    if user_agent:
        driver.execute_cdp_cmd('Network.setUserAgentOverride', {"userAgent": user_agent})
    return driver


def get_simple_driver(driver_path=None, headless=False, user_agent=None):
    """
    Simple driver for DuckDuckGo - no fancy options that might break
    """
    import undetected_chromedriver as uc
    import os

    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    if headless:
        options.add_argument("--headless=new")

    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")
    else:
        options.add_argument(f"--user-agent={USER_AGENT}")

    driver = uc.Chrome(
        driver_executable_path=driver_path,
        options=options,
        use_subprocess=True,
        version_main=CHROME_VERSION,
    )

    return driver


def human_mimic_nudge(driver):
    """Perform random human-like mouse movements and small scrolls."""
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        import random
        
        # 1. Random small scrolls
        for _ in range(random.randint(2, 4)):
            y = random.randint(100, 400)
            driver.execute_script(f"window.scrollBy(0, {y});")
            time.sleep(random.uniform(0.2, 0.5))
        
        # 2. Random mouse movements
        width = driver.execute_script("return window.innerWidth")
        height = driver.execute_script("return window.innerHeight")
        actions = ActionChains(driver)
        # Move to a few random points
        for _ in range(random.randint(3, 5)):
            x = random.randint(0, int(width * 0.8))
            y = random.randint(0, int(height * 0.8))
            # Move relative to the body/current position
            actions.move_by_offset(random.randint(-50, 50), random.randint(-50, 50)).perform()
            time.sleep(random.uniform(0.1, 0.3))
        
        # 3. Scroll back up slightly
        driver.execute_script("window.scrollTo(0, 0);")
    except Exception:
        pass

def get_stealth_driver(driver_path=None, headless=False, user_agent=None):
    import undetected_chromedriver as uc
    import os
    import random

    options = uc.ChromeOptions()
    
    # Randomize viewport to look like a laptop (not a server)
    viewports = [
        (1366, 768), (1536, 864), (1440, 900), (1280, 720)
    ]
    w, h = random.choice(viewports)
    options.add_argument(f"--window-size={w},{h}")
    
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--log-level=3")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-notifications")
    options.add_argument("--lang=en-US")

    if headless:
        options.add_argument("--headless=new")
        options.add_argument(f"--window-size={w},{h}")
        options.add_argument("--hide-scrollbars")

    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")
    else:
        options.add_argument(f"--user-agent={USER_AGENT}")

    driver = uc.Chrome(
        driver_executable_path=driver_path,
        options=options,
        use_subprocess=True,
        version_main=CHROME_VERSION,
    )

    # ─── CDP Stealth Overrides ───
    try:
        # 1. Hide Navigator.webdriver
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
        })
        # 2. Set consistent hardware stats
        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width": w,
            "height": h,
            "deviceScaleFactor": 1,
            "mobile": False
        })
    except Exception:
        pass

    return driver



def is_no_match_page(driver):
    try:
        no_result_element = driver.find_element(
            By.XPATH,
            "//div[contains(@class, 'flex-col') and contains(., '0 results')]"
        )
        return True if no_result_element else False
    except:
        return False

def slow_typing(element, text, delay=0.09):
    for char in text:
        element.send_keys(char)
        time.sleep(delay)

def try_close_overlays(driver):
    try:
        close_btns = driver.find_elements(By.XPATH, "//button[contains(@class,'close') or contains(@class,'Close') or contains(@aria-label,'close')]")
        for btn in close_btns:
            try:
                btn.click()
                time.sleep(1)
            except:
                continue
    except:
        pass

def safe_click(driver, selector, by=By.ID, step="", retries=3):
    try:
        el = WebDriverWait(driver, 12).until(
            EC.element_to_be_clickable((by, selector))
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
        try:
            el.click()
        except ElementClickInterceptedException:
            driver.execute_script("arguments[0].click();", el)
        return el
    except Exception as e:
        print(f"⚠️ Could not click [{selector}] ({by}): {e}")
        
        raise

def scroll_and_click(driver, elem):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        time.sleep(1)
        elem.click()
    except ElementClickInterceptedException:
        try:
            driver.execute_script("arguments[0].click();", elem)
            time.sleep(0.6)
        except Exception:
            raise

def close_yalla_overlays(driver):
    try:
        for sel in [
            "//button[contains(., 'Accept') or contains(., 'OK')]",
            "//div[contains(@class,'cookies') or contains(@id,'cookie')]//button"
        ]:
            btns = driver.find_elements(By.XPATH, sel)
            for b in btns:
                try:
                    if b.is_displayed() and b.is_enabled():
                        b.click()
                        time.sleep(0.6)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        for sel in [
            "//div[contains(@class,'modal') and contains(@class,'show')]//button[contains(@class,'close')]",
            "//div[contains(@class,'modal') and contains(@class,'show')]",
        ]:
            elems = driver.find_elements(By.XPATH, sel)
            for e in elems:
                try:
                    if "close" in e.get_attribute("class").lower():
                        e.click()
                        time.sleep(0.4)
                    else:
                        driver.execute_script("arguments[0].style.display='none';", e)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        driver.execute_script("""
        for (const el of document.querySelectorAll('[class*="nav"],[id*="nav"]')) {
            if (el.offsetHeight > 50) el.style.display = 'none';
        }
        """)
    except Exception:
        pass

# ─── GALLERY EXTRACTORS ──────────────────────────────────────────────

def extract_bidfax_gallery(driver, wait):
    try:
        for btn in driver.find_elements(By.XPATH, "//button[contains(text(),'Accept') or contains(text(),'Allow')]"):
            btn.click(); time.sleep(1)
    except: pass
    imgs = driver.find_elements(
        By.XPATH,
        "//div[contains(@class,'gallery') or contains(@class,'carousel') or contains(@class,'slider')]//img"
    )
    out = []
    for img in imgs:
        for attr in ("src","data-src"):
            v = img.get_attribute(attr)
            if v and "copart.com" in v:
                out.append(v)
    return list(dict.fromkeys(out))

def extract_statvin_gallery(driver, wait):
    try:
        for btn in driver.find_elements(By.XPATH, "//button[contains(text(),'Accept') or contains(text(),'Allow')]"):
            btn.click(); time.sleep(1)
    except: pass
    imgs = driver.find_elements(
        By.XPATH,
        "//div[contains(@class,'f-carousel__viewport') or contains(@class,'gallery') or contains(@class,'slider')]//img"
    )
    out = []
    for img in imgs:
        for attr in ("src","data-src"):
            v = img.get_attribute(attr)
            if v and "stat.vin" in v:
                out.append(v)
    return list(dict.fromkeys(out))

def extract_plc_gallery(driver, wait):
    try:
        btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(),'Accept All')]")))
        btn.click(); time.sleep(1)
    except: pass
    imgs = driver.find_elements(By.XPATH, "//div[contains(@class,'swiper-slide')]//img")
    out = []
    for img in imgs:
        for attr in ("src","data-src"):
            v = img.get_attribute(attr)
            if v and "plc.auction" in v:
                out.append(v)
    return list(dict.fromkeys(out))

def extract_autohelperbot_gallery(driver, wait, chasis_no):
    try:
        for btn in driver.find_elements(By.XPATH, "//button[contains(text(),'Accept') or contains(text(),'Allow')]"):
            btn.click(); time.sleep(1)
    except: pass

    # Scroll to force lazy content
    try:
        total_h = driver.execute_script("return document.body.scrollHeight") or 1000
        for y in range(0, int(total_h), 600):
            driver.execute_script(f"window.scrollTo(0,{y});"); time.sleep(0.2)
        driver.execute_script("window.scrollTo(0,0);")
    except Exception:
        pass

    def _normalize_url(u: str) -> str:
        try:
            p = urlparse(u.strip())
            return (p.scheme or 'https') + '://' + (p.netloc or '').lower() + (p.path or '')
        except Exception:
            return u.strip()

    seen = set(); unique = []
    def _add(u: str):
        if not u: return
        key = _normalize_url(u)
        if key not in seen:
            seen.add(key); unique.append(u)

    vin_lower = (chasis_no or '').lower()

    # Collect anchors that point to autohelperbot or contain the VIN
    anchors = driver.find_elements(By.XPATH, "//a[contains(@href,'autohelperbot.com') or contains(translate(@href,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), '%s') ]" % vin_lower)
    for a in anchors:
        href = a.get_attribute("href")
        if href: _add(href)

    # Collect images anywhere on page
    imgs = driver.find_elements(By.XPATH, "//img")
    for img in imgs:
        for attr in ("src", "data-src", "srcset"):
            v = img.get_attribute(attr)
            if not v: continue
            if " " in v: v = v.split(" ")[0]
            vl = v.lower()
            if "autohelperbot.com" in vl or (vin_lower and vin_lower in vl):
                _add(v)

    # Normalize/whitelist to CAR photos only
    filtered = []
    bad_tokens = [
        'flag', 'flags', 'logo', 'icon', 'sprite', 'avatar', 'badge',
        'app-store', 'googleplay', 'itunes', 'country', 'ads', 'banner'
    ]
    for u in unique:
        if not u:
            continue
        try:
            abs_u = urljoin(driver.current_url, u)
        except Exception:
            abs_u = u
        low = abs_u.lower()
        # Only image file extensions
        if not (low.endswith('.jpg') or low.endswith('.jpeg') or low.endswith('.png')):
            continue
        # Exclude obvious non-car assets
        if any(tok in low for tok in bad_tokens):
            continue
        # Prefer VIN-containing or gallery/photo paths
        if vin_lower and vin_lower in low:
            filtered.append(abs_u)
            continue
        if 'autohelperbot.com' in low and ('/photo' in low or '/images' in low or '/gallery' in low):
            filtered.append(abs_u)

    return filtered


def extract_carcheckby_gallery(driver, wait, chasis_no):
    url = driver.current_url
    time.sleep(2)
    if "carcheck.by" in urlparse(url).netloc:
        tgt = f"https://carcheck.by/en/auto/{chasis_no}"
        if url != tgt:
            time.sleep(3)
            driver.get(tgt); time.sleep(2)
    imgs = driver.find_elements(By.XPATH, "//div[@id='owl_big']//div[contains(@class,'owl-stage')]//img")
    out = []
    for img in imgs:
        for attr in ("src", "data-src"):
            v = img.get_attribute(attr)
            if v:
                out.append(v)
    return list(dict.fromkeys(out))

def generate_results_html(image_paths, folder):
    os.makedirs(folder, exist_ok=True)
    html = ["<html><body><h1>Results</h1><div>"]
    if image_paths:
        for p in image_paths:
            rel = os.path.relpath(p, folder)
            html.append(f'<img src="{rel}" style="max-width:200px;margin:5px">')
    else:
        html.append("<p>No images downloaded.</p>")
    html.append("</div></body></html>")
    with open(os.path.join(folder, "results.html"), "w", encoding="utf-8") as f:
        f.write("".join(html))


def remove_google_vignette_overlay(driver):
    """Attempt to remove Google vignette interstitial overlays via JS only.
    Returns True if anything was removed, False otherwise. No navigation performed.
    """
    try:
        return bool(driver.execute_script(
            """
            try {
              const sels = [
                'iframe[id*="google_ads"]',
                'iframe[name*="google"]',
                'iframe[src*="google_vignette"]',
                'div[id*="google_vignette"]',
                'div[class*="google_vignette"]'
              ];
              let removed = false;
              for (const sel of sels) {
                document.querySelectorAll(sel).forEach(el => { el.remove(); removed = true; });
              }
              return removed;
            } catch(e){ return false; }
            """
        ))
    except Exception:
        return False
