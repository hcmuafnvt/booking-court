"""
Court Booking Bot - Recurring scheduler
Locations config : pickleball/config.json
Bookings         : pickleball/scheduled_bookings.json
History          : pickleball/courts_booked.json
"""

from playwright.sync_api import sync_playwright
from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime, timedelta
import json
import os
import time
import logging
import threading

# ── Paths ──────────────────────────────────────────────────────────────────
DIR                  = os.path.dirname(os.path.abspath(__file__))
LOCATION_CONFIG_FILE = os.path.join(DIR, "config.json")
BOOKINGS_FILE        = os.path.join(DIR, "scheduled_bookings.json")
COURTS_BOOKED_FILE   = os.path.join(DIR, "courts_booked.json")
SESSION_FILE         = os.path.join(DIR, "session.json")

LOGIN_URL = "https://app.courtreserve.com/Online/Account/LogIn/15504"

BOOKED   = "BOOKED"
FAILED   = "FAILED"
WATCHING = "WATCHING"
BOOKING  = "BOOKING"

DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("BookBot")


# ── Config & State ─────────────────────────────────────────────────────────
def load_location_cfg(location):
    """Load config.json and return the sub-dict for the given location key."""
    with open(LOCATION_CONFIG_FILE, "r") as f:
        all_cfg = json.load(f)
    if location not in all_cfg:
        raise KeyError(f"Location '{location}' not found in {LOCATION_CONFIG_FILE}")
    return all_cfg[location]

def load_bookings():
    """Load scheduled_bookings.json (recurring + one_time rules)."""
    with open(BOOKINGS_FILE, "r") as f:
        return json.load(f)

def load_history():
    if not os.path.exists(COURTS_BOOKED_FILE):
        return []
    with open(COURTS_BOOKED_FILE, "r") as f:
        return json.load(f)

def save_history(records):
    with open(COURTS_BOOKED_FILE, "w") as f:
        json.dump(records, f, indent=2, default=str)

def upsert_record(rule_id, date_str, status, note="", extra=None):
    records = load_history()
    idx = next((i for i, r in enumerate(records)
                if r.get("id") == rule_id and r.get("date") == date_str), None)
    day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
    rec = records[idx] if idx is not None else {"id": rule_id, "date": date_str, "day": day}
    rec.update({"day": day, "status": status, "note": note, "updated": datetime.now().isoformat()})
    if extra:
        rec.update(extra)
    if idx is not None:
        records[idx] = rec
    else:
        records.append(rec)
    save_history(records)
    log.info(f"[HISTORY] {rule_id} {date_str} -> {status}  {note}")

def _rule_meta(rule, is_recurring):
    """Extract rule metadata for history records."""
    return {
        "type": "Recurring" if is_recurring else "One-time",
        "start": rule.get("start", ""),
        "end": rule.get("end", ""),
        "location": rule.get("location", ""),
        "who": rule.get("who", ""),
        "courts_requested": rule.get("courts", 1),
    }

def get_status(rule_id, date_str):
    records = load_history()
    rec = next((r for r in records
                if r.get("id") == rule_id and r.get("date") == date_str), None)
    return rec["status"] if rec else None


# ── Date helpers ───────────────────────────────────────────────────────────
def get_upcoming_dates(days, weeks=2):
    today = datetime.now().date()
    result = []
    for i in range(1, weeks * 7 + 1):  # bắt đầu từ ngày mai
        d = today + timedelta(days=i)
        if DAY_NAMES[d.weekday()] in days:
            result.append(d)
    return result

def open_datetime_for(target_date, open_time="19:00", days_before=14):
    h, m = map(int, open_time.split(":"))
    open_date = target_date - timedelta(days=days_before)
    return datetime(open_date.year, open_date.month, open_date.day, h, m)

def is_slot_open(target_date, cfg):
    open_dt = open_datetime_for(target_date, cfg.get("open_time", "20:00"), cfg.get("open_days_before", 14))
    return datetime.now() >= open_dt

def watch_trigger_dt(target_date, cfg):
    open_dt = open_datetime_for(target_date, cfg.get("open_time", "20:00"), cfg.get("open_days_before", 14))
    return open_dt - timedelta(minutes=cfg["watch_before_minutes"])


# ── Playwright helpers ─────────────────────────────────────────────────────
def fill_react_input(page, selector, value):
    page.evaluate("""
        ([selector, value]) => {
            const input = document.querySelector(selector);
            if (!input) return;
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(input, value);
            input.dispatchEvent(new Event('input',  { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
        }
    """, [selector, value])

def is_session_valid(page, booking_url):
    log.info("[LOGIN] Checking session...")
    page.goto(booking_url, wait_until="domcontentloaded")
    time.sleep(3)
    if any(x in page.url for x in ["LogIn", "Login", "login"]):
        log.info("[LOGIN] Session expired.")
        return False
    log.info("[LOGIN] Session valid.")
    return True

def do_login(page, context):
    log.info("[LOGIN] Logging in...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    time.sleep(3)
    for selector, value in [('input[name="email"]', "itlab.nguyenvantoan@gmail.com"),
                             ('input[name="password"]', "Pass4now!")]:
        el = page.locator(selector)
        el.wait_for(state="visible", timeout=10000)
        el.click()
        el.click(click_count=3)
        fill_react_input(page, selector, value)
        time.sleep(0.5)
    page.locator('button[data-testid="Continue"]').click()
    try:
        page.wait_for_url(lambda u: "LogIn" not in u and "Login" not in u, timeout=15000)
    except Exception:
        time.sleep(3)
        if "LogIn" in page.url or "Login" in page.url:
            raise Exception("Login failed!")
    context.storage_state(path=SESSION_FILE)
    log.info("[LOGIN] Success, session saved.")

def open_browser(test_mode=False):
    p = sync_playwright().start()
    _slow = 200 if test_mode else 0
    browser = p.chromium.launch(headless=False, slow_mo=_slow)
    context = browser.new_context(storage_state=SESSION_FILE) if os.path.exists(SESSION_FILE) \
              else browser.new_context()
    page = context.new_page()
    page.on("console", lambda msg: log.info(f"[BROWSER] {msg.text}") if "[BOT]" in msg.text else None)
    return p, browser, context, page

def ensure_logged_in(page, context, booking_url):
    if not (os.path.exists(SESSION_FILE) and is_session_valid(page, booking_url)):
        do_login(page, context)

def navigate_to_date(page, target_date):
    """Navigate calendar tới đúng ngày bằng cách click UI calendar."""
    log.info(f"[BOT] Navigating to {target_date}...")

    # data-value format: "YYYY/M-1/D" (month là 0-indexed)
    data_value = f"{target_date.year}/{target_date.month - 1}/{target_date.day}"
    log.info(f"[BOT] Looking for data-value='{data_value}'")

    # Click vào header để mở calendar picker
    page.locator('a.k-nav-current').first.click()
    time.sleep(0.5)

    # Thử navigate tháng nếu ngày chưa visible (tối đa 2 lần vì chỉ 2 tuần)
    for _ in range(2):
        link = page.locator(f'a.k-link[data-value="{data_value}"]')
        if link.count() > 0:
            log.info(f"[BOT] Found date link, clicking...")
            link.first.click()
            time.sleep(2)
            log.info(f"[BOT] Navigated to {target_date}.")
            return

        # Chưa thấy → click Next để sang tháng tiếp
        log.info("[BOT] Date not in current view, clicking Next month...")
        page.locator('a.k-nav-next[data-action="next"]').click()
        time.sleep(0.5)

    raise Exception(f"Could not find date {target_date} in calendar after 3 months.")

def parse_duration_label(start_str, end_str):
    """Tính duration label từ start/end, e.g. '7:00 PM'-'9:00 PM' -> '2 hours'"""
    try:
        fmt = "%I:%M %p"
        s = datetime.strptime(start_str.strip(), fmt)
        e = datetime.strptime(end_str.strip(), fmt)
        hours = int((e - s).seconds / 3600)
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    except Exception:
        return None


def select_duration(page, preferred_label=None):
    # Mở dropdown
    page.locator('span[aria-owns="Duration_listbox"]').click()
    time.sleep(0.5)
    page.wait_for_selector('#Duration_listbox', state='visible', timeout=5000)

    items = page.locator('#Duration_listbox li.k-list-item span.k-list-item-text')
    count = items.count()
    log.info(f"[BOT] Duration items: {count}, preferred: {preferred_label}")

    # Thử chọn đúng duration từ start/end trước
    if preferred_label:
        for i in range(count):
            if items.nth(i).text_content().strip() == preferred_label:
                items.nth(i).click()
                log.info(f"[BOT] Selected duration: {preferred_label}")
                return preferred_label

    # Fallback: 2 hours → 1 hour
    for target in ["2 hours", "1 hour"]:
        for i in range(count):
            if items.nth(i).text_content().strip() == target:
                items.nth(i).click()
                log.info(f"[BOT] Selected duration (fallback): {target}")
                return target

    log.warning("[BOT] No duration options found!")
    return None

def wait_for_slots_open(page, target_date, start_slot, open_time_str):
    """
    1. MutationObserver chờ link 'Click HERE' xuất hiện trong #ReservationOpenTimeDispplay.
    2. Click HERE → scheduler tự navigate sang ngày mới.
    3. MutationObserver chờ reserveBtn mất class .hide.
    """
    here_selector = '#ReservationOpenTimeDispplay .here-link-text a'

    # ── Bước 1: MutationObserver — fire ngay khi link HERE xuất hiện ──
    log.info(f"[BOT] Watching DOM cho link 'HERE'...")
    page.evaluate("""
        () => new Promise(resolve => {
            const el = document.getElementById('ReservationOpenTimeDispplay');
            if (!el) { resolve(); return; }
            if (el.querySelector('.here-link-text a')) { resolve(); return; }
            const obs = new MutationObserver(() => {
                if (el.querySelector('.here-link-text a')) {
                    obs.disconnect();
                    resolve();
                }
            });
            obs.observe(el, { childList: true, subtree: true });
        })
    """)
    log.info(f"[BOT] ✅ Link HERE xuất hiện!")

    # ── Bước 2: Click HERE → scheduler navigate sang ngày mới ──
    log.info(f"[BOT] Clicking 'HERE' link...")
    page.locator(here_selector).first.click()
    time.sleep(2)

    # ── Bước 3: MutationObserver — fire ngay khi reserveBtn mất class .hide ──
    log.info(f"[BOT] Watching Reserve button cho '{start_slot}'...")
    page.evaluate(f"""
        () => new Promise(resolve => {{
            const selector = 'tr[data-testid="{start_slot}"] button[data-testid="reserveBtn"]';
            // Kiểm tra ngay nếu đã có button visible
            const btns = document.querySelectorAll(selector);
            for (const b of btns) {{
                if (!b.classList.contains('hide')) {{ resolve(); return; }}
            }}
            // Observe attribute changes trên container của scheduler
            const container = document.querySelector('#CourtsScheduler') || document.body;
            const obs = new MutationObserver(() => {{
                const btns = document.querySelectorAll(selector);
                for (const b of btns) {{
                    if (!b.classList.contains('hide')) {{
                        obs.disconnect();
                        resolve();
                        return;
                    }}
                }}
            }});
            obs.observe(container, {{ subtree: true, attributes: true, attributeFilter: ['class'] }});
        }})
    """)
    log.info(f"[BOT] ✅ Reserve button sẵn sàng!")


def book_slot(page, time_slot, courts=1, duration_label=None, test_mode=False):
    """Book `courts` số court tại cùng 1 time_slot (tuần tự, fallback)."""
    log.info(f"[BOT] Booking slot '{time_slot}' x{courts} (duration: {duration_label})...")
    try:
        page.wait_for_selector(f'tr[data-testid="{time_slot}"]', timeout=10000)
    except Exception:
        log.warning(f"[BOT] Row '{time_slot}' not found.")
        return 0

    booked = 0
    for i in range(courts):
        btns = page.locator(f'tr[data-testid="{time_slot}"] button[data-testid="reserveBtn"]:not(.hide)')
        if btns.count() == 0:
            log.info(f"[BOT] No more available courts at '{time_slot}' (booked {booked}/{courts}).")
            break
        btn = btns.first
        log.info(f"[BOT] Court {i+1}: {btn.get_attribute('courtlabel')} — clicking Reserve...")
        btn.click()
        page.wait_for_selector('#modal1.show', timeout=10000)
        log.info("[BOT] Popup opened!")
        time.sleep(1)
        select_duration(page, duration_label)
        if test_mode:
            log.info("[TEST_MODE] Dừng sau khi chọn duration — KHÔNG submit, giữ browser mở.")
            break
        try:
            page.locator('#modal1 button[type="submit"], #modal1 .btn-primary').first.click()
            page.wait_for_selector('#modal1.show', state='hidden', timeout=10000)
            time.sleep(1)
        except Exception:
            pass
        booked += 1
        log.info(f"[BOT] Court {i+1} BOOKED!")

    log.info(f"[BOT] Slot '{time_slot}': booked {booked}/{courts}.")
    return booked


def try_book_slot(page, time_slot, courts=1, duration_label=None):
    return book_slot(page, time_slot, courts, duration_label) > 0


def get_available_courts(page, time_slot):
    """Trả về list courtlabel còn available trong time_slot."""
    try:
        page.wait_for_selector(f'tr[data-testid="{time_slot}"]', timeout=10000)
    except Exception:
        log.warning(f"[BOT] Row '{time_slot}' not found when scouting.")
        return []
    btns = page.locator(f'tr[data-testid="{time_slot}"] button[data-testid="reserveBtn"]:not(.hide)')
    labels = [btns.nth(i).get_attribute('courtlabel') for i in range(btns.count())]
    log.info(f"[BOT] Available courts at '{time_slot}': {labels}")
    return labels


def book_specific_court(page, time_slot, courtlabel, duration_label=None, test_mode=False):
    """Book đúng 1 court theo courtlabel."""
    log.info(f"[BOT] Booking court '{courtlabel}' at '{time_slot}'...")
    try:
        page.wait_for_selector(f'tr[data-testid="{time_slot}"]', timeout=10000)
    except Exception:
        log.warning(f"[BOT] Row '{time_slot}' not found.")
        return 0
    btn = page.locator(
        f'tr[data-testid="{time_slot}"] button[data-testid="reserveBtn"][courtlabel="{courtlabel}"]:not(.hide)'
    ).first
    if btn.count() == 0:
        log.warning(f"[BOT] Court '{courtlabel}' not available anymore.")
        return 0
    log.info(f"[BOT] Clicking Reserve for court '{courtlabel}'...")
    btn.click()
    page.wait_for_selector('#modal1.show', timeout=10000)
    log.info("[BOT] Popup opened!")
    time.sleep(1)
    select_duration(page, duration_label)
    if test_mode:
        log.info("[TEST_MODE] Dừng sau khi chọn duration — KHÔNG submit, giữ browser mở.")
        return 0
    try:
        checkbox = page.locator('#modal1 input[data-testid="DisclosureAgree"]').first
        if checkbox.count() > 0:
            log.info("[BOT] Found DisclosureAgree checkbox — checking via label click...")
            page.locator('#modal1 label[for="DisclosureAgree"]').first.click()
            time.sleep(0.5)
        page.locator('#modal1 button[type="submit"], #modal1 .btn-primary').first.click()
        page.wait_for_selector('#modal1.show', state='hidden', timeout=10000)
        time.sleep(1)
    except Exception:
        pass
    log.info(f"[BOT] Court '{courtlabel}' BOOKED!")
    return 1


# ── Jobs ───────────────────────────────────────────────────────────────────
def _pick_courtlabel(btns, court_index, preferred_courts, loc_cfg=None):
    """
    Chọn court cho thread court_index:
      - available = courts trên trang giới hạn trong loc_cfg["courts"]
      1. Dùng preferred_courts[court_index] nếu có và available.
      2. Fallback: court đầu tiên available mà KHÔNG nằm trong preferred_courts.
      3. Last resort: court đầu tiên bất kỳ còn available.
    """
    allowed = (loc_cfg or {}).get("courts", None)   # None = không giới hạn

    available = [
        btns.nth(i).get_attribute("courtlabel")
        for i in range(btns.count())
        if allowed is None or btns.nth(i).get_attribute("courtlabel") in allowed
    ]
    log.info(f"[BOT] Available courts (filtered): {available}")
    if not available:
        log.info(f"[BOT] Không còn court nào trong allowed={allowed}")
        return None

    # 1. Preferred court cho index này
    if court_index < len(preferred_courts):
        preferred = preferred_courts[court_index]
        if preferred in available:
            log.info(f"[BOT] Preferred court '{preferred}' available ✅")
            return preferred
        log.info(f"[BOT] Preferred court '{preferred}' not available, falling back...")

    # 2. Fallback: bất kỳ court nào không phải là preferred của thread khác
    reserved = set(preferred_courts)
    for court in available:
        if court not in reserved:
            log.info(f"[BOT] Fallback court (non-preferred): '{court}'")
            return court

    # 3. Last resort: court đầu tiên còn lại
    log.info(f"[BOT] Last-resort court: '{available[0]}'")
    return available[0]


def _book_now_worker(rule, target_date, court_index, results):
    """Mỗi thread mở 1 browser song song, pick court thứ N trong list available."""
    start            = rule.get("start", "")
    end              = rule.get("end", "")
    dur              = parse_duration_label(start, end)
    preferred_courts = rule.get("preferred_courts", [])
    loc_cfg          = load_location_cfg(rule["location"])
    test_mode        = loc_cfg.get("test_mode", False)
    booking_url      = loc_cfg["booking_url"]
    p, browser, context, page = open_browser(test_mode=test_mode)
    try:
        ensure_logged_in(page, context, booking_url)
        page.goto(booking_url, wait_until="domcontentloaded")
        time.sleep(3)
        navigate_to_date(page, target_date)
        btns = page.locator(f'tr[data-testid="{start}"] button[data-testid="reserveBtn"]:not(.hide)')
        if btns.count() == 0:
            log.info(f"[BOT] Browser {court_index}: no courts available.")
            results[court_index] = (None, "No slots available")
            return
        courtlabel = _pick_courtlabel(btns, court_index, preferred_courts, loc_cfg)
        if not courtlabel:
            log.info(f"[BOT] Browser {court_index}: court index {court_index} not available.")
            results[court_index] = (None, f"Court {court_index} not available")
            return
        log.info(f"[BOT] Browser {court_index}: booking court '{courtlabel}'...")
        ok = book_specific_court(page, start, courtlabel, dur, test_mode=test_mode)
        results[court_index] = (courtlabel, None) if ok else (None, f"book_specific_court failed for '{courtlabel}'")
    except Exception as e:
        log.error(f"_book_now_worker [{court_index}] error: {e}", exc_info=True)
        results[court_index] = (None, str(e))
    finally:
        pass  # Giữ browser mở để xem kết quả


def _watch_and_book_worker(rule, target_date, court_index, results):
    """Mỗi thread mở 1 browser song song, cùng watch timer rồi pick court thứ N."""
    start            = rule.get("start", "")
    end              = rule.get("end", "")
    dur              = parse_duration_label(start, end)
    preferred_courts = rule.get("preferred_courts", [])
    loc_cfg          = load_location_cfg(rule["location"])
    test_mode        = loc_cfg.get("test_mode", False)
    open_time        = loc_cfg.get("open_time", "19:00")
    booking_url      = loc_cfg["booking_url"]
    p, browser, context, page = open_browser(test_mode=test_mode)
    try:
        ensure_logged_in(page, context, booking_url)
        page.goto(booking_url, wait_until="domcontentloaded")
        time.sleep(3)
        wait_for_slots_open(page, target_date, start, open_time)
        btns = page.locator(f'tr[data-testid="{start}"] button[data-testid="reserveBtn"]:not(.hide)')
        if btns.count() == 0:
            log.info(f"[BOT] Browser {court_index}: no courts available.")
            results[court_index] = (None, "No slots available")
            return
        courtlabel = _pick_courtlabel(btns, court_index, preferred_courts, loc_cfg)
        if not courtlabel:
            log.info(f"[BOT] Browser {court_index}: court index {court_index} not available.")
            results[court_index] = (None, f"Court {court_index} not available")
            return
        log.info(f"[BOT] Browser {court_index}: booking court '{courtlabel}'...")
        ok = book_specific_court(page, start, courtlabel, dur, test_mode=test_mode)
        results[court_index] = (courtlabel, None) if ok else (None, f"book_specific_court failed for '{courtlabel}'")
    except Exception as e:
        log.error(f"_watch_and_book_worker [{court_index}] error: {e}", exc_info=True)
        results[court_index] = (None, str(e))
    finally:
        pass  # Giữ browser mở để xem kết quả


def job_book_now(rule, target_date):
    date_str  = target_date.strftime("%Y-%m-%d")
    courts    = rule.get("courts", 1)
    start     = rule.get("start", "")
    end       = rule.get("end", "")
    loc_cfg   = load_location_cfg(rule["location"])
    test_mode = loc_cfg.get("test_mode", False)
    log.info(f"=== JOB book_now | rule={rule['id']} | date={date_str} | {start}-{end} x{courts} ===")
    if not test_mode:
        upsert_record(rule["id"], date_str, BOOKING, "booking in progress")
    results = [None] * courts
    threads = [threading.Thread(target=_book_now_worker, args=(rule, target_date, i, results))
               for i in range(courts)]
    for t in threads: t.start()
    for t in threads: t.join()
    courts_list = [court for court, _ in results if court]
    reasons     = [r for _, r in results if r]
    total = len(courts_list)
    if test_mode:
        log.info(f"=== JOB book_now [TEST MODE] done — state NOT updated ===")
        return
    if total > 0:
        upsert_record(rule["id"], date_str, BOOKED, f"booked {total}/{courts}",
                      extra={"courts_booked": courts_list})
    else:
        upsert_record(rule["id"], date_str, FAILED, f"booked {total}/{courts}",
                      extra={"reason": "; ".join(reasons)})
    log.info(f"=== JOB book_now done: {total}/{courts} courts booked ===")


def job_watch_and_book(rule, target_date):
    date_str  = target_date.strftime("%Y-%m-%d")
    courts    = rule.get("courts", 1)
    start     = rule.get("start", "")
    end       = rule.get("end", "")
    loc_cfg   = load_location_cfg(rule["location"])
    test_mode = loc_cfg.get("test_mode", False)
    log.info(f"=== JOB watch_and_book | rule={rule['id']} | date={date_str} | {start}-{end} x{courts} ===")
    if not test_mode:
        upsert_record(rule["id"], date_str, BOOKING, "booking in progress")
    results = [None] * courts
    threads = [threading.Thread(target=_watch_and_book_worker, args=(rule, target_date, i, results))
               for i in range(courts)]
    for t in threads: t.start()
    for t in threads: t.join()
    courts_list = [court for court, _ in results if court]
    reasons     = [r for _, r in results if r]
    total = len(courts_list)
    if test_mode:
        log.info(f"=== JOB watch_and_book [TEST MODE] done — state NOT updated ===")
        return
    if total > 0:
        upsert_record(rule["id"], date_str, BOOKED, f"booked {total}/{courts}",
                      extra={"courts_booked": courts_list})
    else:
        upsert_record(rule["id"], date_str, FAILED, f"booked {total}/{courts}",
                      extra={"reason": "; ".join(reasons)})
    log.info(f"=== JOB watch_and_book done: {total}/{courts} courts booked ===")


# ── Event-driven scheduler ────────────────────────────────────────────────
def _cancel_rule_jobs(scheduler, rule_id, prefix):
    """Remove all pending book/watch jobs for a rule and reset WATCHING records."""
    removed = []
    for job in scheduler.get_jobs():
        if job.id.startswith(f"{prefix}_book_{rule_id}_") or \
           job.id.startswith(f"{prefix}_watch_{rule_id}_"):
            job.remove()
            removed.append(job.id)
    if removed:
        log.info(f"[SYNC] Cancelled {len(removed)} pending job(s) for '{rule_id}': {removed}")
    # Reset any WATCHING/BOOKING records so re-enabling reschedules cleanly
    records = load_history()
    changed = False
    for rec in records:
        if rec.get("id") == rule_id and rec.get("status") in (WATCHING, BOOKING):
            rec["status"] = "CANCELLED"
            rec["note"] = "Rule disabled"
            rec["updated"] = datetime.now().isoformat()
            changed = True
    if changed:
        save_history(records)
        log.info(f"[SYNC] Reset WATCHING/BOOKING records for '{rule_id}' to CANCELLED.")


def _schedule_rule(scheduler, rule, cfg, now, is_recurring, target_date):
    # Schedule the right job for one (rule, target_date) pair.
    # Returns True if a new job was added to the scheduler.
    date_str     = target_date.strftime("%Y-%m-%d")
    rule_id      = rule["id"]
    prefix       = "rec" if is_recurring else "one"
    job_id_book  = f"{prefix}_book_{rule_id}_{date_str}"
    job_id_watch = f"{prefix}_watch_{rule_id}_{date_str}"
    status       = get_status(rule_id, date_str)
    meta         = _rule_meta(rule, is_recurring)

    kind     = "Recurring" if is_recurring else "One-time"
    who      = rule.get("who", "?")
    start    = rule.get("start", "?")
    end      = rule.get("end", "?")
    courts   = rule.get("courts", 1)
    location = rule.get("location", "?")
    day_name = target_date.strftime("%A")

    if status == BOOKED:
        log.info(f"[SYNC] '{rule_id}' {date_str} -> BOOKED, skip.")
        return False

    if is_slot_open(target_date, cfg):
        if status == FAILED:
            log.info(f"[SYNC] '{rule_id}' {date_str} -> FAILED, skip.")
            return False
        if scheduler.get_job(job_id_book):
            log.info(f"[SYNC] '{rule_id}' {date_str} -> book job already queued, skip.")
            return False
        fire_dt = now + timedelta(seconds=5)
        log.info(
            f"[WATCH] {kind} | {who} @ {location} | {day_name} {date_str} | {start}-{end} x{courts} court(s)\n"
            f"        Slot already OPEN → book_now fires at {fire_dt.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        upsert_record(rule_id, date_str, WATCHING, "Scheduled book_now", extra=meta)
        scheduler.add_job(job_book_now, "date",
            run_date=fire_dt,
            args=[rule, target_date],
            id=job_id_book, replace_existing=True)
        return True

    trigger_dt = watch_trigger_dt(target_date, cfg)
    open_dt    = trigger_dt + timedelta(minutes=cfg["watch_before_minutes"])

    if trigger_dt <= now:
        if status in (FAILED, WATCHING):
            log.info(f"[SYNC] '{rule_id}' {date_str} -> {status}, skip.")
            return False
        if now < open_dt:
            if scheduler.get_job(job_id_watch):
                log.info(f"[SYNC] '{rule_id}' {date_str} -> watch job already running, skip.")
                return False
            fire_dt = now + timedelta(seconds=3)
            log.info(
                f"[WATCH] {kind} | {who} @ {location} | {day_name} {date_str} | {start}-{end} x{courts} court(s)\n"
                f"        Past trigger, before open_time → watch_and_book fires NOW at {fire_dt.strftime('%H:%M:%S')}"
                f" (open_time ~{open_dt.strftime('%H:%M')})"
            )
            upsert_record(rule_id, date_str, WATCHING, "Watch now", extra=meta)
            scheduler.add_job(job_watch_and_book, "date",
                run_date=fire_dt,
                args=[rule, target_date],
                id=job_id_watch, replace_existing=True)
        else:
            if scheduler.get_job(job_id_book):
                log.info(f"[SYNC] '{rule_id}' {date_str} -> late book job already queued, skip.")
                return False
            fire_dt = now + timedelta(seconds=5)
            log.info(
                f"[WATCH] {kind} | {who} @ {location} | {day_name} {date_str} | {start}-{end} x{courts} court(s)\n"
                f"        Missed trigger → late book_now fires at {fire_dt.strftime('%H:%M:%S')}"
            )
            upsert_record(rule_id, date_str, WATCHING, "Late book_now", extra=meta)
            scheduler.add_job(job_book_now, "date",
                run_date=fire_dt,
                args=[rule, target_date],
                id=job_id_book, replace_existing=True)
        return True

    # Future trigger — schedule watch at the right moment
    if scheduler.get_job(job_id_watch):
        log.info(f"[SYNC] '{rule_id}' {date_str} -> watch already at {trigger_dt.strftime('%H:%M')}, skip.")
        return False
    log.info(
        f"[WATCH] {kind} | {who} @ {location} | {day_name} {date_str} | {start}-{end} x{courts} court(s)\n"
        f"        watch_and_book scheduled → {trigger_dt.strftime('%Y-%m-%d %H:%M')} "
        f"(open_time {open_dt.strftime('%H:%M')})"
    )
    upsert_record(rule_id, date_str, WATCHING, f"Watch at {trigger_dt}", extra=meta)
    scheduler.add_job(job_watch_and_book, "date",
        run_date=trigger_dt,
        args=[rule, target_date],
        id=job_id_watch, replace_existing=True)
    return True


def sync_jobs_from_config(scheduler):
    # Load scheduled_bookings.json + config.json and sync APScheduler jobs.
    # Safe to call at startup *and* whenever either file is modified (idempotent).
    bookings = load_bookings()
    now      = datetime.now()
    added    = 0
    log.info("-- sync_jobs_from_config ------------------------------------------")

    # Recurring rules
    for rule in bookings.get("recurring", []):
        if not rule.get("enabled", False):
            log.info(f"[SYNC] Recurring '{rule['id']}' disabled, skip.")
            _cancel_rule_jobs(scheduler, rule["id"], "rec")
            continue
        try:
            loc_cfg = load_location_cfg(rule["location"])
        except KeyError as e:
            log.warning(f"[SYNC] Recurring '{rule['id']}': {e}, skip.")
            continue
        # day is now a single string, wrap it for get_upcoming_dates
        dates = get_upcoming_dates([rule["day"]], weeks=2)
        start_from = None
        if rule.get("startRecurring"):
            try:
                start_from = datetime.strptime(rule["startRecurring"], "%Y-%m-%d").date()
            except Exception:
                log.warning(f"[SYNC] Recurring '{rule['id']}': invalid startRecurring '{rule['startRecurring']}', ignored.")
        log.info(f"[SYNC] Recurring '{rule['id']}' ({rule['day']} @ {rule['location']}) -- {len(dates)} upcoming dates" +
                 (f", active from {start_from}" if start_from else "") + ".")
        for target_date in dates:
            if (target_date - now.date()).days > 14:
                continue
            if start_from and target_date < start_from:
                log.info(f"[SYNC] Recurring '{rule['id']}' {target_date} -> before startRecurring {start_from}, skip.")
                continue
            if _schedule_rule(scheduler, rule, loc_cfg, now,
                               is_recurring=True, target_date=target_date):
                added += 1

    # One-time rules
    for rule in bookings.get("one_time", []):
        if not rule.get("enabled", False):
            _cancel_rule_jobs(scheduler, rule["id"], "one")
            continue
        try:
            loc_cfg = load_location_cfg(rule["location"])
        except KeyError as e:
            log.warning(f"[SYNC] One-time '{rule['id']}': {e}, skip.")
            continue
        try:
            target_date = datetime.strptime(rule["date"], "%Y-%m-%d").date()
        except Exception:
            log.warning(f"[SYNC] Invalid date for one-time rule '{rule['id']}'")
            continue
        days_away = (target_date - now.date()).days
        if days_away < 0:
            log.info(f"[SYNC] One-time '{rule['id']}' -> past date, skip.")
            continue
        if days_away > 14:
            log.info(f"[SYNC] One-time '{rule['id']}' -> {days_away}d away, too far.")
            continue
        log.info(f"[SYNC] One-time '{rule['id']}' ({rule['location']}) -> {target_date}.")
        if _schedule_rule(scheduler, rule, loc_cfg, now,
                           is_recurring=False, target_date=target_date):
            added += 1

    log.info(f"-- sync done: {added} new job(s) added ----------------------------")


# ── Config file watcher ───────────────────────────────────────────────────
class ConfigWatcher(FileSystemEventHandler):
    # Watches the pickleball/ directory and re-syncs APScheduler whenever
    # config.json OR scheduled_bookings.json is saved.

    _WATCHED = None  # set in __init__ after paths are known

    def __init__(self, scheduler):
        super().__init__()
        self._scheduler = scheduler
        self._last_sync = 0.0   # epoch-s, used for 1-second debounce
        self._WATCHED   = {
            os.path.abspath(LOCATION_CONFIG_FILE),
            os.path.abspath(BOOKINGS_FILE),
        }

    def on_modified(self, event):
        if os.path.abspath(event.src_path) not in self._WATCHED:
            return
        now_ts = time.time()
        if now_ts - self._last_sync < 1.0:   # debounce rapid saves
            return
        self._last_sync = now_ts
        changed = os.path.basename(event.src_path)
        log.info(f"[WATCH] {changed} changed -> re-syncing jobs...")
        try:
            sync_jobs_from_config(self._scheduler)
        except Exception as e:
            log.error(f"[WATCH] sync_jobs_from_config error: {e}")


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    log.info("=== Court Booking Bot starting (event-driven) ===")
    log.info(f"Locations : {LOCATION_CONFIG_FILE}")
    log.info(f"Bookings  : {BOOKINGS_FILE}")
    log.info(f"History   : {COURTS_BOOKED_FILE}")

    scheduler = BackgroundScheduler(timezone="America/Vancouver")
    sync_jobs_from_config(scheduler)   # schedule everything known right now

    # Daily re-sync at 08:00 — picks up new dates entering the 14-day window
    scheduler.add_job(sync_jobs_from_config, "cron", hour=8, minute=0,
                      args=[scheduler], id="daily_sync", replace_existing=True)

    scheduler.start()

    observer = Observer()
    observer.schedule(ConfigWatcher(scheduler), DIR, recursive=False)
    observer.start()

    log.info("Watching config.json + scheduled_bookings.json for changes.  Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping...")
        observer.stop()
        scheduler.shutdown(wait=False)
        log.info("Bot stopped.")


if __name__ == "__main__":
    main()
