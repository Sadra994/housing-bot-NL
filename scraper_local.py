import json
import os
import re
import time
import random

import requests
import cloudscraper
from rebo_scraper import ReboScraper
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

# ── Load .env file ───────────────────────────────────────────────────────────
def _load_env() -> None:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())

_load_env()

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Configuration ─────────────────────────────────────────────────────────────
# Edit these to change your search preferences
CITIES   = ["utrecht", "amersfoort", "zeist"]
MAX_RENT = 1300
SEEN_FILE = "seen_listings.json"

# Holland2stay uses numeric city IDs in their URLs
HOLLAND2STAY_CITIES = {
    "utrecht":    "Utrecht%2C27",
    "amersfoort": "Amersfoort%2C6249",
}

# Vesteda uses lat/lon coordinates per city
VESTEDA_CITIES = {
    "utrecht":    {"lat": 52.091927, "lon": 5.122957,  "label": "Utrecht,%20Nederland"},
    "amersfoort": {"lat": 52.156113, "lon": 5.3878264, "label": "Amersfoort,%20Nederland"},
    "zeist":      {"lat": 52.088100, "lon": 5.235230,  "label": "Zeist,%20Nederland"},
}

# Huurwoningen URL formats tried in order until one returns 200
HUURWONINGEN_URL_TEMPLATES = [
    "https://www.huurwoningen.nl/in/{city}/?price=0-{max_rent}",
    "https://www.huurwoningen.nl/in/{city}/huurprijs-0-{max_rent}/",
    "https://www.huurwoningen.nl/in/{city}/",
]


# ── Helpers ───────────────────────────────────────────────────────────────────
def make_scraper() -> cloudscraper.CloudScraper:
    """Create a cloudscraper session that mimics a real Chrome browser."""
    return cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )


def random_delay(min_sec: float = 8, max_sec: float = 15) -> None:
    """Wait a random amount of time to avoid being detected as a bot."""
    delay = random.uniform(min_sec, max_sec)
    print(f"   ⏳ Waiting {delay:.1f}s...")
    time.sleep(delay)


def clean_price(raw: str) -> str:
    """
    Extract a clean price string from messy scraped text.
    '€ 975 per maandTransparantMeer informatie' → '€ 975 per maand'
    '€932,00per maand excl.*'                   → '€932,00per maand'
    Unparseable text                             → 'Prijs op aanvraag'
    """
    if not raw:
        return "Prijs op aanvraag"
    match = re.search(r"(€[\s\d.,]+per maand)", raw)
    if match:
        return match.group(1).strip()
    if "prijs op aanvraag" in raw.lower():
        return "Prijs op aanvraag"
    return "Prijs op aanvraag"


def is_within_budget(price_str: str, max_rent: int) -> bool:
    """
    Return True if the price is at or below max_rent.
    'Prijs op aanvraag' listings are always kept since we cannot filter them.
    """
    # remove Dutch thousand separators and extract the number
    cleaned = price_str.replace(".", "").replace(",", ".")
    match = re.search(r"(\d+)", cleaned)
    if not match:
        return True  # unknown price — keep it
    return float(match.group(1)) <= max_rent


def make_listing(id: str, title: str, price: str, area: str, url: str, source: str) -> dict:
    """Return a standardised listing dictionary."""
    return {"id": id, "title": title, "price": price, "area": area, "url": url, "source": source}


# ── Telegram notification ────────────────────────────────────────────────────
def send_telegram(listing: dict) -> None:
    """Send a listing notification to Telegram. Silently skips if not configured."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    message = (
        f"🏠 *{listing['source']}* — {listing['title']}\n\n"
        f"📍 {listing['area']}\n"
        f"💶 {listing['price']}\n"
        f"🔗 [View listing]({listing['url']})"
    )

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[Telegram] Failed to send: {e}")


# ── Scraper: Pararius ─────────────────────────────────────────────────────────
def scrape_pararius(city: str, max_rent: int) -> list[dict]:
    url      = f"https://www.pararius.nl/huurwoningen/{city}/0-{max_rent}"
    scraper  = make_scraper()
    listings = []

    try:
        response = scraper.get(url, timeout=15)
        response.raise_for_status()
    except Exception as e:
        print(f"[Pararius] Failed to fetch {city}: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    for item in soup.select("li.search-list__item--listing"):
        try:
            title_tag = item.select_one("a.listing-search-item__link--title")
            price_tag = item.select_one(".listing-search-item__price")
            area_tag  = item.select_one(".listing-search-item__sub-title")

            if not title_tag:
                continue

            price = clean_price(price_tag.get_text(strip=True) if price_tag else "")
            if not is_within_budget(price, max_rent):
                continue

            href = title_tag.get("href", "")
            listings.append(make_listing(
                id     = href,
                title  = title_tag.get_text(strip=True),
                price  = price,
                area   = area_tag.get_text(strip=True) if area_tag else city.capitalize(),
                url    = f"https://www.pararius.nl{href}",
                source = "Pararius",
            ))
        except Exception as e:
            print(f"[Pararius] Parse error: {e}")

    print(f"[Pararius] {city}: {len(listings)} listings on page")
    return listings


# ── Scraper: Huurwoningen ─────────────────────────────────────────────────────
def scrape_huurwoningen(city: str, max_rent: int) -> list[dict]:
    scraper  = make_scraper()
    response = None

    for template in HUURWONINGEN_URL_TEMPLATES:
        url = template.format(city=city, max_rent=max_rent)
        try:
            r = scraper.get(url, timeout=15)
            if r.status_code == 200:
                print(f"[Huurwoningen] {city}: connected → {url}")
                response = r
                break
            print(f"[Huurwoningen] {city}: {r.status_code} for {url}")
        except Exception as e:
            print(f"[Huurwoningen] {city}: error → {e}")

    if not response:
        print(f"[Huurwoningen] {city}: all URLs failed, skipping")
        return []

    soup     = BeautifulSoup(response.text, "html.parser")
    listings = []

    for item in soup.select("div.listing-search-item, li.search-list__item--listing"):
        try:
            title_tag = item.select_one(
                "a.listing-search-item__link--title, a[class*='title'], h2 a"
            )
            price_tag = item.select_one("[class*='price']")

            if not title_tag:
                continue

            price = clean_price(price_tag.get_text(strip=True) if price_tag else "")
            if not is_within_budget(price, max_rent):
                continue

            href = title_tag.get("href", "")
            if not href.startswith("http"):
                href = f"https://www.huurwoningen.nl{href}"

            listings.append(make_listing(
                id     = f"huurwoningen-{href}",
                title  = title_tag.get_text(strip=True),
                price  = price,
                area   = city.capitalize(),
                url    = href,
                source = "Huurwoningen",
            ))
        except Exception as e:
            print(f"[Huurwoningen] Parse error: {e}")

    print(f"[Huurwoningen] {city}: {len(listings)} listings on page")
    return listings


# ── Scraper: Holland2stay ─────────────────────────────────────────────────────
def scrape_holland2stay(city: str, max_rent: int) -> list[dict]:
    city_id = HOLLAND2STAY_CITIES.get(city.lower())
    if not city_id:
        return []  # city not supported by Holland2stay

    url = (
        "https://www.holland2stay.com/residences"
        f"?page=1"
        f"&city%5Bfilter%5D={city_id}"
        f"&available_to_book%5Bfilter%5D=Available+to+book%2C179"
        f"&available_to_book%5Bfilter%5D=Available+in+lottery%2C336"
    )

    listings = []

    try:
        with sync_playwright() as p:
            # Holland2stay is JS-rendered and Cloudflare-protected.
            # We use a visible (non-headless) browser + stealth patches to bypass detection.
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="nl-NL",
            )
            page = context.new_page()
            Stealth().apply_stealth_sync(page)

            # retry once on timeout
            for attempt in range(2):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    break
                except Exception:
                    if attempt == 1:
                        raise
                    print("[Holland2stay] timeout, retrying...")
                    time.sleep(5)

            page.wait_for_timeout(5000)  # let JS finish rendering the listing cards

            soup = BeautifulSoup(page.content(), "html.parser")
            context.close()
            browser.close()

        for item in soup.select("div.residence_block"):
            try:
                title_tag = item.select_one("h5.residence_name")
                price_tag = item.select_one("[class*='price'], [class*='rent'], [class*='cost']")
                href_tag  = item.select_one("a[href*='/residences/']")

                if not title_tag or not href_tag:
                    continue

                price = clean_price(price_tag.get_text(strip=True) if price_tag else "")
                if not is_within_budget(price, max_rent):
                    continue

                href = href_tag.get("href", "")
                if not href.startswith("http"):
                    href = f"https://www.holland2stay.com{href}"

                listings.append(make_listing(
                    id     = f"h2s-{href}",
                    title  = title_tag.get_text(strip=True),
                    price  = price,
                    area   = city.capitalize(),
                    url    = href,
                    source = "Holland2stay",
                ))
            except Exception as e:
                print(f"[Holland2stay] Parse error: {e}")

    except Exception as e:
        print(f"[Holland2stay] {city}: failed → {e}")

    print(f"[Holland2stay] {city}: {len(listings)} listings on page")
    return listings


# ── Scraper: Vesteda ─────────────────────────────────────────────────────────
def scrape_vesteda(city: str, max_rent: int) -> list[dict]:
    city_data = VESTEDA_CITIES.get(city.lower())
    if not city_data:
        return []

    url = (
        "https://www.vesteda.com/nl/woning-zoeken"
        f"?placeType=1&sortType=0&radius=5"
        f"&s={city_data['label']}"
        f"&sc=woning"
        f"&latitude={city_data['lat']}"
        f"&longitude={city_data['lon']}"
        f"&filters=0"
        f"&priceFrom=500"
        f"&priceTo={max_rent}"
    )

    listings = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="nl-NL",
            )
            page = context.new_page()
            Stealth().apply_stealth_sync(page)

            for attempt in range(2):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    break
                except Exception:
                    if attempt == 1:
                        raise
                    print("[Vesteda] timeout, retrying...")
                    time.sleep(5)

            page.wait_for_timeout(5000)  # wait for JS to render listings

            soup  = BeautifulSoup(page.content(), "html.parser")
            context.close()
            browser.close()

        for item in soup.select("div.o-card--listview-container"):
            try:
                title_tag = item.select_one("h2, h3, [class*='title'], [class*='address'], [class*='name']")
                price_tag = item.select_one(".o-card--listview-price")
                href_tag  = item.select_one("a[href*='/nl/huurwoning']")

                if not title_tag or not href_tag:
                    continue

                # Vesteda price format: "€ 1300,-per maand" or "Prijzen€ 880 – € 1395" (complex)
                raw_price = price_tag.get_text(strip=True) if price_tag else ""
                price     = clean_price(raw_price)

                # for price ranges (complex listings), use the lower bound for budget check
                range_match = re.search(r"€\s*([\d.,]+)\s*–", raw_price)
                check_price = f"€ {range_match.group(1)} per maand" if range_match else price

                if not is_within_budget(check_price, max_rent):
                    continue

                href = href_tag.get("href", "")
                if not href.startswith("http"):
                    href = f"https://www.vesteda.com{href}"

                listings.append(make_listing(
                    id     = f"vesteda-{href}",
                    title  = title_tag.get_text(strip=True),
                    price  = raw_price.strip(),  # show full price string including range
                    area   = city.capitalize(),
                    url    = href,
                    source = "Vesteda",
                ))
            except Exception as e:
                print(f"[Vesteda] Parse error: {e}")

    except Exception as e:
        print(f"[Vesteda] {city}: failed → {e}")

    print(f"[Vesteda] {city}: {len(listings)} listings on page")
    return listings


# ── Seen listings (persistent state across runs) ───────────────────────────────
def load_seen() -> list:
    """Load the list of listing IDs already seen in previous runs."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return json.load(f)
    return []


def save_seen(seen: list) -> None:
    """Save the updated list of seen listing IDs."""
    with open(SEEN_FILE, "w") as f:
        json.dump(seen, f, indent=2)


# ── Configuration ─────────────────────────────────────────────────────────────
CHECK_INTERVAL_MINUTES = 30  # how often to re-check all sites


# ── Single check cycle ────────────────────────────────────────────────────────
def run_check(seen: list) -> int:
    """Scrape all cities and sources. Returns the number of new listings found."""
    new_count = 0

    for i, city in enumerate(CITIES):
        print(f"\n{'─' * 40}")
        print(f"  Checking {city.capitalize()}")
        print(f"{'─' * 40}")

        all_listings  = scrape_pararius(city, MAX_RENT)
        random_delay(6, 10)
        all_listings += scrape_huurwoningen(city, MAX_RENT)
        random_delay(6, 10)
        all_listings += scrape_holland2stay(city, MAX_RENT)
        random_delay(6, 10)
        all_listings += scrape_vesteda(city, MAX_RENT)

        # pause between cities to avoid rate limiting (skip after the last city)
        if i < len(CITIES) - 1:
            random_delay(12, 20)

        city_new = 0
        for listing in all_listings:
            if listing["id"] not in seen:
                print(f"\n🏠 [{listing['source']}] {listing['title']}")
                print(f"   💶 {listing['price']}  |  📍 {listing['area']}")
                print(f"   🔗 {listing['url']}")
                send_telegram(listing)
                seen.append(listing["id"])
                new_count += 1
                city_new += 1

        total = len(all_listings)
        if city_new > 0:
            print(f"\n  → {city_new} NEW out of {total} total listings")
        else:
            print(f"  → No new listings ({total} already seen)")

    # Rebo shows all cities in one search (based on your saved search profile)
    # so we scrape it once per full run, not per city
    print(f"\n{'─' * 40}")
    print(f"  Checking Rebo (all cities)")
    print(f"{'─' * 40}")
    try:
        rebo_listings = ReboScraper().scrape()
        rebo_new = 0
        for listing in rebo_listings:
            if listing["id"] not in seen:
                print(f"\n🏠 [{listing['source']}] {listing['title']}")
                print(f"   💶 {listing['price']}  |  📍 {listing['area']}")
                print(f"   🔗 {listing['url']}")
                send_telegram(listing)
                seen.append(listing["id"])
                new_count += 1
                rebo_new += 1
        total = len(rebo_listings)
        if rebo_new > 0:
            print(f"\n  → {rebo_new} NEW out of {total} total listings")
        else:
            print(f"  → No new listings ({total} already seen)")
    except ValueError as e:
        print(f"[Rebo] Skipped: {e}")

    save_seen(seen)
    return new_count


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    """Single run — called once by GitHub Actions on each scheduled trigger."""
    seen = load_seen()
    now  = time.strftime("%H:%M:%S")
    print(f"\n{'═' * 40}")
    print(f"  Run —  {now}")
    print(f"{'═' * 40}")

    new_count = run_check(seen)
    print(f"\n✅ Done. {new_count} new listing(s) found.")


if __name__ == "__main__":
    main()
