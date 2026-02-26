"""
Court Booking Bot - Recurring scheduler
Config: pickleball/config.json
State:  pickleball/booking_state.json
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
DIR          = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(DIR, "config.json")
STATE_FILE   = os.path.join(DIR, "booking_state.json")
SESSION_FILE = os.path.join(DIR, "session.json")

LOGIN_URL    = "https://app.courtreserve.com/Online/Account/LogIn/15504"
BOOKING_URL  = "https://app.courtreserve.com/Online/Reservations/Bookings/15504?sId=21420"
#BOOKING_URL  = "https://app.courtreserve.com/Online/Reservations/Bookings/16646"

PENDING  = "PENDING"
BOOKED   = "BOOKED"
FULL     = "FULL"
WATCHING = "WATCHING"

DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("BookBot")


# ── Config & State ─────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def update_state(rule_id, date_str, status, note=""):
    state = load_state()
    key = f"{rule_id}_{date_str}"
    state[key] = {"status": status, "note": note, "updated": datetime.now().isoformat()}
    save_state(state)
    log.info(f"[STATE] {key} -> {status}  {note}")

def get_status(rule_id, date_str):
    state = load_state()
    entry = state.get(f"{rule_id}_{date_str}")
    return entry["status"] if entry else None


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
    open_dt = open_datetime_for(target_date, cfg["open_time"], cfg.get("open_days_before", 14))
    return datetime.now() >= open_dt

def watch_trigger_dt(target_date, cfg):
    open_dt = open_datetime_for(target_date, cfg["open_time"], cfg.get("open_days_before", 14))
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

def is_session_valid(page):
    log.info("[LOGIN] Checking session...")
    page.goto(BOOKING_URL, wait_until="domcontentloaded")
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

def open_browser():
    p = sync_playwright().start()
    _slow = 200 if load_config().get("test_mode", False) else 0
    browser = p.chromium.launch(headless=False, slow_mo=_slow)
    context = browser.new_context(storage_state=SESSION_FILE) if os.path.exists(SESSION_FILE) \
              else browser.new_context()
    page = context.new_page()
    page.on("console", lambda msg: log.info(f"[BROWSER] {msg.text}") if "[BOT]" in msg.text else None)
    return p, browser, context, page

def ensure_logged_in(page, context):
    if not (os.path.exists(SESSION_FILE) and is_session_valid(page)):
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


def book_slot(page, time_slot, courts=1, duration_label=None):
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
        if load_config().get("test_mode", False):
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


def book_specific_court(page, time_slot, courtlabel, duration_label=None):
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
    if load_config().get("test_mode", False):
        log.info("[TEST_MODE] Dừng sau khi chọn duration — KHÔNG submit, giữ browser mở.")
        return 0
    try:
        page.locator('#modal1 button[type="submit"], #modal1 .btn-primary').first.click()
        page.wait_for_selector('#modal1.show', state='hidden', timeout=10000)
        time.sleep(1)
    except Exception:
        pass
    log.info(f"[BOT] Court '{courtlabel}' BOOKED!")
    return 1


# ── Jobs ───────────────────────────────────────────────────────────────────
def _pick_courtlabel(btns, court_index, preferred_courts):
    """
    Chọn court theo thứ tự ưu tiên preferred_courts[court_index],
    fallback sang court available theo thứ tự nếu preferred không có.
    Bỏ qua các court trong excluded_courts (từ config).
    """
    excluded = load_config().get("excluded_courts", [])
    available = [
        btns.nth(i).get_attribute("courtlabel")
        for i in range(btns.count())
        if btns.nth(i).get_attribute("courtlabel") not in excluded
    ]
    if not available:
        log.info(f"[BOT] Không còn court nào sau khi loại excluded: {excluded}")
        return None
    if preferred_courts and court_index < len(preferred_courts):
        preferred = preferred_courts[court_index]
        if preferred in available:
            log.info(f"[BOT] Preferred court '{preferred}' available ✅")
            return preferred
        log.info(f"[BOT] Preferred court '{preferred}' not available, falling back...")
    # Fallback: lấy court thứ court_index trong list available (sau khi loại excluded)
    if court_index < len(available):
        lab = available[court_index]
        log.info(f"[BOT] Fallback court: '{lab}'")
        return lab
    return None


def _book_now_worker(rule, target_date, court_index, results):
    """Mỗi thread mở 1 browser song song, pick court thứ N trong list available."""
    start = rule.get("start", "")
    end   = rule.get("end", "")
    dur   = parse_duration_label(start, end)
    preferred_courts = rule.get("preferred_courts", [])
    p, browser, context, page = open_browser()
    try:
        ensure_logged_in(page, context)
        page.goto(BOOKING_URL, wait_until="domcontentloaded")
        time.sleep(3)
        navigate_to_date(page, target_date)
        btns = page.locator(f'tr[data-testid="{start}"] button[data-testid="reserveBtn"]:not(.hide)')
        if btns.count() == 0:
            log.info(f"[BOT] Browser {court_index}: no courts available.")
            results[court_index] = 0
            return
        courtlabel = _pick_courtlabel(btns, court_index, preferred_courts)
        if not courtlabel:
            log.info(f"[BOT] Browser {court_index}: court index {court_index} not available.")
            results[court_index] = 0
            return
        log.info(f"[BOT] Browser {court_index}: booking court '{courtlabel}'...")
        ok = book_specific_court(page, start, courtlabel, dur)
        results[court_index] = ok
    except Exception as e:
        log.error(f"_book_now_worker [{court_index}] error: {e}", exc_info=True)
        results[court_index] = 0
    finally:
        pass  # Giữ browser mở để xem kết quả


def _watch_and_book_worker(rule, target_date, court_index, results):
    """Mỗi thread mở 1 browser song song, cùng watch timer rồi pick court thứ N."""
    start     = rule.get("start", "")
    end       = rule.get("end", "")
    dur       = parse_duration_label(start, end)
    open_time = load_config().get("open_time", "19:00")
    preferred_courts = rule.get("preferred_courts", [])
    p, browser, context, page = open_browser()
    try:
        ensure_logged_in(page, context)
        page.goto(BOOKING_URL, wait_until="domcontentloaded")
        time.sleep(3)
        wait_for_slots_open(page, target_date, start, open_time)
        btns = page.locator(f'tr[data-testid="{start}"] button[data-testid="reserveBtn"]:not(.hide)')
        if btns.count() == 0:
            log.info(f"[BOT] Browser {court_index}: no courts available.")
            results[court_index] = 0
            return
        courtlabel = _pick_courtlabel(btns, court_index, preferred_courts)
        if not courtlabel:
            log.info(f"[BOT] Browser {court_index}: court index {court_index} not available.")
            results[court_index] = 0
            return
        log.info(f"[BOT] Browser {court_index}: booking court '{courtlabel}'...")
        ok = book_specific_court(page, start, courtlabel, dur)
        results[court_index] = ok
    except Exception as e:
        log.error(f"_watch_and_book_worker [{court_index}] error: {e}", exc_info=True)
        results[court_index] = 0
    finally:
        pass  # Giữ browser mở để xem kết quả


def job_book_now(rule, target_date):
    date_str = target_date.strftime("%Y-%m-%d")
    courts   = rule.get("courts", 1)
    start    = rule.get("start", "")
    end      = rule.get("end", "")
    log.info(f"=== JOB book_now | rule={rule['id']} | date={date_str} | {start}-{end} x{courts} ===")
    results = [0] * courts
    threads = [threading.Thread(target=_book_now_worker, args=(rule, target_date, i, results))
               for i in range(courts)]
    for t in threads: t.start()
    for t in threads: t.join()
    total  = sum(results)
    status = BOOKED if total > 0 else FULL
    update_state(rule["id"], date_str, status, f"booked {total}/{courts}")
    log.info(f"=== JOB book_now done: {total}/{courts} courts booked ===")


def job_watch_and_book(rule, target_date):
    date_str = target_date.strftime("%Y-%m-%d")
    courts   = rule.get("courts", 1)
    start    = rule.get("start", "")
    end      = rule.get("end", "")
    log.info(f"=== JOB watch_and_book | rule={rule['id']} | date={date_str} | {start}-{end} x{courts} ===")
    results = [0] * courts
    threads = [threading.Thread(target=_watch_and_book_worker, args=(rule, target_date, i, results))
               for i in range(courts)]
    for t in threads: t.start()
    for t in threads: t.join()
    total  = sum(results)
    status = BOOKED if total > 0 else FULL
    update_state(rule["id"], date_str, status, f"booked {total}/{courts}")
    log.info(f"=== JOB watch_and_book done: {total}/{courts} courts booked ===")


# ── Event-driven scheduler ────────────────────────────────────────────────
def _schedule_rule(scheduler, rule, cfg, now, is_recurring, target_date):
    # Schedule the right job for one (rule, target_date) pair.
    # Returns True if a new job was added to the scheduler.
    date_str     = target_date.strftime("%Y-%m-%d")
    rule_id      = rule["id"]
    prefix       = "rec" if is_recurring else "one"
    job_id_book  = f"{prefix}_book_{rule_id}_{date_str}"
    job_id_watch = f"{prefix}_watch_{rule_id}_{date_str}"
    status       = get_status(rule_id, date_str)

    if status == BOOKED:
        log.info(f"[SYNC] '{rule_id}' {date_str} -> BOOKED, skip.")
        return False

    if is_slot_open(target_date, cfg):
        if status == FULL:
            log.info(f"[SYNC] '{rule_id}' {date_str} -> FULL, skip.")
            return False
        if scheduler.get_job(job_id_book):
            log.info(f"[SYNC] '{rule_id}' {date_str} -> book job already queued, skip.")
            return False
        log.info(f"[SYNC] '{rule_id}' {date_str} -> slot open -> book_now in 5s.")
        update_state(rule_id, date_str, WATCHING, "Scheduled book_now")
        scheduler.add_job(job_book_now, "date",
            run_date=now + timedelta(seconds=5),
            args=[rule, target_date],
            id=job_id_book, replace_existing=True)
        return True

    trigger_dt = watch_trigger_dt(target_date, cfg)

    if trigger_dt <= now:
        if status in (FULL, WATCHING):
            log.info(f"[SYNC] '{rule_id}' {date_str} -> {status}, skip.")
            return False
        open_dt = trigger_dt + timedelta(minutes=cfg["watch_before_minutes"])
        if now < open_dt:
            if scheduler.get_job(job_id_watch):
                log.info(f"[SYNC] '{rule_id}' {date_str} -> watch job already running, skip.")
                return False
            log.info(f"[SYNC] '{rule_id}' {date_str} -> past trigger, before open_time -> watch now.")
            update_state(rule_id, date_str, WATCHING, "Watch now")
            scheduler.add_job(job_watch_and_book, "date",
                run_date=now + timedelta(seconds=3),
                args=[rule, target_date],
                id=job_id_watch, replace_existing=True)
        else:
            if scheduler.get_job(job_id_book):
                log.info(f"[SYNC] '{rule_id}' {date_str} -> late book job already queued, skip.")
                return False
            log.info(f"[SYNC] '{rule_id}' {date_str} -> missed trigger -> late book_now in 5s.")
            update_state(rule_id, date_str, WATCHING, "Late book_now")
            scheduler.add_job(job_book_now, "date",
                run_date=now + timedelta(seconds=5),
                args=[rule, target_date],
                id=job_id_book, replace_existing=True)
        return True

    # Future trigger — schedule watch at the right moment
    if scheduler.get_job(job_id_watch):
        log.info(f"[SYNC] '{rule_id}' {date_str} -> watch already at {trigger_dt.strftime('%H:%M')}, skip.")
        return False
    log.info(f"[SYNC] '{rule_id}' {date_str} -> watch at {trigger_dt.strftime('%Y-%m-%d %H:%M')}.")
    update_state(rule_id, date_str, WATCHING, f"Watch at {trigger_dt}")
    scheduler.add_job(job_watch_and_book, "date",
        run_date=trigger_dt,
        args=[rule, target_date],
        id=job_id_watch, replace_existing=True)
    return True


def sync_jobs_from_config(scheduler):
    # Load config.json and ensure APScheduler has the right jobs scheduled.
    # Safe to call at startup *and* whenever config.json is modified (idempotent).
    cfg = load_config()
    now = datetime.now()
    added = 0
    log.info("-- sync_jobs_from_config ------------------------------------------")

    # Recurring rules
    for rule in cfg.get("recurring", []):
        if not rule.get("enabled", False):
            log.info(f"[SYNC] Recurring '{rule['id']}' disabled, skip.")
            continue
        dates = get_upcoming_dates(rule["days"], weeks=2)
        log.info(f"[SYNC] Recurring '{rule['id']}' ({rule['days']}) -- {len(dates)} upcoming dates.")
        for target_date in dates:
            if (target_date - now.date()).days > 14:
                continue
            if _schedule_rule(scheduler, rule, cfg, now,
                               is_recurring=True, target_date=target_date):
                added += 1

    # One-time rules
    for rule in cfg.get("one_time", []):
        if not rule.get("enabled", False):
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
        log.info(f"[SYNC] One-time '{rule['id']}' -> {target_date}.")
        if _schedule_rule(scheduler, rule, cfg, now,
                           is_recurring=False, target_date=target_date):
            added += 1

    log.info(f"-- sync done: {added} new job(s) added ----------------------------")


# ── Config file watcher ───────────────────────────────────────────────────
class ConfigWatcher(FileSystemEventHandler):
    # Watches the pickleball/ directory and re-syncs APScheduler whenever
    # config.json is saved.

    def __init__(self, scheduler):
        super().__init__()
        self._scheduler = scheduler
        self._last_sync = 0.0   # epoch-s, used for 1-second debounce

    def on_modified(self, event):
        if os.path.abspath(event.src_path) != os.path.abspath(CONFIG_FILE):
            return
        now_ts = time.time()
        if now_ts - self._last_sync < 1.0:   # debounce rapid saves
            return
        self._last_sync = now_ts
        log.info("[WATCH] config.json changed -> re-syncing jobs...")
        try:
            sync_jobs_from_config(self._scheduler)
        except Exception as e:
            log.error(f"[WATCH] sync_jobs_from_config error: {e}")


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    log.info("=== Court Booking Bot starting (event-driven) ===")
    log.info(f"Config : {CONFIG_FILE}")
    log.info(f"State  : {STATE_FILE}")

    scheduler = BackgroundScheduler(timezone="America/Vancouver")
    sync_jobs_from_config(scheduler)   # schedule everything known right now
    scheduler.start()

    observer = Observer()
    observer.schedule(ConfigWatcher(scheduler), DIR, recursive=False)
    observer.start()

    log.info("Watching config.json for changes.  Press Ctrl+C to stop.")
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
