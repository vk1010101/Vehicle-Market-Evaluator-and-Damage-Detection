# ─────────────────────────────────────────────────────────────────────────────
# opensooq.py — cloudscraper + BeautifulSoup approach (replaces Selenium)
# ─────────────────────────────────────────────────────────────────────────────
# Date:   2026-04-02
# Reason: The original Selenium-based scraper (backed up as opensooq_selenium_backup.py)
#         was unreliable due to:
#         1. OpenSooq's aggressive IP banning (5-min bans after ~3-4 rapid requests)
#         2. Page reload cycles that cleared filters mid-interaction
#         3. ~60s per request (too slow for 500-1000 daily runs)
#
#         This version uses cloudscraper (bypasses Cloudflare) + BeautifulSoup (parses SSR HTML).
#         Filters are applied via URL path/params — no browser, no form filling, ~2-3s per request.
#
# Original Selenium version: scrapers/opensooq_selenium_backup.py
# ─────────────────────────────────────────────────────────────────────────────

import re
import time
import random
from urllib.parse import urljoin, quote

import cloudscraper
from bs4 import BeautifulSoup


def _build_url(make=None, model_value=None, body_type=None,
               year_min=None, year_max=None, price_min=None, price_max=None,
               page=1):
    """
    Build OpenSooq search URL from filters.
    URL pattern: https://om.opensooq.com/en/cars/cars-for-sale/{make}/{model}
    """
    base = "https://om.opensooq.com/en/cars/cars-for-sale"

    # Make + Model go into the URL path
    if make:
        base += f"/{quote(make.lower().strip())}"
    if model_value:
        base += f"/{quote(model_value.lower().strip().replace(' ', '-'))}"

    # Everything else is query params
    params = []
    if year_min:
        params.append(f"year_min={year_min}")
    if year_max:
        params.append(f"year_max={year_max}")
    if price_min:
        params.append(f"price_min={price_min}")
    if price_max:
        params.append(f"price_max={price_max}")
    if body_type:
        params.append(f"body_type={quote(body_type.strip())}")
    if page and page > 1:
        params.append(f"page={page}")

    if params:
        base += "?" + "&".join(params)
    return base


def _parse_listings(html, base_url="https://om.opensooq.com"):
    """
    Parse car listings from OpenSooq HTML using BeautifulSoup.
    Returns list of dicts matching the same schema as the old Selenium scraper.
    """
    soup = BeautifulSoup(html, "html.parser")
    cars = []

    # Card selector: <a> tags with data-post-index that are actual listings (not ads)
    cards = soup.select("a[data-post-index]")
    if not cards:
        # Fallback: any anchor with class postListItemData
        cards = soup.select("a.postListItemData")

    for card in cards:
        # Skip promoted/recommended items
        if card.get("data-is-recommended", "").lower() == "true":
            continue

        car_name = ""
        meta_text = ""
        price = ""
        year = ""
        kms = ""
        body = ""
        link = ""

        # Car Name — h2 with breakWord class
        h2 = card.select_one("h2.breakWord, h2")
        if h2:
            car_name = h2.get_text(strip=True)

        # Meta text — <p> tag with comma-separated info (year, km, body type)
        p_tag = card.select_one("p")
        if p_tag:
            meta_text = p_tag.get_text(strip=True)
            parts = [pt.strip() for pt in meta_text.split(",") if pt.strip()]

            # Year — any 4-digit number like 2017
            for p in parts:
                ym = re.search(r"\b(19|20)\d{2}\b", p)
                if ym:
                    year = ym.group(0)
                    break

            # Kilometers — take the longest km match
            km_matches = re.findall(
                r'[+]?[\d]{1,3}(?:,\d{3})*(?:\s*-\s*[+]?[\d]{1,3}(?:,\d{3})*)?\\s*km',
                meta_text, flags=re.IGNORECASE
            )
            if not km_matches:
                # Simpler fallback
                km_matches = re.findall(r'[\d,]+\s*km', meta_text, flags=re.IGNORECASE)
            kms = max(km_matches, key=len).strip() if km_matches else ""

            # Body type
            for p in parts:
                m = re.search(
                    r"(sedan|suv|hatchback|pickup|coupe|convertible|truck|bus\s*-\s*van)",
                    p, re.IGNORECASE
                )
                if m:
                    body = m.group(0).strip().upper()
                    break

        # Price — div with priceColor class
        price_el = card.select_one("div.priceColor, [class*='priceColor']")
        if price_el:
            price = price_el.get_text(strip=True)

        # Link
        href = card.get("href", "")
        if href:
            link = urljoin(base_url, href)

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
            "link": link,
        })

    return cars


def scrape_opensooq(
    make=None,
    model_value=None,
    body_type=None,
    price_min=None,
    price_max=None,
    year_min=None,
    year_max=None,
    page_num=1,
    driver_path='chromedriver.exe',  # kept for API compatibility, unused
    headless=True                    # kept for API compatibility, unused
):
    """
    Scrape OpenSooq using cloudscraper + BeautifulSoup (no browser needed).
    Same function signature as the old Selenium version for drop-in replacement.
    """
    print("🟢 OpenSooq scraper started (cloudscraper mode)")

    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )

    all_cars = []

    for page_idx in range(1, max(1, page_num) + 1):
        url = _build_url(
            make=make, model_value=model_value, body_type=body_type,
            year_min=year_min, year_max=year_max,
            price_min=price_min, price_max=price_max,
            page=page_idx
        )
        print(f"🌍 Fetching page {page_idx}: {url}")

        try:
            resp = scraper.get(url, timeout=30)
        except Exception as e:
            print(f"❌ Request failed: {e}")
            break

        if resp.status_code == 403:
            print("⚠️ 403 Forbidden — IP may be temporarily banned. Waiting 30s and retrying...")
            time.sleep(30)
            try:
                resp = scraper.get(url, timeout=30)
            except Exception as e:
                print(f"❌ Retry failed: {e}")
                break

        if resp.status_code != 200:
            print(f"❌ HTTP {resp.status_code} — stopping")
            break

        page_cars = _parse_listings(resp.text)
        print(f"📄 Page {page_idx}: found {len(page_cars)} cars")

        if not page_cars:
            print("ℹ️ No listings found — stopping pagination")
            break

        all_cars.extend(page_cars)

        # Polite delay between pages to avoid triggering IP ban
        if page_idx < page_num:
            delay = random.uniform(1.5, 3.5)
            print(f"⏳ Waiting {delay:.1f}s before next page...")
            time.sleep(delay)

    print(f"✅ OpenSooq collected {len(all_cars)} cars across {min(page_idx, page_num)} page(s)")
    return all_cars
