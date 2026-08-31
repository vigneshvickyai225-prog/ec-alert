# PARIVESH EC Monitor (Tamil Nadu / Karnataka / Telangana)

Watches https://parivesh.nic.in/#/ec for newly published **Agenda** and
**MoM** entries for Tamil Nadu, Karnataka, and Telangana, and sends a
Telegram alert when something new shows up. Runs on a schedule for free
via GitHub Actions -- no server needed.

## How it works

Because PARIVESH is a JavaScript app with no documented public API, this
uses **Playwright** to drive a real (headless) browser: it opens the page,
clicks through to the Agenda/MoM sections, filters by state, and reads
whatever is on screen. A GitHub Action runs this every 20 minutes,
compares results to `seen_ids.json` (committed back to the repo each run),
and Telegrams you anything new.

## One-time setup

1. **Create the repo**: push this folder's contents to a new GitHub repo
   (public or private, doesn't matter).

2. **Add two repo secrets** (Settings -> Secrets and variables -> Actions
   -> New repository secret):
   - `TELEGRAM_BOT_TOKEN` -- the token from @BotFather
   - `TELEGRAM_CHAT_ID` -- your chat ID (see below if you don't have it yet)

   **Getting your chat ID**: message your bot anything on Telegram, then
   visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a
   browser. Look for `"chat":{"id":...}` in the response -- that number is
   your `TELEGRAM_CHAT_ID`.

3. **Run it once manually** before trusting the schedule:
   - Go to the repo's **Actions** tab -> **PARIVESH EC Monitor** -> **Run workflow**
   - Let it finish, then open the run and download the
     **debug-screenshots** artifact

4. **Check the screenshots**, in order, for each section/state
   (e.g. `Agenda_Tamil Nadu_00_loaded.png`,
   `Agenda_Tamil Nadu_01_section_opened.png`, etc.):
   - Does `_01_section_opened` actually show the Agenda listing?
   - Does `_02_state_selected` show Tamil Nadu selected in the filter?
   - Does `_03_results` show a table/list of entries?

   If any step looks wrong, **that's expected on the first run** -- the
   selectors in `scraper.py` are my best guess from your earlier
   screenshot, not something I could verify against the live site myself.

## If a step is broken

Send me:
- The screenshot(s) where it goes wrong
- The **Actions log output** for that run (click into the "Run scraper"
  step)

I'll adjust the selectors in `scrape_section()` in `scraper.py` --
usually just a text label or a dropdown-detection tweak, not a rewrite.

## Notes / limitations

- If PARIVESH puts up a CAPTCHA or bot-detection wall, headless scraping
  will need adjusting (e.g. slower actions, a persistent browser profile).
  This isn't visible until we try a real run.
- The state-dropdown and results-table selectors are written defensively
  (text-based, with fallbacks) but this is the part most likely to need
  one round of fixing based on real screenshots.
- Runs every 20 minutes via GitHub's cron -- GitHub doesn't guarantee
  exact timing, especially under load, so treat "immediately" as
  "within a few minutes to ~20 minutes."
