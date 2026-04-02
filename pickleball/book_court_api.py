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
from zoneinfo import ZoneInfo
import json
import os
import time
import logging
import logging.handlers
import threading

TZ = ZoneInfo("America/Vancouver")

def _now():
    """Return current time in Vancouver timezone (naive for APScheduler compat)."""
    return datetime.now(TZ).replace(tzinfo=None)

# ── Paths ──────────────────────────────────────────────────────────────────
DIR                  = os.path.dirname(os.path.abspath(__file__))
LOCATION_CONFIG_FILE = os.path.join(DIR, "config.json")
BOOKINGS_FILE        = os.path.join(DIR, "scheduled_bookings.json")
COURTS_BOOKED_FILE   = os.path.join(DIR, "courts_booked.json")
SESSIONS_DIR         = DIR  # session files stored alongside config

BOOKED   = "BOOKED"
FAILED   = "FAILED"
WATCHING = "WATCHING"
BOOKING  = "BOOKING"

DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

# ── Logging ────────────────────────────────────────────────────────────────
def _setup_logging():
    try:
        with open(LOCATION_CONFIG_FILE, "r") as f:
            _cfg = json.load(f)
        debug_mode = _cfg.get("debug_mode", True)
    except Exception:
        debug_mode = True

    logger = logging.getLogger("BookBot")
    logger.setLevel(logging.DEBUG if debug_mode else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    # Formatter that converts timestamps to Vancouver time
    class VancouverFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            dt = datetime.fromtimestamp(record.created, tz=TZ)
            return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

    if debug_mode:
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(VancouverFormatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
    else:
        log_file = os.path.join(DIR, "logs", "bookbot.log")
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setLevel(logging.INFO)
        handler.setFormatter(VancouverFormatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%m-%d %H:%M"
        ))

    logger.addHandler(handler)
    # Ensure APScheduler exceptions are visible
    aps_logger = logging.getLogger("apscheduler")
    aps_logger.setLevel(logging.WARNING)
    aps_logger.addHandler(handler)
    return logger

log = _setup_logging()


# ── Config & State ─────────────────────────────────────────────────────────
def load_global_cfg():
    """Load top-level (shared) fields from config.json."""
    with open(LOCATION_CONFIG_FILE, "r") as f:
        return json.load(f)

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

def upsert_record(rule_id, date_str, start_str, status, note="", extra=None):
    records = load_history()
    idx = next((i for i, r in enumerate(records)
                if r.get("id") == rule_id and r.get("date") == date_str
                and r.get("start") == start_str), None)
    day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
    now_iso = _now().isoformat(timespec="seconds")
    rec = records[idx] if idx is not None else {
        "id": rule_id, "date": date_str, "day": day,
        "start": start_str, "created_at": now_iso,
    }
    rec.update({"day": day, "status": status, "note": note, "updated": now_iso, "updated_at": now_iso})
    if extra:
        rec.update(extra)
    if idx is not None:
        records[idx] = rec
    else:
        records.append(rec)
    save_history(records)
    log.info(f"[HISTORY] {rule_id} {date_str} {start_str} -> {status}  {note}")


def remove_one_time_scheduled(rule_id: str):
    """Xóa một one-time rule khỏi scheduled_bookings.json sau khi đã được book (hoặc thất bại)."""
    try:
        with open(BOOKINGS_FILE, "r") as f:
            data = json.load(f)
        original_len = len(data.get("one_time", []))
        data["one_time"] = [r for r in data.get("one_time", []) if r.get("id") != rule_id]
        if len(data["one_time"]) < original_len:
            with open(BOOKINGS_FILE, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            log.info(f"[CLEANUP] Removed one-time rule '{rule_id}' from scheduled_bookings.json")
    except Exception as e:
        log.error(f"[CLEANUP] Failed to remove one-time rule '{rule_id}': {e}")

def _rule_meta(rule, is_recurring):
    """Extract rule metadata for history records."""
    return {
        "type": "Recurring" if is_recurring else "One-time",
        "start": rule.get("start", ""),
        "duration": rule.get("duration", ""),
        "location": rule.get("location", ""),
        "who": rule.get("who", ""),
        "courts_requested": rule.get("courts", 1),
    }

def get_status(rule_id, date_str, start_str):
    records = load_history()
    rec = next((r for r in records
                if r.get("id") == rule_id and r.get("date") == date_str
                and r.get("start") == start_str), None)
    return rec["status"] if rec else None


# ── Date helpers ───────────────────────────────────────────────────────────
def get_upcoming_dates(days, weeks=2):
    today = _now().date()
    result = []
    for i in range(1, weeks * 7 + 1):  # bắt đầu từ ngày mai
        d = today + timedelta(days=i)
        if DAY_NAMES[d.weekday()] in days:
            result.append(d)
    return result

def _open_times(cfg):
    """Normalize open_time to list (supports both str and list in config)."""
    raw = cfg.get("open_time", "20:00")
    return raw if isinstance(raw, list) else [raw]

def open_datetime_for(target_date, open_time="19:00", days_before=14):
    h, m = map(int, open_time.split(":"))
    open_date = target_date - timedelta(days=days_before)
    return datetime(open_date.year, open_date.month, open_date.day, h, m)

def is_slot_open(target_date, cfg):
    days_before = cfg.get("open_days_before", 14)
    return any(_now() >= open_datetime_for(target_date, ot, days_before) for ot in _open_times(cfg))


# ── Playwright helpers ─────────────────────────────────────────────────────
def session_file(loc_cfg):
    """Return path to session file for this location, e.g. toan_session.json"""
    name = loc_cfg.get("loginInfo", {}).get("name", "default_session")
    return os.path.join(SESSIONS_DIR, f"{name}.json")

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
    resp = page.goto(booking_url, wait_until="domcontentloaded", timeout=30000)
    if resp and resp.status == 403:
        log.warning("[LOGIN] Got 403 (Cloudflare block).")
        return False
    if any(x in page.url for x in ["LogIn", "Login", "login"]):
        log.info("[LOGIN] Session expired.")
        return False
    log.info("[LOGIN] Session valid.")
    return True

def do_login(page, context, loc_cfg):
    info      = loc_cfg.get("loginInfo", {})
    login_url = info.get("login_url", "")
    username  = info.get("username", "")
    password  = info.get("password", "")
    sess_file = session_file(loc_cfg)
    log.info(f"[LOGIN] Logging in as {username}...")
    page.goto(login_url, wait_until="domcontentloaded")
    for selector, value in [('input[name="email"]', username),
                             ('input[name="password"]', password)]:
        el = page.locator(selector)
        el.wait_for(state="visible", timeout=10000)
        el.click()
        el.click(click_count=3)
        fill_react_input(page, selector, value)
    page.locator('button[data-testid="Continue"]').click()
    try:
        page.wait_for_url(lambda u: "LogIn" not in u and "Login" not in u, timeout=15000)
    except Exception:
        page.wait_for_timeout(1000)
        if "LogIn" in page.url or "Login" in page.url:
            raise Exception("Login failed!")
    context.storage_state(path=sess_file)
    log.info(f"[LOGIN] Success, session saved to {sess_file}.")

def open_browser(loc_cfg, test_mode=False):
    sess_file = session_file(loc_cfg)
    p = sync_playwright().start()
    headless = load_global_cfg().get("headless_mode", False)
    browser = p.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
    )
    ctx_opts = {
        "viewport": {"width": 1280, "height": 900},
        "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/144.0.0.0 Safari/537.36"),
    }
    if os.path.exists(sess_file):
        ctx_opts["storage_state"] = sess_file
    context = browser.new_context(**ctx_opts)
    page = context.new_page()
    # Ẩn webdriver flag
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page.on("console", lambda msg: log.info(f"[BROWSER] {msg.text}") if "[BOT]" in msg.text else None)
    return p, browser, context, page

def ensure_logged_in(page, context, booking_url, loc_cfg):
    sess_file = session_file(loc_cfg)
    if not (os.path.exists(sess_file) and is_session_valid(page, booking_url)):
        do_login(page, context, loc_cfg)
        # After login, navigate to booking_url (login redirects to home, not booking page)
        page.goto(booking_url, wait_until="domcontentloaded", timeout=30000)
    # If session valid, is_session_valid already loaded booking_url — no extra goto needed

def _find_frame_with(page, selector, timeout=20000):
    """Tìm frame (main hoặc iframe) chứa selector, trả về frame object."""
    import time as _time
    deadline = _time.time() + timeout / 1000
    while _time.time() < deadline:
        for frame in page.frames:
            try:
                if frame.locator(selector).count() > 0:
                    return frame
            except Exception:
                pass
        _time.sleep(0.3)
    raise Exception(f"Element '{selector}' not found in any frame after {timeout}ms")

def navigate_to_date(page, target_date, booking_url=None):
    """Set cookie InternalCalendarDate — gọi trước khi load trang."""
    import urllib.parse as _urlparse
    date_str   = f"{target_date.month}/{target_date.day}/{target_date.year}"
    cookie_val = _urlparse.quote(date_str, safe="")
    log.info(f"[BOT] Setting InternalCalendarDate={date_str} (before page load)")
    page.context.add_cookies([{
        "name":   "InternalCalendarDate",
        "value":  cookie_val,
        "domain": "app.courtreserve.com",
        "path":   "/",
    }])


def verify_calendar_date(page, target_date):
    """Check calendar trên page đang hiển thị đúng target_date.
    Returns True nếu đúng, False nếu website reset về ngày khác."""
    displayed = page.evaluate("""
        () => {
            const el = document.querySelector('.k-lg-date-format');
            return el ? el.textContent.trim() : '';
        }
    """)
    expected_str = target_date.strftime("%A, %B %-d, %Y")
    if not displayed:
        log.warning("[BOT] Cannot read calendar date from page")
        return True  # không đọc được thì cứ tiếp tục
    if displayed == expected_str:
        log.info(f"[BOT] ✅ Calendar date verified: {displayed}")
        return True
    log.warning(f"[BOT] ❌ Calendar date mismatch! Expected: {expected_str}, Got: {displayed}")
    return False


def _duration_label(rule):
    """Convert duration field (e.g. '2') to booking label e.g. '2 hours'."""
    d = str(rule.get("duration", "1")).strip()
    try:
        h = int(d)
        return "1 hour" if h == 1 else f"{h} hours"
    except Exception:
        return "1 hour"


def _wait_duration_listbox(page):
    """Wait for Duration listbox to be populated and stable (no re-bind in progress)."""
    page.wait_for_function(
        """() => {
            const check = () => document.querySelectorAll('#Duration_listbox li.k-list-item').length > 0;
            if (!check()) return false;
            return new Promise(resolve => setTimeout(() => resolve(check()), 100));
        }""",
        timeout=10000
    )

def _click_duration(page, preferred_label=None):
    """Open dropdown, wait for animation, click the matching li."""
    # Step 1: click DDL to show dropdown (Playwright click = real browser events)
    page.locator('span[aria-owns="Duration_listbox"]').click()
    # Step 2: wait for Kendo dropdown fully open (k-state-border-down = animation complete)
    page.wait_for_function(
        "() => document.querySelector('span[aria-owns=\"Duration_listbox\"]')?.classList.contains('k-state-border-down')",
        timeout=3000
    )
    # Step 3: find + click correct li
    selected = page.evaluate("""
        (preferred) => {
            const items = document.querySelectorAll('#Duration_listbox li.k-list-item');
            const itemTexts = Array.from(items).map(li => {
                const s = li.querySelector('span.k-list-item-text');
                return s ? s.textContent.trim() : li.textContent.trim();
            });
            for (const li of items) {
                const text = li.querySelector('span.k-list-item-text');
                if (text && text.textContent.trim() === preferred) {
                    li.click();
                    return {selected: preferred, items: itemTexts, hit: true};
                }
            }
            return {selected: null, items: itemTexts, hit: false};
        }
    """, preferred_label)
    if not selected['hit']:
        raise Exception(f"[BOT] Duration '{preferred_label}' not found in listbox. Available: {selected['items']}")
    log.info(f"[BOT] Selected duration: {selected['selected']} (available: {selected['items']})")
    return selected['selected']

def select_duration(page, preferred_label=None):
    """Wait for Duration listbox then click preferred duration."""
    _wait_duration_listbox(page)
    return _click_duration(page, preferred_label)

def _parse_ajax_available_courts(ajax_data, target_date, start_time_str, allowed_courts):
    """Parse member-expanded AJAX response to find available courts at target time.
    Returns list of CourtLabel strings that are bookable.

    Key insight: entries in the AJAX response represent OCCUPIED slots (events,
    member reservations shown as green #80d059 with ReservationId=0, etc.).
    A court is AVAILABLE when it has NO overlapping entry at the target time.
    """
    # Parse start_time_str (e.g., "8:00 PM") to hour/minute
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            t = datetime.strptime(start_time_str, fmt)
            break
        except ValueError:
            continue
    else:
        log.warning(f"[AJAX] Cannot parse start time: {start_time_str}")
        return []

    # Build target datetime in UTC for comparison with AJAX Start/End
    local_dt = datetime(target_date.year, target_date.month, target_date.day,
                        t.hour, t.minute, tzinfo=TZ)
    target_utc = local_dt.astimezone(ZoneInfo("UTC"))

    # Collect courts that are OCCUPIED at target time
    occupied = set()
    for item in ajax_data.get("Data", []):
        if item.get("IsWaitListSlot"):
            continue
        court_label = item.get("CourtLabel", "")
        if not any(ac in court_label for ac in allowed_courts):
            continue
        start_str = item.get("Start", "")
        end_str = item.get("End", "")
        try:
            slot_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            slot_end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if slot_start <= target_utc < slot_end:
            occupied.add(court_label)

    # Available = allowed courts that have NO overlapping entry
    available = [c for c in allowed_courts if c not in occupied]
    log.info(f"[AJAX] Available at {start_time_str}: {available}")
    return available


def _wait_for_reserve_btn(page, start_slot, allowed, courts_needed):
    """Wait until courts_needed allowed courts are visible for start_slot, then proceed.
    Timeout 3s → return False (not enough slots). Returns True when ready.
    """
    result = page.evaluate("""
        ({ selector, allowedList, needed, timeout }) => new Promise(resolve => {
            function countAllowed() {
                const btns = document.querySelectorAll(selector);
                if (!allowedList || allowedList.length === 0) return btns.length;
                let count = 0;
                for (const b of btns) {
                    const label = b.getAttribute('courtlabel') || '';
                    if (allowedList.some(a => label.includes(a))) count++;
                }
                return count;
            }

            if (countAllowed() >= needed) { resolve(true); return; }

            const timer = setTimeout(() => { obs.disconnect(); resolve(false); }, timeout);
            const container = document.querySelector('#CourtsScheduler') || document.body;
            const obs = new MutationObserver(() => {
                if (countAllowed() >= needed) {
                    clearTimeout(timer); obs.disconnect(); resolve(true);
                }
            });
            obs.observe(container, { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
        })
    """, {"selector": f'tr[data-testid="{start_slot}"] button[data-testid="reserveBtn"]:not(.hide)',
          "allowedList": allowed or [],
          "needed": courts_needed,
          "timeout": 3000})
    if result:
        log.info(f"[BOT] ✅ Reserve button sẵn sàng!")
    else:
        log.warning(f"[BOT] Timeout: chỉ tìm thấy < {courts_needed} allowed courts cho slot {start_slot}")
    return result


def _trigger_and_scan(page, trigger_fn, target_date, start_slot, allowed, courts_needed, verify_date=False):
    """Intercept member-expanded AJAX during trigger_fn(), parse available courts.
    Injects client-side XHR hook to capture response body (avoids Playwright CDP GC issue).
    Sets page._ajax_available_courts on success.
    Falls back to DOM scan if AJAX intercept fails.
    """
    _xhr_hook = """
        window.__capturedAjax = null;
        (function() {
            const origSend = XMLHttpRequest.prototype.send;
            const origOpen = XMLHttpRequest.prototype.open;
            XMLHttpRequest.prototype.open = function(method, url) {
                this.__url = url;
                return origOpen.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function() {
                if (this.__url && this.__url.includes('member-expanded')) {
                    this.addEventListener('load', function() {
                        try { window.__capturedAjax = JSON.parse(this.responseText); }
                        catch(e) {}
                    });
                }
                return origSend.apply(this, arguments);
            };
        })();
    """
    try:
        # Inject on CURRENT page (for non-navigation triggers like click HERE link)
        page.evaluate(_xhr_hook)
        # Also inject for FUTURE navigations (for page.goto triggers)
        page.add_init_script(_xhr_hook)

        trigger_fn()

        # Wait for the captured data to appear (max 15s)
        ajax_data = page.evaluate("""
            () => new Promise((resolve, reject) => {
                let elapsed = 0;
                const iv = setInterval(() => {
                    if (window.__capturedAjax) {
                        clearInterval(iv);
                        resolve(window.__capturedAjax);
                    }
                    elapsed += 100;
                    if (elapsed > 15000) {
                        clearInterval(iv);
                        reject(new Error('Timeout waiting for member-expanded AJAX'));
                    }
                }, 100);
            })
        """)

        if verify_date and not verify_calendar_date(page, target_date):
            log.warning("[BOT] Calendar date mismatch after reload")
            return False

        available = _parse_ajax_available_courts(ajax_data, target_date, start_slot, allowed)
        page._ajax_available_courts = available
        page._ajax_raw_data = ajax_data

        # Extract court ID map từ DOM header (data-courtid)
        try:
            court_id_map = page.evaluate("""
                () => {
                    const map = {};
                    document.querySelectorAll('span[data-courtid].header-title-name').forEach(el => {
                        const name = (el.querySelector('.bold-org-color') || el).textContent.trim();
                        const cid = el.getAttribute('data-courtid');
                        if (name && cid) map[name] = cid;
                    });
                    return map;
                }
            """)
            if court_id_map:
                page._court_id_map = court_id_map
                log.info(f"[BOT] Court ID map from DOM: {court_id_map}")
        except Exception as e:
            log.warning(f"[BOT] Could not extract court ID map from DOM: {e}")

        log.info(f"[BOT] AJAX intercept: {len(available)} courts available: {available}")
        if len(available) == 0:
            log.warning(f"[BOT] AJAX: no courts available, need {courts_needed}")
            return False
        if len(available) < courts_needed:
            log.info(f"[BOT] AJAX: only {len(available)} courts, need {courts_needed} — proceeding with partial")
        return True
    except Exception as e:
        log.warning(f"[BOT] AJAX intercept failed ({e}) — no courts available")
        return False


def wait_for_slots_open(page, target_date, start_slot, open_time_str, loc_cfg, context=None, is_last=True, courts_needed=1):
    """
    Dual-mode wait:
    - Có #ReservationOpenTimeDispplay: MutationObserver chờ HERE link → click
    - Không có: sleep đến open_time → navigate_to_date → reload (retry 3 lần)
    Cả 2 flow đều intercept AJAX member-expanded → parse available courts.
    """
    booking_url = loc_cfg["booking_url"]
    ensure_logged_in(page, context, booking_url, loc_cfg)
    allowed = loc_cfg.get("courts", [])

    has_countdown = page.evaluate(
        "() => !!document.getElementById('ReservationOpenTimeDispplay')"
    )

    if has_countdown:
        log.info("[BOT] Có countdown timer, chờ HERE link...")
        page.evaluate("""
            () => new Promise(resolve => {
                const el = document.getElementById('ReservationOpenTimeDispplay');
                if (!el) { resolve(); return; }
                if (el.querySelector('.here-link-text a')) { resolve(); return; }
                const obs = new MutationObserver(() => {
                    if (el.querySelector('.here-link-text a')) {
                        obs.disconnect(); resolve();
                    }
                });
                obs.observe(el, { childList: true, subtree: true });
            })
        """)
        log.info("[BOT] ✅ HERE link xuất hiện — intercepting AJAX + clicking...")
        trigger_fn = lambda: page.locator('#ReservationOpenTimeDispplay .here-link-text a').first.click()
        return _trigger_and_scan(page, trigger_fn, target_date, start_slot, allowed, courts_needed)
    else:
        h, m = map(int, open_time_str.split(":"))
        now = _now()
        open_dt = datetime(now.year, now.month, now.day, h, m)
        wait_secs = (open_dt - now).total_seconds()
        if wait_secs > 0:
            log.info(f"[BOT] Không có countdown, sleeping {wait_secs:.0f}s until {open_time_str}...")
            time.sleep(wait_secs)
        days_before = loc_cfg.get("open_days_before", 14)
        days_until = (target_date - _now().date()).days
        need_verify = days_until >= days_before  # chỉ verify khi book đúng ngày mở slot
        for attempt in range(1, 4):
            log.info(f"[BOT] Reload attempt {attempt}/3...")
            navigate_to_date(page, target_date)
            trigger_fn = lambda: page.goto(booking_url, wait_until="domcontentloaded", timeout=30000)
            result = _trigger_and_scan(page, trigger_fn, target_date, start_slot, allowed, courts_needed)
            if need_verify and not verify_calendar_date(page, target_date):
                log.warning(f"[BOT] Slots not open yet (attempt {attempt}), retrying in 3s...")
                if attempt < 3:
                    time.sleep(3)
                continue
            if result:
                return True
            log.warning(f"[BOT] Attempt {attempt}/3: not enough allowed courts, {'retrying...' if attempt < 3 else 'giving up.'}")
            if attempt < 3:
                time.sleep(3)
        if is_last:
            log.warning(f"[BOT] Failed after 3 attempts for open_time {open_time_str} (last in array). Booking failed.")
        else:
            log.warning(f"[BOT] Failed after 3 attempts for open_time {open_time_str}. Will try next open_time.")
        return False


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
    # Snapshot atomic bằng JS — tránh race condition
    labels = page.evaluate(f"""
        () => Array.from(document.querySelectorAll(
            'tr[data-testid="{time_slot}"] button[data-testid="reserveBtn"]'
        )).filter(b => !b.classList.contains('hide'))
          .map(b => b.getAttribute('courtlabel'))
    """)
    log.info(f"[BOT] Available courts at '{time_slot}': {labels}")
    return labels


def _ensure_court_selected(page, loc_cfg, courtlabel, available_courts, claimed=None, btag=""):
    """After duration change, verify court still available using GetAvailableCourtsMemberPortal response.
    - available_courts: JSON response from GetAvailableCourtsMemberPortal
    - courtlabel: the court originally selected
    - claimed: set of courts already claimed by other browsers (skip these when replacing)
    - btag: browser tag prefix for logging e.g. '[B0] '
    Returns True if court is confirmed/replaced, False if no replacement available.
    """
    allowed_courts = (loc_cfg or {}).get("courts", [])
    claimed = claimed or set()

    if not available_courts:
        log.warning(f"{btag}No AJAX response data — cannot verify court")
        return False

    # Filter: not conflicted + in allowed list
    available_names = [
        c["Name"] for c in available_courts
        if not c.get("IsConflicted", True)
        and (not allowed_courts or any(a in c["Name"] or c["Name"] in a for a in allowed_courts))
    ]
    log.info(f"{btag}Available courts (allowed + not conflicted): {available_names}")

    # Court ban đầu còn available → không cần làm gì
    if courtlabel and any(courtlabel in name or name in courtlabel for name in available_names):
        log.info(f"{btag}Court '{courtlabel}' still available ✅")
        return True

    # Court ban đầu mất → chọn court thay thế (skip claimed courts)
    replacement = None
    for c in available_courts:
        if c.get("IsConflicted", True):
            continue
        if not (not allowed_courts or any(a in c["Name"] or c["Name"] in a for a in allowed_courts)):
            continue
        if courtlabel and (courtlabel in c["Name"] or c["Name"] in courtlabel):
            continue
        if any(cl in c["Name"] or c["Name"] in cl for cl in claimed):
            continue
        replacement = c
        break
    if not replacement:
        log.warning(f"{btag}Court '{courtlabel}' not available and no replacement found (claimed={claimed})")
        return False
    target_id = replacement["Id"]
    target_name = replacement["DisplayName"]
    log.info(f"{btag}Court '{courtlabel}' not available — replacing with '{target_name}' (Id={target_id})")

    # Wait for CourtIds listbox to be populated (Kendo MultiSelect rebind after AJAX)
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('#CourtIds_listbox li.k-list-item').length > 0",
            timeout=5000
        )
    except Exception:
        log.warning("[BOT] CourtIds listbox items never populated")
        return False

    # Select court thay thế bằng Kendo MultiSelect API
    result = page.evaluate(f"""() => {{
        const ms = $("#CourtIds").data("kendoMultiSelect");
        if (!ms) return false;
        ms.value([{target_id}]);
        ms.trigger("change");
        return true;
    }}""")
    if not result:
        log.warning("[BOT] Kendo MultiSelect not found on #CourtIds")
        return False
    log.info(f"[BOT] Court replaced to '{target_name}' ✅")
    return True


def book_specific_court(page, time_slot, courtlabel, duration_label=None, test_mode=False, loc_cfg=None, courts_total=1, payment_done=None, payment_lock=None, claimed=None, btag="", court_index=0, booking_events=None, cart_counts=None):
    """Book đúng 1 court theo courtlabel — API submit (không qua UI form)."""
    import re as _re
    from urllib.parse import quote
    _api_log_path = os.path.join(os.path.dirname(__file__), "api_booking_log.jsonl")

    def _log_api(action, data):
        entry = {"timestamp": _now().isoformat(), "court": courtlabel, "action": action, **data}
        with open(_api_log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    _booking_signaled = [False]
    def _signal_done():
        if not _booking_signaled[0] and booking_events and court_index < len(booking_events):
            booking_events[court_index].set()
            _booking_signaled[0] = True

    log.info(f"{btag}[STEP 1/3] Click Reserve button for '{courtlabel}' at '{time_slot}'...")

    # ── Intercept form GET response ──
    _form_html = [None]
    def _capture_form(response):
        try:
            if 'ReservationsApi/CreateReservation' not in response.url:
                return
            if response.request.method != "GET":
                return
            body = response.text()
            if len(body) > 500:
                _form_html[0] = body
        except Exception:
            pass
    page.on("response", _capture_form)

    use_fast_click = getattr(page, '_ajax_available_courts', None) is not None
    if use_fast_click:
        log.info(f"{btag}Fast-click mode: injecting setInterval for '{courtlabel}'...")
        try:
            with page.expect_response(lambda r: 'GetDurationDropdown' in r.url and r.status == 200, timeout=15000):
                with page.expect_response(lambda r: 'GetDurationDropdown' in r.url and r.status == 200, timeout=15000):
                    page.evaluate("""
                        ({timeSlot, courtlabel}) => {
                            window.__bookClickCount = 0;
                            const origOpen = XMLHttpRequest.prototype.open;
                            XMLHttpRequest.prototype.open = function(method, url) {
                                if (url.includes('CreateReservationCourtsView')) {
                                    clearInterval(window.__bookInterval);
                                    window.__bookInterval = null;
                                    XMLHttpRequest.prototype.open = origOpen;
                                }
                                return origOpen.apply(this, arguments);
                            };
                            window.__bookInterval = setInterval(() => {
                                const row = document.querySelector('tr[data-testid="' + timeSlot + '"');
                                if (!row) return;
                                const btn = row.querySelector('button[data-testid="reserveBtn"][courtlabel="' + courtlabel + '"]');
                                if (btn && !btn.classList.contains('hide')) {
                                    window.__bookClickCount++;
                                    btn.click();
                                }
                            }, 10);
                            setTimeout(() => {
                                if (window.__bookInterval) {
                                    clearInterval(window.__bookInterval);
                                    window.__bookInterval = null;
                                }
                            }, 15000);
                        }
                    """, {"timeSlot": time_slot, "courtlabel": courtlabel})
            log.info(f"{btag}[STEP 1/3] ✅ Reserve clicked for '{courtlabel}'")
        except Exception as e:
            page.evaluate("() => { if (window.__bookInterval) { clearInterval(window.__bookInterval); window.__bookInterval = null; } }")
            log.warning(f"{btag}Fast-click failed for '{courtlabel}': {e}")
            _signal_done(); return 0
    else:
        try:
            page.wait_for_selector(f'tr[data-testid="{time_slot}"]', timeout=10000)
        except Exception:
            log.warning(f"{btag}Row '{time_slot}' not found.")
            _signal_done(); return 0
        btn = page.locator(
            f'tr[data-testid="{time_slot}"] button[data-testid="reserveBtn"][courtlabel="{courtlabel}"]:not(.hide)'
        ).first
        if btn.count() == 0:
            log.warning(f"{btag}Court '{courtlabel}' not available.")
            _signal_done(); return 0
        log.info(f"{btag}[STEP 1/3] Clicking Reserve for '{courtlabel}'...")
        with page.expect_response(lambda r: 'GetDurationDropdown' in r.url and r.status == 200, timeout=10000):
            with page.expect_response(lambda r: 'GetDurationDropdown' in r.url and r.status == 200, timeout=10000):
                btn.click()

    # Chờ form HTML được capture
    for _ in range(50):  # max 5s
        if _form_html[0]:
            break
        time.sleep(0.1)
    page.remove_listener("response", _capture_form)

    if not _form_html[0]:
        log.warning(f"{btag}Form HTML not captured — aborting.")
        _signal_done(); return 0
    # ── STEP 2/3: Extract form fields + set CourtIds & Duration ──────────
    form_html = _form_html[0]

    # Parse all input fields from form HTML
    fields = {}
    # hidden + text inputs: name="X" value="Y" (any order of attributes)
    for m in _re.finditer(r'<input[^>]*?name="([^"]+)"[^>]*?value="([^"]*)"', form_html):
        fields[m.group(1)] = m.group(2)
    for m in _re.finditer(r'<input[^>]*?value="([^"]*)"[^>]*?name="([^"]+)"', form_html):
        fields[m.group(2)] = m.group(1)

    if not fields.get("__RequestVerificationToken"):
        log.warning(f"{btag}__RequestVerificationToken not found in form — aborting.")
        _log_api("error", {"reason": "no_token", "fields_found": list(fields.keys())})
        _signal_done(); return 0

    # CourtIds: lấy từ DOM court ID map (data-courtid trong header table)
    court_id = None
    court_id_map = getattr(page, '_court_id_map', {}) or {}
    if court_id_map:
        court_id = court_id_map.get(courtlabel)
    if not court_id:
        log.warning(f"{btag}CourtId not found for '{courtlabel}' in DOM court ID map — aborting.")
        _signal_done(); return 0

    # Duration: trực tiếp từ rule (số giờ → phút)
    duration_minutes = 60
    if duration_label:
        try:
            duration_minutes = int(float(duration_label)) * 60
        except (ValueError, TypeError):
            duration_minutes = 60

    fields["CourtIds"] = court_id
    fields["Duration"] = str(duration_minutes)
    fields["SelectedCourtType"] = courtlabel
    fields["DisclosureAgree"] = "true"

    log.info(f"{btag}[STEP 2/3] ✅ Form fields ready: CourtIds={court_id}, Duration={duration_minutes}, Token={fields['__RequestVerificationToken'][:20]}...")
    _log_api("form_fields", {"court_id": court_id, "duration": duration_minutes, "fields_count": len(fields)})

    if test_mode:
        log.info(f"{btag}[TEST_MODE] Dừng trước API submit — giữ browser mở.")
        _log_api("test_stopped", {"fields": fields})
        return "test_stopped"

    # ── STEP 3/3: POST API CreateReservation ─────────────────────────────
    org_id = fields.get("OrgId", fields.get("Id", ""))
    api_url = f"https://reservations.courtreserve.com//Online/ReservationsApi/CreateReservation/{org_id}?uiCulture=en-CA"

    # Build form body (URL-encoded)
    body_parts = []
    for k, v in fields.items():
        body_parts.append(f"{quote(k, safe='')}={quote(str(v), safe='')}")
    form_body = "&".join(body_parts)

    log.info(f"{btag}[STEP 3/3] Submitting API: {api_url}")

    enable_payment = load_global_cfg().get("enable_payment", True)

    # Dùng Playwright request API (bypass CORS, share cookies với browser)
    try:
        pw_resp = page.request.post(api_url, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
            "Referer": "https://app.courtreserve.com/",
        }, data=form_body)
        resp_body = None
        try:
            resp_body = pw_resp.json()
        except Exception:
            try:
                resp_body = pw_resp.text()
            except Exception:
                resp_body = "(could not read body)"
        api_result = {
            "ok": pw_resp.ok,
            "status": pw_resp.status,
            "url": pw_resp.url,
            "body": resp_body,
        }
    except Exception as e:
        api_result = {"ok": False, "status": 0, "error": str(e)}

    status = api_result.get("status", 0)
    log.info(f"{btag}[STEP 3/3] API response: status={status}, ok={api_result.get('ok')}")

    _log_api("api_response", {
        "url": api_url,
        "status": status,
        "ok": api_result.get("ok"),
        "response_url": api_result.get("url"),
        "body": api_result.get("body"),
        "request_fields": {k: v[:50] if len(str(v)) > 50 else v for k, v in fields.items()},
    })

    if api_result.get("error"):
        log.warning(f"{btag}API fetch error: {api_result['error']}")
        _signal_done(); return 0

    # Kiểm tra kết quả
    resp_body = api_result.get("body", "")
    if isinstance(resp_body, dict):
        if resp_body.get("isValid") is False or resp_body.get("success") is False:
            log.warning(f"{btag}API returned invalid: {resp_body}")
            _signal_done(); return 0
    elif isinstance(resp_body, str) and "Reservation Notice" in resp_body:
        log.warning(f"{btag}API returned 'Reservation Notice' error")
        _signal_done(); return 0

    if status == 200 and api_result.get("ok"):
        log.info(f"{btag}[STEP 3/3] ✅ Court '{courtlabel}' reservation created via API!")

        # Lấy payment URL từ response JSON
        payment_path = ""
        if isinstance(resp_body, dict) and resp_body.get("isRedirect") and resp_body.get("url"):
            payment_path = resp_body["url"]  # e.g. "/Online/Payments/ProcessPayment/16646?..."

        if not enable_payment:
            log.info(f"{btag}enable_payment=false — reservation created, navigating to payment page (no submit).")

        # Navigate browser tới payment page
        if payment_path:
            payment_url = f"https://app.courtreserve.com{payment_path}"
            log.info(f"{btag}Navigating to payment: {payment_url}")
            page.goto(payment_url, wait_until="domcontentloaded", timeout=15000)
        else:
            log.info(f"{btag}No payment redirect in response — checking current page...")

        # SweetAlert2 popup: server có thể đổi court (ví dụ book #3 → assign #4) → click OK
        try:
            swal_btn = page.locator('button.swal2-confirm[data-testid="toast-success"]')
            swal_btn.wait_for(state='visible', timeout=3000)
            swal_msg = page.locator('#swal2-html-container').text_content(timeout=2000) or ""
            log.info(f"{btag}SweetAlert2 popup: {swal_msg.strip()} — clicking OK")
            swal_btn.click()
            try:
                page.wait_for_selector('.swal2-container', state='hidden', timeout=3000)
            except Exception:
                pass
        except Exception:
            pass  # Không có popup → tiếp tục bình thường

        # Tìm PayButton
        try:
            page.wait_for_selector('#PayButton', state='visible', timeout=10000)
            amount = ""
            try:
                amount = page.locator('[data-testid="total-value"]').first.text_content(timeout=0).strip()
                log.info(f"{btag}Amount due: {amount}")
            except Exception:
                pass

            cart_rows = page.evaluate("""
                () => document.querySelectorAll(
                    '#kendo-table-grid tbody[data-testid="table-grid-body"] tr'
                ).length
            """)
            log.info(f"{btag}Cart has {cart_rows} row(s)")

            # ── Payment gate ─────────────────────────────────────────────
            if booking_events and cart_counts is not None and payment_lock is not None:
                # Ghi cart_rows vào shared dict, sau đó signal
                with payment_lock:
                    cart_counts[court_index] = cart_rows
                _signal_done()  # tao đọc cart xong
                log.info(f"{btag}Waiting for all {len(booking_events)} threads to finish booking...")
                for ev in booking_events:
                    ev.wait(timeout=30)
                time.sleep(0.3)  # đợi browser khác kịp ghi cart_counts
                with payment_lock:
                    snap = dict(cart_counts)
                max_rows = max(snap.values()) if snap else 0
                payer_index = min(idx for idx, rows in snap.items() if rows == max_rows)
                log.info(f"{btag}Cart counts: {snap}, payer=B{payer_index}, max_rows={max_rows}")
                if court_index != payer_index:
                    log.info(f"{btag}Not the payer (payer=B{payer_index}) — delegating.")
                    return "delegated"
                if max_rows == 0:
                    log.info(f"{btag}All carts empty — no payment needed.")
                    return "delegated"
            else:
                # Fallback: single-thread
                _signal_done()

            if not enable_payment:
                log.info(f"{btag}enable_payment=false — dừng tại payment. Amount: {amount}")
                return amount

            log.info(f"{btag}Clicking Pay button...")
            page.locator('#PayButton').click()
            try:
                page.wait_for_selector('#PayButton', state='hidden', timeout=8000)
            except Exception:
                pass
            log.info(f"{btag}✅ Court '{courtlabel}' BOOKED + PAID! Amount: {amount}")
            _log_api("booked", {"amount": amount})
            return amount or "paid"
        except Exception as e:
            log.info(f"{btag}PayButton not found ({e}) — treating as BOOKED.")
            _log_api("booked_no_payment", {})
            _signal_done(); return "paid"
    else:
        log.warning(f"{btag}API submit failed: status={status}")
        _signal_done(); return 0


# ── Jobs ───────────────────────────────────────────────────────────────────
def _court_matches(courtlabel, court_name):
    """True if courtlabel (from button attr) contains court_name (from config).
    e.g. 'Pickleball - Court #4' matches 'Court #4'.
    """
    return court_name in courtlabel


def _find_preferred(unique, preferred, claimed):
    """Return the entry in unique that matches preferred (contains), not yet claimed."""
    for c in unique:
        if _court_matches(c, preferred) and c not in claimed:
            return c
    return None


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
        if allowed is None or any(_court_matches(btns.nth(i).get_attribute("courtlabel"), a) for a in allowed)
    ]
    log.info(f"[BOT] Available courts (filtered): {available}")
    if not available:
        log.info(f"[BOT] Không còn court nào trong allowed={allowed}")
        return None

    # 1. Preferred court cho index này
    if court_index < len(preferred_courts):
        preferred = preferred_courts[court_index]
        match = next((c for c in available if _court_matches(c, preferred)), None)
        if match:
            log.info(f"[BOT] Preferred court '{preferred}' → '{match}' available ✅")
            return match
        log.info(f"[BOT] Preferred court '{preferred}' not available, falling back...")

    # 2. Fallback: bất kỳ court nào không phải là preferred của thread khác
    reserved_names = set(preferred_courts)
    for court in available:
        if not any(_court_matches(court, r) for r in reserved_names):
            log.info(f"[BOT] Fallback court (non-preferred): '{court}'")
            return court

    # 3. Last resort: court đầu tiên còn lại
    log.info(f"[BOT] Last-resort court: '{available[0]}'")
    return available[0]


def _close_browser(browser, p):
    try:
        browser.close()
        p.stop()
    except Exception:
        pass


# ── Pre-scan functions (Phase-1 differs between book_now and watch) ────────
def _pre_scan_book_now(page, context, target_date, start, loc_cfg):
    """Login, set date, reload with AJAX intercept."""
    booking_url  = loc_cfg["booking_url"]
    days_before  = loc_cfg.get("open_days_before", 14)
    days_until   = (target_date - _now().date()).days
    allowed      = loc_cfg.get("courts", [])
    ensure_logged_in(page, context, booking_url, loc_cfg)
    navigate_to_date(page, target_date, booking_url=booking_url)
    verify = days_until >= days_before
    trigger_fn = lambda: page.goto(booking_url, wait_until="domcontentloaded", timeout=30000)
    return _trigger_and_scan(page, trigger_fn, target_date, start, allowed, 1, verify_date=verify)


def _pre_scan_watch(open_time, is_last=True, courts_needed=1):
    """Returns a pre-scan fn that waits for slots to open before scanning."""
    def fn(page, context, target_date, start, loc_cfg):
        return wait_for_slots_open(page, target_date, start, open_time, loc_cfg,
                                   context=context, is_last=is_last, courts_needed=courts_needed)
    return fn


# ── Shared worker core ─────────────────────────────────────────────────────
def _worker(rule, target_date, court_index, results, courts_total, lock, claimed,
            scan_results, barrier, payment_done, pre_scan_fn, booking_events=None, cart_counts=None):
    """Phase-1: pre_scan_fn prepares page. Phase-2: assign court + book."""
    start            = rule.get("start", "")
    dur              = str(rule.get("duration", "1"))
    preferred_courts = rule.get("preferred_courts", [])
    loc_cfg          = load_location_cfg(rule["location"])
    test_mode        = loc_cfg.get("test_mode", False)
    allowed          = loc_cfg.get("courts", None)
    p, browser, context, page = open_browser(loc_cfg, test_mode=test_mode)
    try:
        if not pre_scan_fn(page, context, target_date, start, loc_cfg):
            log.info(f"[BOT] Browser {court_index}: pre-scan failed (no courts available or date not open), closing browser.")
            results[court_index] = (None, "Slots not open after 3 reload attempts", "")
            if booking_events and court_index < len(booking_events):
                booking_events[court_index].set()
            _close_browser(browser, p)
            try:
                barrier.abort()
            except Exception:
                pass
            return
        # ── Get available courts (set by pre_scan_fn) ──
        ajax_courts = getattr(page, '_ajax_available_courts', None)
        if ajax_courts is not None:
            available = ajax_courts
            log.info(f"[BOT] Browser {court_index}: available courts: {available}")
        else:
            log.warning(f"[BOT] Browser {court_index}: _ajax_available_courts not set — this should not happen")
            available = []
        with lock:
            scan_results[court_index] = available
        _scan_ts = time.time()

        # ── Phase-2: wait for all threads then decide ──────────────────────
        log.info(f"[BOT] Browser {court_index}: entering barrier.wait()...")
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            results[court_index] = (None, "Barrier broken — another thread failed", "")
            if booking_events and court_index < len(booking_events):
                booking_events[court_index].set()
            return
        _barrier_ts = time.time()
        log.info(f"[BOT] Browser {court_index}: barrier passed, barrier_ts={_barrier_ts:.3f}, gap={(_barrier_ts - _scan_ts)*1000:.0f}ms")

        with lock:
            seen, unique = set(), []
            for avail in scan_results.values():
                for c in avail:
                    if c not in seen:
                        seen.add(c)
                        unique.append(c)
            if len(unique) == 0:
                results[court_index] = (None, "No slots available", "")
                if booking_events and court_index < len(booking_events):
                    booking_events[court_index].set()
                return
            if len(unique) < courts_total:
                log.info(f"[BOT] Browser {court_index}: only {len(unique)} of {courts_total} courts available — proceeding with partial booking")
            courtlabel = None
            if court_index < len(preferred_courts):
                courtlabel = _find_preferred(unique, preferred_courts[court_index], claimed)
            if not courtlabel:
                reserved_names = set(preferred_courts)
                for c in unique:
                    if c not in claimed and not any(_court_matches(c, r) for r in reserved_names):
                        courtlabel = c
                        break
            if not courtlabel:
                for c in unique:
                    if c not in claimed:
                        courtlabel = c
                        break
            if not courtlabel:
                results[court_index] = (None, "No unclaimed court left after assignment", "")
                if booking_events and court_index < len(booking_events):
                    booking_events[court_index].set()
                return
            claimed.add(courtlabel)

        _assign_ts = time.time()
        log.info(f"[BOT] Browser {court_index}: court assigned='{courtlabel}', assign_ts={_assign_ts:.3f}, total_gap={(_assign_ts - _scan_ts)*1000:.0f}ms")

        log.info(f"[BOT] Browser {court_index}: booking court '{courtlabel}'...")
        _btag = f"[B{court_index}] "
        ok = book_specific_court(page, start, courtlabel, dur, test_mode=test_mode, loc_cfg=loc_cfg,
                                 courts_total=courts_total, payment_done=payment_done, payment_lock=lock,
                                 claimed=claimed, btag=_btag,
                                 court_index=court_index, booking_events=booking_events, cart_counts=cart_counts)
        results[court_index] = (courtlabel, None, ok if isinstance(ok, str) else "") if ok != 0 \
                               else (None, f"Court '{courtlabel}' not available", "")
        if ok == "delegated":
            log.info(f"[BOT] Browser {court_index}: delegated — closing browser.")
            _close_browser(browser, p)
    except Exception as e:
        log.error(f"_worker [{court_index}] error: {e}", exc_info=True)
        results[court_index] = (None, f"TECHNICAL_ERROR: {e}", "")
        if booking_events and court_index < len(booking_events):
            booking_events[court_index].set()
        try:
            barrier.abort()
        except Exception:
            pass
    finally:
        if load_global_cfg().get("close_after_book", False) and not loc_cfg.get("test_mode", False):
            _close_browser(browser, p)


def _book_now_worker(rule, target_date, court_index, results, courts_total, lock, claimed,
                     scan_results, barrier, payment_done, booking_events=None, cart_counts=None):
    _worker(rule, target_date, court_index, results, courts_total, lock, claimed,
            scan_results, barrier, payment_done, _pre_scan_book_now,
            booking_events=booking_events, cart_counts=cart_counts)


def _watch_and_book_worker(rule, target_date, court_index, results, courts_total, lock, claimed,
                           scan_results, barrier, open_time, payment_done, is_last=True,
                           booking_events=None, cart_counts=None):
    _worker(rule, target_date, court_index, results, courts_total, lock, claimed,
            scan_results, barrier, payment_done, _pre_scan_watch(open_time, is_last, courts_needed=courts_total),
            booking_events=booking_events, cart_counts=cart_counts)


def _job_impl(rule, target_date, open_time, is_last, worker_fn, job_name):
    """Shared implementation for book_now and watch_and_book jobs."""
    date_str  = target_date.strftime("%Y-%m-%d")
    courts    = rule.get("courts", 1)
    start     = rule.get("start", "")
    duration  = rule.get("duration", "")
    loc_cfg   = load_location_cfg(rule["location"])
    test_mode = loc_cfg.get("test_mode", False)
    if open_time is None:
        open_time = _open_times(loc_cfg)[0]
    log.info(f"=== JOB {job_name} | rule={rule['id']} | date={date_str} | {start} x{duration}h x{courts} | T={open_time} ===")
    if not test_mode and get_status(rule["id"], date_str, start) == BOOKED:
        log.info(f"[{job_name}] {rule['id']} {date_str} already BOOKED, skip.")
        return
    is_recurring = "date" not in rule
    meta = _rule_meta(rule, is_recurring)
    if not test_mode:
        upsert_record(rule["id"], date_str, start, BOOKING, f"booking in progress T={open_time}", extra=meta)
    results        = [None] * courts
    lock           = threading.Lock()
    claimed        = set()
    scan_results   = {}
    barrier        = threading.Barrier(courts)
    payment_done   = [False]
    booking_events = [threading.Event() for _ in range(courts)]
    cart_counts    = {}
    threads = [threading.Thread(target=worker_fn,
                args=(rule, target_date, i, results, courts, lock, claimed, scan_results, barrier, payment_done, booking_events, cart_counts))
               for i in range(courts)]
    for t in threads: t.start()
    for t in threads: t.join()
    courts_list = [court for court, _, _ in results if court]
    reasons     = list(dict.fromkeys(r for _, r, _ in results if r))
    amounts     = [amt  for _, _, amt in results if amt and amt != "delegated"]
    total = len(courts_list)
    if test_mode:
        log.info(f"=== JOB {job_name} [TEST MODE] done — state NOT updated ===")
        return
    # Technical errors (exceptions, Ctrl+C, etc.) — do NOT touch booking data
    if total == 0 and any(r and r.startswith("TECHNICAL_ERROR") for r in reasons):
        log.error(f"=== JOB {job_name} aborted due to technical error — booking data unchanged ===")
        return
    if total >= courts:
        upsert_record(rule["id"], date_str, start, BOOKED, f"booked {total}/{courts}",
                      extra={**meta, "courts_booked": courts_list, "amount_paid": ", ".join(amounts)})
        if "date" in rule:
            remove_one_time_scheduled(rule["id"])
    elif total > 0:
        upsert_record(rule["id"], date_str, start, BOOKED, f"booked {total}/{courts}",
                      extra={**meta, "courts_booked": courts_list, "amount_paid": ", ".join(amounts)})
        log.info(f"=== JOB {job_name} partial: {total}/{courts} courts booked ===")
        if "date" in rule:
            remove_one_time_scheduled(rule["id"])
    elif is_last:
        reason_str = f"No courts available (need {courts})"
        upsert_record(rule["id"], date_str, start, FAILED, reason_str,
                      extra={**meta, "courts_booked": []})
        log.error(f"=== JOB {job_name} FAILED: {reason_str} ===")
        if "date" in rule:
            remove_one_time_scheduled(rule["id"])
    else:
        log.info(f"=== JOB {job_name} T={open_time} no courts — next open_time slot will retry ===")
    log.info(f"=== JOB {job_name} done: {total}/{courts} courts booked ===")


def job_book_now(rule, target_date, open_time=None, is_last=True):
    try:
        _job_impl(rule, target_date, open_time, is_last, _book_now_worker, "book_now")
    except Exception as e:
        import traceback
        print(f"[JOB_CRASH] job_book_now crashed: {e}", flush=True)
        traceback.print_exc()
        log.error(f"[JOB_CRASH] job_book_now crashed: {e}", exc_info=True)


def job_watch_and_book(rule, target_date, open_time=None, is_last=True):
    try:
        _ot = open_time or _open_times(load_location_cfg(rule["location"]))[0]
        def worker_fn(rule, target_date, court_index, results, courts_total, lock, claimed,
                      scan_results, barrier, payment_done, booking_events=None, cart_counts=None):
            _watch_and_book_worker(rule, target_date, court_index, results, courts_total, lock,
                                   claimed, scan_results, barrier, _ot, payment_done,
                                   is_last=is_last, booking_events=booking_events, cart_counts=cart_counts)
        _job_impl(rule, target_date, open_time, is_last, worker_fn, "watch_and_book")
    except Exception as e:
        import traceback
        print(f"[JOB_CRASH] job_watch_and_book crashed: {e}", flush=True)
        traceback.print_exc()
        log.error(f"[JOB_CRASH] job_watch_and_book crashed: {e}", exc_info=True)


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
            rec["updated"] = _now().isoformat(timespec="seconds")
            rec["updated_at"] = rec["updated"]
            changed = True
    if changed:
        save_history(records)
        log.info(f"[SYNC] Reset WATCHING/BOOKING records for '{rule_id}' to CANCELLED.")


def _schedule_rule(scheduler, rule, cfg, now, is_recurring, target_date):
    # Schedule jobs for all open_times for one (rule, target_date) pair.
    # Returns True if at least one new job was added.
    date_str     = target_date.strftime("%Y-%m-%d")
    rule_id      = rule["id"]
    prefix       = "rec" if is_recurring else "one"
    kind         = "Recurring" if is_recurring else "One-time"
    who          = rule.get("who", "?")
    start        = rule.get("start", "?")
    duration     = rule.get("duration", "?")
    courts       = rule.get("courts", 1)
    location     = rule.get("location", "?")
    day_name     = target_date.strftime("%A")
    days_before  = cfg.get("open_days_before", 14)
    watch_before = cfg.get("watch_before_minutes", 1)
    status       = get_status(rule_id, date_str, start)
    meta         = _rule_meta(rule, is_recurring)
    open_times   = _open_times(cfg)
    n            = len(open_times)

    if status == BOOKED:
        log.info(f"[SYNC] '{rule_id}' {date_str} -> BOOKED, skip.")
        return False
    # Only skip FAILED if ALL open_times have already passed
    if status == FAILED:
        all_passed = all(now >= open_datetime_for(target_date, ot, days_before) for ot in _open_times(cfg))
        if all_passed:
            log.info(f"[SYNC] '{rule_id}' {date_str} -> FAILED and all open_times past, skip.")
            return False

    # Nếu target_date đã nằm trong window (days_until < days_before) → slot đã mở, book ngay
    days_until = (target_date - now.date()).days
    if days_until < days_before:
        fire_dt = now + timedelta(seconds=1)
        job_id_immed = f"{prefix}_book_{rule_id}_{date_str}_immed"
        label_immed = f"{kind} | {who} @ {location} | {day_name} {date_str} | {start} x{duration}h x{courts} court(s)"
        if scheduler.get_job(job_id_immed):
            log.info(f"[SYNC] '{rule_id}' {date_str} -> in-window, already scheduled.")
            return True
        log.info(f"[BOOK] {label_immed}\n        Target in booking window ({days_until}d < {days_before}d) → book_now at {fire_dt.strftime('%H:%M:%S')}")
        upsert_record(rule_id, date_str, start, WATCHING, f"In-window book_now ({days_until}d < {days_before}d)", extra=meta)
        scheduler.add_job(job_book_now, "date", run_date=fire_dt,
            args=[rule, target_date, open_times[-1], True],
            id=job_id_immed, replace_existing=True, misfire_grace_time=60)
        return True

    added = 0
    has_pending = False  # track nếu có job nào đang pending (đã scheduled hoặc mới add)
    for idx, open_time in enumerate(open_times):
        is_last     = (idx == n - 1)
        open_dt     = open_datetime_for(target_date, open_time, days_before)
        trigger_dt  = open_dt - timedelta(minutes=watch_before)
        ot_key      = open_time.replace(":", "")
        job_id_book = f"{prefix}_book_{rule_id}_{date_str}_{ot_key}"
        job_id_watch= f"{prefix}_watch_{rule_id}_{date_str}_{ot_key}"
        label = f"{kind} | {who} @ {location} | {day_name} {date_str} | {start} x{duration}h x{courts} court(s) | T={open_time}"

        if scheduler.get_job(job_id_book) or scheduler.get_job(job_id_watch):
            log.info(f"[SYNC] '{rule_id}' {date_str} T={open_time} -> already scheduled, skip.")
            has_pending = True
            continue

        if now >= open_dt:
            # Window đã qua — nếu còn open_time tiếp theo thì để nó xử lý
            log.info(f"[SYNC] '{rule_id}' {date_str} T={open_time} -> past open_time, skip.")
            continue
        elif now >= trigger_dt:
            fire_dt = now + timedelta(seconds=3)
            log.info(f"[WATCH] {label}\n        Past trigger → watch_and_book fires at {fire_dt.strftime('%H:%M:%S')} (open {open_dt.strftime('%H:%M')})")
            upsert_record(rule_id, date_str, start, WATCHING, f"Watch now T={open_time}", extra=meta)
            scheduler.add_job(job_watch_and_book, "date", run_date=fire_dt,
                args=[rule, target_date, open_time, is_last],
                id=job_id_watch, replace_existing=True, misfire_grace_time=60)
        else:
            log.info(f"[WATCH] {label}\n        watch_and_book → {trigger_dt.strftime('%Y-%m-%d %H:%M')} (open {open_dt.strftime('%H:%M')})")
            upsert_record(rule_id, date_str, start, WATCHING, f"Watch at {trigger_dt} T={open_time}", extra=meta)
            scheduler.add_job(job_watch_and_book, "date", run_date=trigger_dt,
                args=[rule, target_date, open_time, is_last],
                id=job_id_watch, replace_existing=True, misfire_grace_time=60)
        added += 1

    # All open_times past but still in booking window → book now
    if added == 0 and not has_pending and days_until <= days_before:
        fire_dt = now + timedelta(seconds=1)
        job_id_immed = f"{prefix}_book_{rule_id}_{date_str}_immed"
        label_immed = f"{kind} | {who} @ {location} | {day_name} {date_str} | {start} x{duration}h x{courts} court(s)"
        if scheduler.get_job(job_id_immed):
            log.info(f"[SYNC] '{rule_id}' {date_str} -> all open_times past, already scheduled.")
            return True
        log.info(f"[BOOK] {label_immed}\n        All open_times past, still in window ({days_until}d <= {days_before}d) → book_now at {fire_dt.strftime('%H:%M:%S')}")
        upsert_record(rule_id, date_str, start, WATCHING, f"All open_times past, book_now ({days_until}d <= {days_before}d)", extra=meta)
        scheduler.add_job(job_book_now, "date", run_date=fire_dt,
            args=[rule, target_date, open_times[-1], True],
            id=job_id_immed, replace_existing=True, misfire_grace_time=60)
        return True

    return added > 0


def cleanup_old_records(days=30):
    """Remove records whose date is older than `days` days."""
    records  = load_history()
    cutoff   = (_now() - timedelta(days=days)).date()
    before   = len(records)
    records  = [r for r in records
                if datetime.strptime(r["date"], "%Y-%m-%d").date() >= cutoff]
    removed  = before - len(records)
    if removed:
        save_history(records)
        log.info(f"[CLEANUP] Removed {removed} record(s) older than {days} days.")


def sync_jobs_from_config(scheduler):
    # Load scheduled_bookings.json + config.json and sync APScheduler jobs.
    # Safe to call at startup *and* whenever either file is modified (idempotent).
    cleanup_old_records(days=30)
    bookings = load_bookings()
    now      = _now()
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
            days_before_loc = loc_cfg.get("open_days_before", 14)
            if (target_date - now.date()).days > days_before_loc:
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
        days_before = loc_cfg.get("open_days_before", 14)
        if days_away < 0:
            log.info(f"[SYNC] One-time '{rule['id']}' -> past date, skip.")
            continue
        if days_away > days_before:
            log.info(f"[SYNC] One-time '{rule['id']}' -> {days_away}d away (window={days_before}d), too far.")
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
    scheduler.add_job(sync_jobs_from_config, "cron", hour=6, minute=0,
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
