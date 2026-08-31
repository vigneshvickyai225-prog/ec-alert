"""
PARIVESH EC Agenda/MoM monitor for Tamil Nadu, Karnataka, Telangana.

Strategy:
  - Drive a real headless browser (Playwright) against https://parivesh.nic.in/#/ec
  - This is an Angular SPA with no documented public API, so we read the
    RENDERED page instead of hitting an API directly. That makes it more
    resilient to backend changes but sensitive to *frontend* DOM/text
    changes -- if PARIVESH redesigns the page, selectors below may need
    updating.
  - We take a debug screenshot after every major step. If something breaks,
    check the `debug_screenshots/` folder (uploaded as a GitHub Actions
    artifact) to see exactly what the browser saw.
  - Every run compares freshly scraped entries against seen_ids.json
    (committed back to the repo by the GitHub Action). Anything new
    triggers a Telegram message.

Run modes:
  - Normal: python scraper.py
  - Debug (verbose logging + screenshots, no Telegram send): python scraper.py --debug
"""

import json
import os
import re
import sys
import time
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import requests

BASE_URL = "https://parivesh.nic.in/#/ec"
STATES = ["Tamil Nadu", "Karnataka", "Telangana"]
SECTIONS = ["Agenda", "MOM"]  # sidebar labels seen in the screenshot

SEEN_FILE = Path(__file__).parent / "seen_ids.json"
SCREENSHOT_DIR = Path(__file__).parent / "debug_screenshots"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DEBUG = "--debug" in sys.argv


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def shot(page, name):
    """Save a screenshot for debugging. Always safe to call."""
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log(f"  screenshot saved: {path.name}")
    except Exception as e:
        log(f"  screenshot failed ({name}): {e}")


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False))


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- skipping send")
        log(f"  Would have sent:\n{text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        log(f"  Telegram send FAILED: {resp.status_code} {resp.text}")
    else:
        log("  Telegram message sent.")


def make_id(entry):
    """Stable identifier for a scraped entry, used for dedup."""
    basis = entry.get("link") or (entry.get("title", "") + entry.get("date", ""))
    return re.sub(r"\s+", " ", basis).strip()


def scrape_section(page, section_label, state_name):
    """
    Navigate to a section (Agenda / MOM), filter by state, and extract
    a list of {title, date, link} dicts.

    NOTE: The selectors below are best-effort based on the sidebar labels
    visible in the screenshot you shared ("Agenda", "MOM") and typical
    Angular Material patterns PARIVESH uses elsewhere on the site. They
    are intentionally text-based (not brittle CSS classes) so they have
    the best chance of working, but this section is the most likely to
    need a fix after the first real run -- send me debug_screenshots/
    if it doesn't find anything.
    """
    log(f"-- Section: {section_label} | State: {state_name} --")
    entries = []

    try:
        # Reset to base EC page each time for a clean state
        page.goto(BASE_URL, wait_until="networkidle", timeout=45000)
        shot(page, f"{section_label}_{state_name}_00_loaded")

        # Open the sidebar section (Agenda or MOM)
        section_locator = page.get_by_text(section_label, exact=True).first
        section_locator.click(timeout=15000)
        page.wait_for_timeout(1500)
        shot(page, f"{section_label}_{state_name}_01_section_opened")

        # Try to find a State dropdown/select and choose our state.
        # Common patterns: a <select>, or an Angular Material mat-select
        # (a clickable div that opens a floating option panel).
        state_set = False

        # Attempt 1: native <select> containing "State" nearby
        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            try:
                sel.select_option(label=state_name, timeout=3000)
                state_set = True
                log(f"  set state via native <select> #{i}")
                break
            except Exception:
                continue

        # Attempt 2: mat-select style dropdown -- click something with
        # placeholder/label text "State", then click the option text.
        if not state_set:
            try:
                dropdown = page.get_by_text(re.compile("state", re.I)).first
                dropdown.click(timeout=5000)
                page.wait_for_timeout(500)
                option = page.get_by_text(state_name, exact=True).first
                option.click(timeout=5000)
                state_set = True
                log("  set state via mat-select-style dropdown")
            except Exception as e:
                log(f"  could not set state dropdown: {e}")

        shot(page, f"{section_label}_{state_name}_02_state_selected")

        # Trigger search if there's a Search/Submit button
        try:
            search_btn = page.get_by_role(
                "button", name=re.compile("search|submit|go", re.I)
            ).first
            search_btn.click(timeout=5000)
            page.wait_for_timeout(2000)
        except Exception:
            log("  no explicit search button found (may auto-filter)")

        page.wait_for_load_state("networkidle", timeout=20000)
        shot(page, f"{section_label}_{state_name}_03_results")

        # Extract results. Try a table first, then fall back to list items.
        rows = page.locator("table tbody tr")
        row_count = rows.count()
        log(f"  found {row_count} table rows")

        if row_count > 0:
            for i in range(row_count):
                row = rows.nth(i)
                text = row.inner_text().strip()
                if not text:
                    continue
                link_el = row.locator("a").first
                href = None
                try:
                    href = link_el.get_attribute("href", timeout=1000)
                except Exception:
                    pass
                title = text.split("\n")[0][:300]
                entries.append({"title": title, "date": "", "link": href or "", "raw": text[:500]})
        else:
            # Fallback: list-like cards
            cards = page.locator("[class*='card'], [class*='list-item'], li")
            c = min(cards.count(), 100)
            log(f"  falling back to card/list scan, {c} candidates")
            for i in range(c):
                el = cards.nth(i)
                text = el.inner_text().strip()
                if len(text) < 15:
                    continue
                link_el = el.locator("a").first
                href = None
                try:
                    href = link_el.get_attribute("href", timeout=500)
                except Exception:
                    pass
                entries.append({"title": text[:300], "date": "", "link": href or "", "raw": text[:500]})

    except PWTimeout as e:
        log(f"  TIMEOUT in {section_label}/{state_name}: {e}")
        shot(page, f"{section_label}_{state_name}_ERROR")
    except Exception as e:
        log(f"  ERROR in {section_label}/{state_name}: {e}")
        shot(page, f"{section_label}_{state_name}_ERROR")

    log(f"  scraped {len(entries)} entries")
    return entries


def main():
    seen = load_seen()
    new_alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = context.new_page()

        for section in SECTIONS:
            for state in STATES:
                key = f"{section}:{state}"
                entries = scrape_section(page, section, state)

                seen_ids = set(seen.get(key, []))
                for entry in entries:
                    eid = make_id(entry)
                    if eid and eid not in seen_ids:
                        seen_ids.add(eid)
                        new_alerts.append((section, state, entry))

                seen[key] = sorted(seen_ids)

        browser.close()

    save_seen(seen)

    if not new_alerts:
        log("No new entries found.")
        return

    log(f"{len(new_alerts)} new entr(y/ies) found -- sending alerts.")
    for section, state, entry in new_alerts:
        msg = (
            f"\U0001F4E2 <b>New {section} published</b>\n"
            f"State: {state}\n"
            f"{entry['title']}\n"
            f"{entry['link'] or '(no direct link found)'}"
        )
        if DEBUG:
            log(f"  [debug] would send:\n{msg}")
        else:
            send_telegram(msg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args, _ = parser.parse_known_args()
    main()
