"""Debug: set cookie InternalCalendarDate rồi reload."""
import urllib.parse, os
from playwright.sync_api import sync_playwright
from datetime import date

TARGET      = date(2026, 3, 12)
BOOKING_URL = "https://app.courtreserve.com/Online/Reservations/Bookings/15504?sId=21420"
SESSION     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toan_session.json")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--auto-open-devtools-for-tabs"])
    ctx  = browser.new_context(storage_state=SESSION, viewport={"width": 1400, "height": 900})
    page = ctx.new_page()

    date_str   = f"{TARGET.month}/{TARGET.day}/{TARGET.year}"
    cookie_val = urllib.parse.quote(date_str, safe="")
    print(f"Setting cookie InternalCalendarDate={cookie_val}")
    ctx.add_cookies([{
        "name":   "InternalCalendarDate",
        "value":  cookie_val,
        "domain": "app.courtreserve.com",
        "path":   "/",
    }])

    print("Loading page...")
    page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    print(f"Done. Browser should show {TARGET}. Press Enter to close...")
    input()
    browser.close()
