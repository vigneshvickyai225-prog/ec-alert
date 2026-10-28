"""
PARIVESH EC Agenda/MoM monitor for Tamil Nadu, Karnataka, Telangana.

Strategy:
  - Drive a real headless browser (Playwright) against https://parivesh.nic.in/#/ec
  - This is an Angular SPA with no documented public API, so we read the
    RENDERED page instead of hitting an API directly.
  - For each of Agenda / MOM, and each state, we check the two state-level
    committees (SEIAA and SEAC), filter by state, and scrape the results
    table.
  - Every run compares freshly scraped entries against seen_ids.json
    (committed back to the repo by the GitHub Action). Anything new
    triggers a Telegram message.
  - On a normal run, only a handful of screenshots are taken (errors,
    mainly). Run with --debug for the full step-by-step screenshot trail
    used while building/fixing this script.

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
from datetime import date, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import requests

BASE_URL = "https://parivesh.nic.in/#/ec"
STATES = ["Tamil Nadu", "Karnataka", "Telangana"]
COMMITTEE_TYPES = ["SEIAA", "SEAC"]  # the two state-level committees
SECTIONS = ["Agenda", "MOM"]

# Wide date range for the From Date / To Date filter fields -- required
# for the backend to actually apply the state filter (confirmed by
# testing: without dates filled, the State dropdown showed the right
# state but results stayed completely unfiltered).
SEARCH_FROM_DATE = date(2015, 1, 1)
SEARCH_TO_DATE = date.today() + timedelta(days=1)

SEEN_FILE = Path(__file__).parent / "seen_ids.json"
SCREENSHOT_DIR = Path(__file__).parent / "debug_screenshots"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DEBUG = "--debug" in sys.argv


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def click_first_visible(locator, timeout=15000, description="element"):
    """
    Click the first VISIBLE match for a locator, skipping any hidden
    duplicates. Needed because Angular can leave a previous section's
    collapsed submenu in the DOM (hidden, not removed) -- e.g. after
    scraping "Agenda", its "PARIVESH 2.0" link can still be present when
    we later look for MOM's "PARIVESH 2.0" link. A plain .first() can
    grab the hidden one and time out trying to click it.
    """
    count = locator.count()
    for i in range(count):
        candidate = locator.nth(i)
        try:
            if candidate.is_visible():
                candidate.click(timeout=timeout)
                return
        except Exception:
            continue
    raise RuntimeError(
        f"No visible match found for {description} ({count} candidate(s) checked)"
    )


SECTION_ERRORS = 0  # count of section/state combos that failed outright


def goto_with_retry(page, url, attempts=3):
    """Open a page, retrying with a growing pause if the site refuses us."""
    for i in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            return
        except Exception as e:
            if i == attempts:
                raise
            log(f"  could not open site (attempt {i}/{attempts}): {str(e)[:80]} "
                f"-- retrying")
            time.sleep(5 * i)


def shot(page, name, force=False):
    """
    Save a screenshot. On a normal run this is a no-op unless force=True
    (used for error states) -- keeps scheduled runs fast and the
    debug-screenshots artifact small. Pass --debug to capture every step.
    """
    if not (DEBUG or force):
        return
    SCREENSHOT_DIR.mkdir(exist_ok=True)
    path = SCREENSHOT_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception as e:
        log(f"  screenshot failed ({name}): {e}")


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False))


def send_telegram(text, max_attempts=5):
    """
    Send a Telegram message. Returns True only if it was delivered.

    Telegram rate-limits bursts (HTTP 429 with a retry_after value). The
    first run can have ~100+ alerts, so we honour retry_after and retry
    instead of silently dropping messages.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- skipping send")
        log(f"  Would have sent:\n{text}")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for attempt in range(1, max_attempts + 1):
        try:
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
        except requests.RequestException as e:
            log(f"  Telegram send error (attempt {attempt}): {e}")
            time.sleep(3)
            continue
        if resp.status_code == 200:
            return True
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            log(f"  Telegram rate limit hit; waiting {wait}s (attempt {attempt})")
            time.sleep(wait + 1)
            continue
        log(f"  Telegram send FAILED: {resp.status_code} {resp.text}")
        return False
    return False


def make_id(entry):
    """Stable identifier for a scraped entry, used for dedup."""
    basis = entry.get("link") or (entry.get("title", "") + entry.get("date", ""))
    return re.sub(r"\s+", " ", basis).strip()


def scrape_section(page, section_label, state_name):
    """
    Navigate to a section (Agenda / MOM) and scrape both state-level
    committee types (SEIAA, SEAC) filtered to the given state.
    """
    log(f"-- Section: {section_label} | State: {state_name} --")
    entries = []

    try:
        goto_with_retry(page, BASE_URL)
        shot(page, f"{section_label}_{state_name}_00_loaded")

        # Open the sidebar section (Agenda or MOM)
        section_locator = page.get_by_text(section_label, exact=True)
        click_first_visible(section_locator, timeout=15000,
                             description=f"sidebar '{section_label}' link")
        page.wait_for_timeout(1000)

        # The sidebar section expands into a submenu with version links
        # ("PARIVESH 2.0" / "PARIVESH 1.0"). We want the current version.
        version_link = page.get_by_text("PARIVESH 2.0", exact=True)
        click_first_visible(version_link, timeout=15000,
                             description="'PARIVESH 2.0' submenu link")
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        shot(page, f"{section_label}_{state_name}_01_section_opened")

        # The default view lands on Committee Type = EAC (central-level,
        # not state-specific). Tamil Nadu / Karnataka / Telangana agendas
        # live under the state-level committees instead.
        for committee_type in COMMITTEE_TYPES:
            entries.extend(
                scrape_committee_type(page, section_label, state_name, committee_type)
            )

    except PWTimeout as e:
        global SECTION_ERRORS
        SECTION_ERRORS += 1
        log(f"  TIMEOUT in {section_label}/{state_name}: {e}")
        shot(page, f"{section_label}_{state_name}_ERROR", force=True)
    except Exception as e:
        SECTION_ERRORS += 1
        log(f"  ERROR in {section_label}/{state_name}: {e}")
        shot(page, f"{section_label}_{state_name}_ERROR", force=True)

    log(f"  scraped {len(entries)} entries total for {section_label}/{state_name}")
    return entries


def scrape_committee_type(page, section_label, state_name, committee_type):
    """
    Within the already-open Agenda/MOM listing page, select a state-level
    Committee Type (SEIAA or SEAC), set the State filter and date range,
    search, and scrape the results table.
    """
    tag = f"{section_label}_{state_name}_{committee_type}"
    entries = []
    try:
        # Committee Type radio buttons: scope to role=radio, not plain
        # text -- the sidebar also has a "SEIAA" link under Notification &
        # Order, and a loose text click grabbed that instead.
        radio = page.get_by_role("radio", name=committee_type, exact=True).first
        radio.click(timeout=10000)
        page.wait_for_timeout(1200)

        # State filter. Try a native <select> first, then a custom
        # dropdown whose label text is EXACTLY "State" (never a substring
        # match -- a looser match once matched "Accessibility Statement"
        # in the site footer and navigated away entirely).
        #
        # The results table displays state names in ALL CAPS, so try a
        # few casings for the option label rather than assuming one.
        state_set = False
        state_label_variants = [state_name, state_name.upper(), state_name.title()]

        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            for variant in state_label_variants:
                try:
                    sel.select_option(label=variant, timeout=2000)
                    state_set = True
                    break
                except Exception:
                    continue
            if state_set:
                break

        if not state_set:
            try:
                dropdown = page.get_by_text(
                    re.compile(r"^\s*state\s*$", re.I)
                ).first
                dropdown.click(timeout=4000)
                page.wait_for_timeout(500)
                for variant in state_label_variants:
                    try:
                        option = page.get_by_text(variant, exact=True).first
                        option.click(timeout=2000)
                        state_set = True
                        break
                    except Exception:
                        continue
            except Exception as e:
                log(f"  [{committee_type}] no State filter found ({e}) -- "
                    f"this committee type may not support state filtering")

        shot(page, f"{tag}_a_state_selected")

        if not state_set:
            # No state filter for this committee type -- nothing reliable
            # to scrape/filter by state here.
            return entries

        # Fill From Date / To Date with a wide range -- required for the
        # backend to actually apply the state filter.
        try:
            date_inputs = page.locator("input[type='date']")
            if date_inputs.count() >= 2:
                date_inputs.nth(0).fill(SEARCH_FROM_DATE.isoformat())
                date_inputs.nth(1).fill(SEARCH_TO_DATE.isoformat())
            else:
                log(f"  [{committee_type}] expected 2 date inputs, found "
                    f"{date_inputs.count()}")
        except Exception as e:
            log(f"  [{committee_type}] could not fill date range: {e}")

        # Click Search. NOTE: the page has TWO "Search" buttons -- a
        # site-wide search in the top navbar, and the actual filter's
        # Search button. .last targets the filter's button (further down
        # the page than the navbar one).
        try:
            row0_before = page.locator("table tbody tr").first.inner_text().strip()
        except Exception:
            row0_before = ""

        try:
            search_buttons = page.get_by_role(
                "button", name=re.compile(r"^\s*search\s*$", re.I)
            )
            search_btn = search_buttons.last
            search_btn.scroll_into_view_if_needed(timeout=5000)
            search_btn.click(timeout=5000)
            page.wait_for_timeout(2000)

            # Sanity check: if the table content didn't change at all,
            # try a JS-dispatched click as a fallback -- a real click and
            # a programmatic one can behave differently with some
            # framework event listeners.
            try:
                row0_after = page.locator("table tbody tr").first.inner_text().strip()
            except Exception:
                row0_after = ""
            if row0_after == row0_before:
                try:
                    search_btn.evaluate("el => el.click()")
                    page.wait_for_timeout(2000)
                except Exception as e:
                    log(f"  [{committee_type}] JS-dispatched click failed: {e}")
        except Exception as e:
            log(f"  [{committee_type}] search button click failed: {e}")

        page.wait_for_load_state("networkidle", timeout=20000)
        try:
            page.wait_for_selector("table tbody tr", timeout=15000)
        except PWTimeout:
            log(f"  [{committee_type}] no table rows appeared within timeout")
        shot(page, f"{tag}_b_results")

        rows = page.locator("table tbody tr")
        row_count = rows.count()

        row_texts = []
        for i in range(row_count):
            row = rows.nth(i)
            text = row.inner_text().strip()
            if not text:
                continue
            row_texts.append(text)
            link_el = row.locator("a").first
            href = None
            try:
                href = link_el.get_attribute("href", timeout=1000)
            except Exception:
                pass
            title = text.split("\n")[0][:300]
            entries.append({
                "title": f"[{committee_type}] {title}",
                "date": "",
                "link": href or "",
                "raw": text[:500],
            })

        # Sanity log: distinct State values actually found in the results,
        # so a quick glance at the Actions log confirms filtering worked.
        states_seen = set()
        for t in row_texts:
            parts = t.split("\n")
            if len(parts) >= 2:
                states_seen.add(parts[1].strip())
        log(f"  [{committee_type}] {row_count} rows, states found: {sorted(states_seen)}")

    except PWTimeout as e:
        log(f"  [{committee_type}] TIMEOUT: {e}")
        shot(page, f"{tag}_ERROR", force=True)
    except Exception as e:
        log(f"  [{committee_type}] ERROR: {e}")
        shot(page, f"{tag}_ERROR", force=True)

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
                        new_alerts.append((section, state, key, eid, entry))

        browser.close()

    total_combos = len(SECTIONS) * len(STATES)
    if SECTION_ERRORS >= total_combos:
        log("ALL sections failed -- PARIVESH looks unreachable from this "
            "runner (down, or blocking GitHub's servers). Failing the run "
            "so it shows red instead of a false green.")
        sys.exit(1)

    if not new_alerts:
        log("No new entries found.")
        return

    log(f"{len(new_alerts)} new entr(y/ies) found -- sending alerts.")
    for section, state, key, eid, entry in new_alerts:
        msg = (
            f"\U0001F4E2 <b>New {section} published</b>\n"
            f"State: {state}\n"
            f"{entry['title']}\n"
            f"{entry['link'] or '(no direct link found)'}"
        )
        if DEBUG:
            log(f"  [debug] would send:\n{msg}")
            continue
        # Only mark an entry as "seen" once Telegram actually accepted it,
        # so a failed/rate-limited send is retried on the next run instead
        # of being lost forever.
        if send_telegram(msg):
            ids = set(seen.get(key, []))
            ids.add(eid)
            seen[key] = sorted(ids)
            save_seen(seen)
            time.sleep(1.1)  # stay under Telegram's ~1 msg/sec limit
        else:
            log(f"  Not marking as seen (send failed): {eid}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args, _ = parser.parse_known_args()
    main()
