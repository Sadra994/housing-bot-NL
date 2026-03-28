"""
Scraper for rebowonenhuur.nl
Requires login — credentials are read from environment variables or a local .env file.

Usage:
    Set REBO_EMAIL and REBO_PASSWORD in a .env file next to this script, or
    as environment variables before running.
"""

import os
import re
import time

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


# ── Credentials ───────────────────────────────────────────────────────────────
# Store your login details in a .env file:
#   REBO_EMAIL=your@email.com
#   REBO_PASSWORD=yourpassword
#
# Never hardcode passwords directly in code.

def _load_env() -> None:
    """Load .env file if it exists (simple key=value parser, no extra libs needed)."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


# ── Rebo scraper class ────────────────────────────────────────────────────────
class ReboScraper:
    """
    Logs into rebowonenhuur.nl and scrapes the search results page.
    Uses Playwright for JS rendering and login session management.
    """

    BASE_URL   = "https://rebowonenhuur.nl"
    LOGIN_URL  = "https://rebowonenhuur.nl/inloggen"
    SEARCH_URL = "https://rebowonenhuur.nl/zoekopdracht/"
    MAX_RENT   = 1250  # separate budget for this site

    def __init__(self):
        _load_env()
        self.email    = os.environ.get("REBO_EMAIL", "")
        self.password = os.environ.get("REBO_PASSWORD", "")

        if not self.email or not self.password:
            raise ValueError(
                "REBO_EMAIL and REBO_PASSWORD must be set in a .env file or environment variables.\n"
                "Create a file called .env in your project folder with:\n"
                "  REBO_EMAIL=your@email.com\n"
                "  REBO_PASSWORD=yourpassword"
            )

    def scrape(self) -> list[dict]:
        """Login and return all listings within budget as a list of dicts."""
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

                # ── Step 1: log in ─────────────────────────────────────────
                print("[Rebo] Logging in...")
                page.goto(self.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)

                page.fill("input[type='email'], input[name*='email'], input[id*='email']", self.email)
                page.fill("input[type='password']", self.password)
                page.click("button[type='submit'], input[type='submit']")
                page.wait_for_timeout(3000)

                # check if login succeeded by looking for error message
                if "onjuist" in page.content().lower() or "incorrect" in page.content().lower():
                    print("[Rebo] Login failed — check your email/password in .env")
                    context.close()
                    browser.close()
                    return []

                print("[Rebo] Login successful")

                # ── Step 2: go to search results ───────────────────────────
                page.goto(self.SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)

                soup = BeautifulSoup(page.content(), "html.parser")
                context.close()
                browser.close()

            # ── Step 3: parse listings ─────────────────────────────────────
            # Save debug HTML if nothing found
            raw_items = soup.select(
                "div.property-card, article.property, div[class*='result'], "
                "div[class*='listing'], div[class*='woning'], div[class*='card']"
            )

            if not raw_items:
                with open("rebo_debug.html", "w", encoding="utf-8") as f:
                    f.write(soup.prettify())
                print("[Rebo] 0 results — saved rebo_debug.html for inspection")
                return []

            for item in raw_items:
                try:
                    title_tag = item.select_one("h2, h3, h4, [class*='title'], [class*='address'], [class*='name']")
                    price_tag = item.select_one("[class*='price'], [class*='rent'], [class*='huur']")
                    href_tag  = item.select_one("a")

                    if not title_tag or not href_tag:
                        continue

                    title = title_tag.get_text(strip=True)
                    href  = href_tag.get("href", "")
                    if not href.startswith("http"):
                        href = f"{self.BASE_URL}{href}"

                    raw_price = price_tag.get_text(strip=True) if price_tag else ""
                    price     = self._clean_price(raw_price)

                    if not self._is_within_budget(price):
                        continue

                    listings.append({
                        "id":     f"rebo-{href}",
                        "title":  title,
                        "price":  price,
                        "area":   "Utrecht/Amersfoort",
                        "url":    href,
                        "source": "Rebo",
                    })
                except Exception as e:
                    print(f"[Rebo] Parse error: {e}")

        except Exception as e:
            print(f"[Rebo] Failed: {e}")

        print(f"[Rebo] {len(listings)} listings on page within budget (max €{self.MAX_RENT})")
        return listings

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _clean_price(self, raw: str) -> str:
        if not raw:
            return "Prijs op aanvraag"
        match = re.search(r"(€[\s\d.,]+-?,?)", raw)
        if match:
            return match.group(1).strip()
        if "aanvraag" in raw.lower():
            return "Prijs op aanvraag"
        return raw.strip()[:30]

    def _is_within_budget(self, price_str: str) -> bool:
        cleaned = price_str.replace(".", "").replace(",", ".").replace("-", "")
        match   = re.search(r"(\d+)", cleaned)
        if not match:
            return True  # unknown price — keep it
        return float(match.group(1)) <= self.MAX_RENT
