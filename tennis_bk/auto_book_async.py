import asyncio
import os
from datetime import datetime
import aiohttp
from playwright.async_api import async_playwright

# ================== CONFIG ==================

USERNAME = "Gpham88@hotmail.com"
PASSWORD = "Yvrsgn88!"

STORAGE_FILE = "storage_state.json"

LOGIN_URL = "https://nvrc.perfectmind.com/23734/MemberRegistration/MemberSignIn"
START_PAGE = (
    "https://nvrc.perfectmind.com/23734/Clients/BookMe4BookingPages/Classes"
    "?widgetId=a28b2c65-61af-407f-80d1-eaa58f30a94a"
    "&calendarId=d0a5979d-2f83-4696-997e-ea18f86cbf30"
    "&singleCalendarWidget=False"
)

API_URL = "https://nvrc.perfectmind.com/23734/Clients/BookMe4BookingPagesV2/ClassesV2"

WIDGET_ID = "a28b2c65-61af-407f-80d1-eaa58f30a94a"
CALENDAR_ID = "d0a5979d-2f83-4696-997e-ea18f86cbf30"
LOCATION_ID = "346dc9e5-e7a4-4bf1-805f-a7d191295dcc"

DATE_START = "2026-02-28T00:00:00.000Z"
DATE_END   = "2026-02-28T00:00:00.000Z"

WORKER_COUNT = 5
WORKER_START_GAP = 0.05  # 50ms stagger

# ================== POLLER ==================

async def poller(worker_id, stop_event, navigate_lock, session, page, form_data):
    try:
        while not stop_event.is_set():
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

            try:
                async with session.post(API_URL, data=form_data) as resp:
                    data = await resp.json()
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientError):
                # Session closed, another worker succeeded
                if stop_event.is_set():
                    return
                raise  # Re-raise if not expected

            # Check if another worker already found it
            if stop_event.is_set():
                print(f"[{ts}] W{worker_id}: stopped by another worker")
                return

            classes = data.get("classes", [])
            print(f"[{ts}] W{worker_id}: classes={len(classes)}")

            for c in classes:
                # Check before processing each class
                if stop_event.is_set():
                    print(f"W{worker_id}: stopped during processing")
                    return
                if c.get("BookButtonText") == "Book Now":
                    # Try to acquire lock - only first worker succeeds
                    if navigate_lock.locked():
                        # Another worker already navigating, exit
                        return
                    
                    async with navigate_lock:
                        # Double check after acquiring lock
                        if stop_event.is_set():
                            return
                        
                        hit_ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

                        print(
                            "\n🔥🔥🔥 BOOK NOW FOUND 🔥🔥🔥\n"
                            f"Worker : W{worker_id}\n"
                            f"Time   : {hit_ts}\n"
                            f"Event  : {c['EventName']}\n"
                        )

                        stop_event.set()

                        url = (
                            "https://nvrc.perfectmind.com/23734/Clients/BookMe4EventParticipants"
                            f"?eventId={c['EventId']}"
                            f"&occurrenceDate={c['OccurrenceDate']}"
                            f"&widgetId={WIDGET_ID}"
                            f"&locationId={LOCATION_ID}"
                            "&waitListMode=False"
                        )

                        try:
                            print(f"🔄 W{worker_id}: Navigating to booking page...")
                            await page.goto(url, wait_until="domcontentloaded")
                            print(f"✅ W{worker_id}: Navigation complete!")
                        except Exception as e:
                            print(f"⚠️  W{worker_id}: Navigation error: {type(e).__name__}")
                        
                        # Keep browser open for user to complete booking
                        print(f"⏸️  W{worker_id}: Waiting for manual booking completion...")
                        while True:
                            await asyncio.sleep(1)
    except asyncio.CancelledError:
        print(f"W{worker_id}: cancelled")
        raise

# ================== MAIN ==================

async def main():
    stop_event = asyncio.Event()
    navigate_lock = asyncio.Lock()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)

        # ---- LOAD / CREATE CONTEXT ----
        if os.path.exists(STORAGE_FILE):
            print("♻️  Load session from storage_state.json")
            context = await browser.new_context(storage_state=STORAGE_FILE)
        else:
            print("🆕 No session file, create new context")
            context = await browser.new_context()

        page = await context.new_page()
        await page.goto(START_PAGE)

        # ---- CHECK LOGIN ----
        if "MemberSignIn" in page.url:
            print("❌ Session expired → login")

            await page.goto(LOGIN_URL)
            await page.fill("input[name='username']", USERNAME)
            await page.fill("input[name='password']", PASSWORD)
            await page.click("#buttonLogin")
            await page.wait_for_load_state("networkidle")

            await context.storage_state(path=STORAGE_FILE)
            print("💾 Session saved")
        else:
            print("✅ Session still valid")

        # ---- SHARE COOKIE TO AIOHTTP ----
        cookies = await context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        headers = {
            "accept": "*/*",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
            "cookie": cookie_str,
        }

        form_data = {
            "calendarId": CALENDAR_ID,
            "widgetId": WIDGET_ID,
            "page": "0",
            "values[0][Name]": "Keyword",
            "values[0][Value]": "",
            "values[0][Value2]": "",
            "values[0][ValueKind]": "9",
            "values[1][Name]": "Date Range",
            "values[1][Value]": DATE_START,
            "values[1][Value2]": DATE_END,
            "values[1][ValueKind]": "6",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            workers = []

            for i in range(WORKER_COUNT):
                print(f"🚀 Start worker W{i}")
                task = asyncio.create_task(
                    poller(i, stop_event, navigate_lock, session, page, form_data)
                )
                workers.append(task)
                await asyncio.sleep(WORKER_START_GAP)

            # Wait for first worker to complete
            done, pending = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)

            # Cancel all pending workers  
            for task in pending:
                task.cancel()

            # Wait for all to finish cleanup
            if pending:
                await asyncio.wait(pending, return_when=asyncio.ALL_COMPLETED)

        print("✅ DONE - Browser will stay open for manual booking")
        print("Press Ctrl+C to exit")
        
        # Keep browser open
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n👋 Closing browser...")
        
        await browser.close()

# ================== RUN ==================

asyncio.run(main())
