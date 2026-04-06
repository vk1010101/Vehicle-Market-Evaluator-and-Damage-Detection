"""
DuckDuckGo-based image search implementation
Replacement for Google search to avoid CAPTCHA issues
"""
import os
import time
import random
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException
from PIL import Image
from urllib.parse import urlparse

from .scraper_utils import (
    USER_AGENT,
    DOWNLOAD_PATH,
    is_known_captcha_site,
    filter_high_res,
    download_images,
    get_simple_driver,  # Using simple driver for DuckDuckGo
    extract_bidfax_gallery,
    extract_statvin_gallery,
    extract_plc_gallery,
    extract_autohelperbot_gallery,
    extract_carcheckby_gallery,
)


def duckduckgo_image_search(chasis_no, headless=True, max_sites=10):
    """
    Main DuckDuckGo search function with all features from Google implementation
    """
    from urllib.parse import urlparse
    
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)
    downloaded, found, sources = [], [], []
    first_page = None
    driver = None
    
    # Human-like delay function
    def human_delay(min_sec=0.5, max_sec=2.0):
        time.sleep(random.uniform(min_sec, max_sec))
    
    try:
        # Initialize simple driver for DuckDuckGo
        driver = get_simple_driver(headless=headless, user_agent=USER_AGENT)
        wait = WebDriverWait(driver, 15)
        
        print("🦆 DUCKDUCKGO: Starting chassis image search...")
        
        # Navigate to DuckDuckGo with human-like behavior
        human_delay(0.5, 1.5)
        driver.get("https://duckduckgo.com/")
        human_delay(1.0, 2.5)
        
        # Find and interact with search box
        try:
            search_box = wait.until(EC.presence_of_element_located((By.NAME, "q")))
            search_box.click()
            human_delay(0.2, 0.5)
            
            # Clear any existing text
            search_box.send_keys(Keys.CONTROL, "a")
            human_delay(0.1, 0.2)
            search_box.send_keys(Keys.DELETE)
            human_delay(0.1, 0.3)
            
            # Type search query with human-like delays
            search_query = f'"{chasis_no}"'
            for char in search_query:
                search_box.send_keys(char)
                time.sleep(random.uniform(0.05, 0.15))
            
            human_delay(0.3, 0.7)
            search_box.send_keys(Keys.ENTER)
            
        except TimeoutException:
            print("❌ DUCKDUCKGO: Could not find search box")
            return {
                "found_images": [],
                "gallery_source": [],
                "first_gallery_page": None,
                "downloaded_images": [],
                "error": "Could not find DuckDuckGo search box"
            }
        
        # Wait for results to load
        human_delay(2.0, 3.0)
        print("🦆 DUCKDUCKGO: Search submitted, extracting results...")
        
        # Extract search result links
        links = extract_duckduckgo_results(driver, wait)
        print(f"🦆 DUCKDUCKGO: Found {len(links)} search result links")
        
        # If no links found, try different extraction methods
        if not links:
            print("🦆 DUCKDUCKGO: No links found, trying alternative extraction...")
            # Scroll to trigger any lazy loading
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
            human_delay(1.0, 2.0)
            links = extract_duckduckgo_results_alternative(driver)
            print(f"🦆 DUCKDUCKGO: Alternative extraction found {len(links)} links")
        
        # Visit each result and look for images
        for url in links[:max_sites]:
            if is_known_captcha_site(url):
                print(f"🦆 DUCKDUCKGO: Skipping known captcha site: {urlparse(url).netloc}")
                continue
                
            try:
                print(f"🦆 DUCKDUCKGO: Visiting {urlparse(url).netloc}...")
                driver.set_page_load_timeout(20)
                driver.get(url)
                human_delay(2.0, 3.5)
            except Exception as e:
                print(f"🦆 DUCKDUCKGO: Error loading {url}: {str(e)}")
                continue
            
            curr = driver.current_url
            
            # Check if chassis number is present on page
            if chasis_no.lower() not in driver.page_source.lower():
                print(f"🦆 DUCKDUCKGO: Chassis number not found on {urlparse(curr).netloc}")
                continue
            
            first_page = curr
            
            # Use existing gallery extractors
            print(f"🦆 DUCKDUCKGO: Extracting images from {urlparse(curr).netloc}...")
            
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
                print(f"🦆 DUCKDUCKGO: No specific extractor for {urlparse(curr).netloc}")
                continue
            
            # Filter and download high-res images
            highres = filter_high_res(raw)
            if highres:
                print(f"🦆 DUCKDUCKGO: Found {len(highres)} high-res images")
                saved = download_images(highres, DOWNLOAD_PATH, curr)
                if saved:
                    downloaded, found = saved, highres
                    sources.append(urlparse(curr).netloc)
                    print(f"✅ DUCKDUCKGO: Successfully downloaded {len(saved)} images")
                    break
        
        # Fallback: No images found
        if not downloaded:
            print(f"🦆 DUCKDUCKGO: No relevant images found for chassis {chasis_no}")
            return {
                "found_images": [],
                "gallery_source": [],
                "first_gallery_page": None,
                "downloaded_images": [],
                "error": "No reference images found for this specific chassis. Please verify manually or try again later."
            }
        
        return {
            "found_images": found,
            "gallery_source": sources,
            "first_gallery_page": first_page,
            "downloaded_images": downloaded,
            "error": None
        }
        
    except Exception as e:
        print(f"❌ DUCKDUCKGO ERROR: {str(e)}")
        # Try to take error screenshot
        try:
            if driver:
                error_screenshot = os.path.join(DOWNLOAD_PATH, f"error_{chasis_no}.jpg")
                driver.save_screenshot(error_screenshot)
                return {
                    "found_images": [],
                    "gallery_source": ["duckduckgo.com"],
                    "first_gallery_page": driver.current_url if driver else None,
                    "downloaded_images": [error_screenshot],
                    "error": str(e)
                }
        except:
            pass
            
        return {
            "found_images": [],
            "gallery_source": [],
            "first_gallery_page": None,
            "downloaded_images": [],
            "error": str(e)
        }
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def extract_duckduckgo_results(driver, wait, timeout=8):
    """
    Extract search result links from DuckDuckGo SERP
    """
    urls = []
    seen = set()
    
    try:
        # Wait for results to appear
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='result']")))
    except TimeoutException:
        print("🦆 DUCKDUCKGO: Results container not found")
        return urls
    
    # Multiple selectors for different result types
    selectors = [
        "a[data-testid='result-title-a']",  # Main result titles
        "article[data-testid='result'] a",   # All links in results
        ".result__a",                        # Alternative class
        "h2 a",                             # Headers with links
        ".results a[href]"                  # General results links
    ]
    
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for element in elements:
                try:
                    href = element.get_attribute("href")
                    if href and href not in seen and is_valid_result_url(href):
                        seen.add(href)
                        urls.append(href)
                except:
                    continue
        except:
            continue
    
    return urls


def extract_duckduckgo_results_alternative(driver):
    """
    Alternative method to extract results using different approaches
    """
    urls = []
    seen = set()
    
    # Try JavaScript extraction
    try:
        js_urls = driver.execute_script("""
            let urls = [];
            document.querySelectorAll('a').forEach(a => {
                let href = a.href;
                if (href && !href.includes('duckduckgo.com') && !href.includes('duck.co')) {
                    urls.push(href);
                }
            });
            return urls;
        """)
        
        for url in js_urls:
            if url not in seen and is_valid_result_url(url):
                seen.add(url)
                urls.append(url)
    except:
        pass
    
    # Try XPath extraction
    try:
        links = driver.find_elements(By.XPATH, "//a[@href and not(contains(@href, 'duckduckgo.com'))]")
        for link in links:
            try:
                href = link.get_attribute("href")
                if href and href not in seen and is_valid_result_url(href):
                    seen.add(href)
                    urls.append(href)
            except:
                continue
    except:
        pass
    
    return urls


def is_valid_result_url(url):
    """
    Check if URL is a valid search result (not DuckDuckGo internal)
    """
    if not url or not url.startswith(("http://", "https://")):
        return False
    
    # Skip DuckDuckGo internal links
    skip_domains = [
        "duckduckgo.com",
        "duck.co",
        "duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion"
    ]
    
    for domain in skip_domains:
        if domain in url:
            return False
    
    # Skip common non-content URLs
    skip_patterns = [
        "/preferences",
        "/settings",
        "/privacy",
        "/about",
        "/help",
        "javascript:",
        "mailto:",
        "#"
    ]
    
    for pattern in skip_patterns:
        if pattern in url.lower():
            return False
    
    return True
