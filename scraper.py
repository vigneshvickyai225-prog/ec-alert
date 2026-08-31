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
from datetime import date, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import requests

BASE_URL = "https://parivesh.nic.in/#/ec"
STATES = ["Tamil Nadu", "Karnataka", "Telangana"]
# Wide date range for the From Date / To Date filter fields -- these may
# be required for the search to actually filter (rather than silently
# falling back to the unfiltered default list, which is what we observed).
SEARCH_FROM_DATE = date(2015, 1, 1)
SEARCH_TO_DATE = date.today() + timedelta(days=1)
SECTIONS = ["Agenda", "MOM"]  # sidebar labels seen in the screenshot

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
    duplicates. Needed because Angular sometimes leaves a previous
    section's collapsed submenu in the DOM (hidden, not removed) --
    e.g. after scraping "Agenda", its "PARIVESH 2.0" link can still be
    present when we later look for MOM's "PARIVESH 2.0" link. A plain
    .first() can grab the hidden one and time out trying to click it.
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
        section_locator = page.get_by_text(section_label, exact=True)
        click_first_visible(section_locator, timeout=15000,
                             description=f"sidebar '{section_label}' link")
        page.wait_for_timeout(1000)
        shot(page, f"{section_label}_{state_name}_01a_submenu_opened")

        # The sidebar section expands into a submenu with version links
        # ("PARIVESH 2.0" / "PARIVESH 1.0"). We want the current version.
        # IMPORTANT: use click_first_visible here, not .first -- a
        # previously-scraped section's submenu (e.g. Agenda's) can leave
        # its own "PARIVESH 2.0" link hidden-but-present in the DOM, and
        # .first was grabbing that instead of the current section's link,
        # timing out because it's not clickable while hidden.
        version_link = page.get_by_text("PARIVESH 2.0", exact=True)
        click_first_visible(version_link, timeout=15000,
                             description="'PARIVESH 2.0' submenu link")
        page.wait_for_load_state("networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        shot(page, f"{section_label}_{state_name}_01_section_opened")

        # The default view lands on Committee Type = EAC (central-level,
        # not state-specific). Tamil Nadu / Karnataka / Telangana agendas
        # live under the STATE-level committees instead: SEIAA (State EIA
        # Authority) and SEAC (State Expert Appraisal Committee) -- see the
        # org chart on the EC overview page. We try both, since we don't
        # yet know which one (or both) carries a per-state agenda list.
        for committee_type in ["SEIAA", "SEAC"]:
            entries.extend(
                scrape_committee_type(page, section_label, state_name, committee_type)
            )

    except PWTimeout as e:
        log(f"  TIMEOUT in {section_label}/{state_name}: {e}")
        shot(page, f"{section_label}_{state_name}_ERROR")
    except Exception as e:
        log(f"  ERROR in {section_label}/{state_name}: {e}")
        shot(page, f"{section_label}_{state_name}_ERROR")

    log(f"  scraped {len(entries)} entries total for {section_label}/{state_name}")
    return entries


def scrape_committee_type(page, section_label, state_name, committee_type):
    """
    Within the already-open Agenda/MOM listing page, select a state-level
    Committee Type (SEIAA or SEAC), look for a resulting State filter, set
    it, search, and scrape the results table. Returns a list of entries
    (possibly empty if this committee type has no state filter or no
    results).
    """
    tag = f"{section_label}_{state_name}_{committee_type}"
    entries = []
    try:
        # Re-select the committee type radio button. IMPORTANT: plain text
        # matching is ambiguous here -- the sidebar also has a "SEIAA" link
        # under Notification & Order, and a loose text click grabbed that
        # instead of the actual radio button. Scope to role=radio so we only
        # ever hit the real form control.
        radio = page.get_by_role("radio", name=committee_type, exact=True).first
        radio.click(timeout=10000)
        page.wait_for_timeout(1200)
        shot(page, f"{tag}_a_committee_selected")

        # Look for a State filter. Try a native <select> first, then an
        # Angular Material-style dropdown whose label text is EXACTLY
        # "State" (never a substring match -- see note above about the
        # "Accessibility Statement" bug).
        #
        # The results table displays state names in ALL CAPS (e.g.
        # "MAHARASHTRA", "UTTAR PRADESH"), so the <select> options are very
        # likely uppercase too even though our STATES list is Title Case.
        # Try a few casings rather than assuming one.
        state_set = False
        state_label_variants = [state_name, state_name.upper(), state_name.title()]

        selects = page.locator("select")
        for i in range(selects.count()):
            sel = selects.nth(i)
            for variant in state_label_variants:
                try:
                    sel.select_option(label=variant, timeout=2000)
                    state_set = True
                    log(f"  [{committee_type}] set state via native <select> #{i} "
                        f"(label '{variant}')")
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
                        log(f"  [{committee_type}] set state via dropdown click "
                            f"('{variant}')")
                        break
                    except Exception:
                        continue
            except Exception as e:
                log(f"  [{committee_type}] no State filter found ({e}) -- "
                    f"this committee type may not support state filtering")

        shot(page, f"{tag}_b_state_selected")

        if not state_set:
            # No state filter for this committee type -- nothing reliable
            # to scrape/filter by state here. Bail out for this combo.
            return entries

        # Fill From Date / To Date with a wide range. These may be
        # required for the backend to actually apply the state filter --
        # earlier runs showed the State dropdown correctly set to e.g.
        # "TAMIL NADU" but the results table stayed completely unfiltered
        # (same row content and same huge page count as with no filter at
        # all), which points to a required-field fallback rather than a
        # click-target problem.
        try:
            date_inputs = page.locator("input[type='date']")
            if date_inputs.count() >= 2:
                date_inputs.nth(0).fill(SEARCH_FROM_DATE.isoformat())
                date_inputs.nth(1).fill(SEARCH_TO_DATE.isoformat())
                log(f"  [{committee_type}] filled date range "
                    f"{SEARCH_FROM_DATE} to {SEARCH_TO_DATE}")
            else:
                log(f"  [{committee_type}] expected 2 date inputs, found "
                    f"{date_inputs.count()}")
        except Exception as e:
            log(f"  [{committee_type}] could not fill date range: {e}")

        shot(page, f"{tag}_b3_dates_filled")

        # Capture the first row's text now so we can tell, after clicking
        # Search, whether the table content actually changed at all.
        try:
            row0_before = page.locator("table tbody tr").first.inner_text().strip()
        except Exception:
            row0_before = ""
        log(f"  [{committee_type}] first row BEFORE search click: {row0_before[:80]!r}")

        try:
            search_buttons = page.get_by_role(
                "button", name=re.compile(r"^\s*search\s*$", re.I)
            )
            count = search_buttons.count()
            log(f"  [{committee_type}] found {count} 'Search' button(s) on page")
            # Log each candidate's position so we can tell which one is
            # actually the filter's Search button vs. the top-nav one.
            for bi in range(count):
                try:
                    box = search_buttons.nth(bi).bounding_box()
                    log(f"    Search button #{bi}: bounding_box={box}")
                except Exception as e:
                    log(f"    Search button #{bi}: bounding_box failed ({e})")

            # The page has (at least) two "Search" buttons: a site-wide
            # search in the top navbar, and the actual filter's Search
            # button below the State dropdown. Use .last, which should be
            # further down the page (higher y-coordinate) than the navbar
            # one.
            search_btn = search_buttons.last
            search_btn.scroll_into_view_if_needed(timeout=5000)
            search_btn.click(timeout=5000)
            page.wait_for_timeout(500)
            shot(page, f"{tag}_b2_after_search_click")
            page.wait_for_timeout(1500)

            try:
                row0_after = page.locator("table tbody tr").first.inner_text().strip()
            except Exception:
                row0_after = ""
            log(f"  [{committee_type}] first row AFTER search click:  {row0_after[:80]!r}")

            if row0_after == row0_before:
                log(f"  [{committee_type}] table content DID NOT CHANGE after "
                    f"click -- trying a JS-dispatched click as a fallback")
                try:
                    search_btn.evaluate("el => el.click()")
                    page.wait_for_timeout(2000)
                    row0_retry = page.locator("table tbody tr").first.inner_text().strip()
                    log(f"  [{committee_type}] first row AFTER JS click:     "
                        f"{row0_retry[:80]!r}")
                except Exception as e:
                    log(f"  [{committee_type}] JS-dispatched click failed: {e}")
        except Exception as e:
            log(f"  [{committee_type}] search button click failed: {e}")

        page.wait_for_load_state("networkidle", timeout=20000)
        try:
            page.wait_for_selector("table tbody tr", timeout=15000)
        except PWTimeout:
            log(f"  [{committee_type}] no table rows appeared within timeout")
        shot(page, f"{tag}_c_results")

        rows = page.locator("table tbody tr")
        row_count = rows.count()
        log(f"  [{committee_type}] found {row_count} table rows")

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

        # Sanity log: pull out the State column value (2nd tab-separated
        # field in each row's text) so we can confirm filtering worked
        # just by reading the Actions log, without needing new screenshots
        # each round.
        states_seen = set()
        for t in row_texts:
            parts = t.split("\n")
            if len(parts) >= 2:
                states_seen.add(parts[1].strip())
        log(f"  [{committee_type}] distinct State values in results: {sorted(states_seen)}")

    except PWTimeout as e:
        log(f"  [{committee_type}] TIMEOUT: {e}")
        shot(page, f"{tag}_ERROR")
    except Exception as e:
        log(f"  [{committee_type}] ERROR: {e}")
        shot(page, f"{tag}_ERROR")

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
