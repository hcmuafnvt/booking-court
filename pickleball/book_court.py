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
import logging.handlers
import threading

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

    if debug_mode:
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
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
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%m-%d %H:%M"
        ))

    logger.addHandler(handler)
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
    now_iso = datetime.now().isoformat(timespec="seconds")
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
    today = datetime.now().date()
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
    return any(datetime.now() >= open_datetime_for(target_date, ot, days_before) for ot in _open_times(cfg))


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
    expected_str = target_date.strftime("%A, %B %d, %Y")
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


def select_duration(page, preferred_label=None):
    # Wait for Duration list items (= modal loaded + duration data ready)
    page.wait_for_function(
        "() => document.querySelectorAll('#Duration_listbox li.k-list-item').length > 0",
        timeout=10000
    )
    # Open dropdown + select item
    selected = page.evaluate("""
        (preferred) => {
            const combo = document.querySelector('span[aria-owns="Duration_listbox"]');
            if (combo) combo.click();
            const items = document.querySelectorAll('#Duration_listbox li.k-list-item');
            const fallbacks = ['2 hours', '1 hour'];
            const targets = preferred ? [preferred, ...fallbacks] : fallbacks;
            for (const target of targets) {
                for (const li of items) {
                    const text = li.querySelector('span.k-list-item-text');
                    if (text && text.textContent.trim() === target) {
                        li.click();
                        return target;
                    }
                }
            }
            if (items.length > 0) { items[0].click(); return items[0].textContent.trim(); }
            return null;
        }
    """, preferred_label)
    log.info(f"[BOT] Selected duration: {selected} (preferred: {preferred_label})")
    return selected

def _wait_for_reserve_btn(page, start_slot):
    """MutationObserver: wait for any reserveBtn without .hide for start_slot."""
    page.evaluate(f"""
        () => new Promise(resolve => {{
            const selector = 'tr[data-testid="{start_slot}"] button[data-testid="reserveBtn"]';
            const btns = document.querySelectorAll(selector);
            for (const b of btns) {{
                if (!b.classList.contains('hide')) {{ resolve(); return; }}
            }}
            const container = document.querySelector('#CourtsScheduler') || document.body;
            const obs = new MutationObserver(() => {{
                const btns = document.querySelectorAll(selector);
                for (const b of btns) {{
                    if (!b.classList.contains('hide')) {{
                        obs.disconnect(); resolve(); return;
                    }}
                }}
            }});
            obs.observe(container, {{ subtree: true, childList: true, attributes: true, attributeFilter: ['class'] }});
        }})
    """)
    log.info(f"[BOT] ✅ Reserve button sẵn sàng!")


def wait_for_slots_open(page, target_date, start_slot, open_time_str, loc_cfg, context=None):
    """
    Dual-mode wait:
    - Có #ReservationOpenTimeDispplay: MutationObserver chờ HERE link → click → chờ reserveBtn
    - Không có: sleep đến open_time → navigate_to_date → wait_for_selector (retry 3 lần)
    """
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
        log.info("[BOT] ✅ HERE link xuất hiện, clicking...")
        page.locator('#ReservationOpenTimeDispplay .here-link-text a').first.click()
        _wait_for_reserve_btn(page, start_slot)
    else:
        log.info("[BOT] Không có countdown, sleep đến open_time rồi reload...")
        booking_url = loc_cfg["booking_url"]
        h, m = map(int, open_time_str.split(":"))
        now = datetime.now()
        open_dt = datetime(now.year, now.month, now.day, h, m)
        wait_secs = (open_dt - now).total_seconds()
        if wait_secs > 0:
            log.info(f"[BOT] Sleeping {wait_secs:.0f}s until {open_time_str}...")
            time.sleep(wait_secs)
        for attempt in range(1, 4):
            log.info(f"[BOT] Reload attempt {attempt}/3...")
            navigate_to_date(page, target_date, booking_url=booking_url)
            if context:
                ensure_logged_in(page, context, booking_url, loc_cfg)
            try:
                page.wait_for_selector(
                    f'tr[data-testid="{start_slot}"] button[data-testid="reserveBtn"]:not(.hide)',
                    timeout=10000
                )
                log.info(f"[BOT] ✅ Reserve button found on attempt {attempt}!")
                return
            except Exception:
                log.warning(f"[BOT] Attempt {attempt}/3: no reserve button, {'retrying...' if attempt < 3 else 'giving up.'}")
                if attempt < 3:
                    time.sleep(3)
        raise Exception(f"No reserve button found after 3 reload attempts at {open_time_str}")


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


def _ensure_court_selected(page, loc_cfg):
    """Called immediately after AJAX response — if chip still present the court is available.
    If chip is gone, click the Kendo combobox to open dropdown, wait for items, then select.
    """
    allowed_courts = (loc_cfg or {}).get("courts", [])

    # Chip still present → court is available for this duration, nothing to do
    chip = page.locator('#modal1 span.k-chip-content').first
    if chip.count() > 0:
        log.info(f"[BOT] Court chip still present: '{chip.text_content().strip()}' ✅")
        return True

    log.info("[BOT] Court chip gone — opening CourtIds combobox...")

    # Click Kendo combobox span to open dropdown
    combobox = page.locator('#modal1 span[role="combobox"][aria-controls="CourtIds_listbox"]')
    combobox.click()

    # Wait for listbox items to be populated (Kendo may load async)
    try:
        page.wait_for_function(
            """() => document.querySelectorAll('#CourtIds_listbox li.k-list-item').length > 0""",
            timeout=5000
        )
    except Exception:
        log.warning("[BOT] CourtIds listbox items never populated")
        return False

    # Read items and click the matching court via dispatch_event to bypass visibility check
    items_data = page.evaluate("""() =>
        Array.from(document.querySelectorAll('#CourtIds_listbox li.k-list-item')).map((li, i) => ({
            index: i,
            text: li.textContent.trim()
        }))
    """)
    log.info(f"[BOT] CourtIds listbox: {len(items_data)} item(s), looking for {allowed_courts}")

    for court_name in allowed_courts:
        for item in items_data:
            if court_name in item["text"]:
                log.info(f"[BOT] Clicking court: '{item['text']}'")
                page.evaluate(f"""() => {{
                    const items = document.querySelectorAll('#CourtIds_listbox li.k-list-item');
                    items[{item['index']}].dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
                }}""")
                # Đợi chip xuất hiện (Kendo cập nhật selection) thay vì sleep cứng
                try:
                    page.wait_for_function(
                        """() => document.querySelectorAll('#modal1 span.k-chip-content').length > 0""",
                        timeout=3000
                    )
                except Exception:
                    pass
                return True

    log.warning(f"[BOT] No available court matching {allowed_courts}")
    return False


def book_specific_court(page, time_slot, courtlabel, duration_label=None, test_mode=False, loc_cfg=None, courts_total=1, payment_done=None, payment_lock=None):
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
    # select_duration sẽ wait cho Duration list items xuất hiện (= modal loaded + data ready)
    ajax_pattern = (loc_cfg or {}).get("duration_ajax_pattern")
    if ajax_pattern:
        try:
            with page.expect_response(
                lambda r: ajax_pattern in r.url and r.status == 200,
                timeout=8000
            ):
                select_duration(page, duration_label)
        except Exception:
            pass  # AJAX không fire (free court) → tiếp tục luôn
        _ensure_court_selected(page, loc_cfg)
    else:
        select_duration(page, duration_label)
    if test_mode:
        log.info("[TEST_MODE] Dừng sau khi chọn duration — KHÔNG submit, giữ browser mở.")
        return 0
    try:
        # Checkbox + submit bằng JS 1 call
        page.evaluate("""
            () => {
                const cb = document.querySelector('#modal1 input[data-testid="DisclosureAgree"]');
                if (cb && !cb.checked) {
                    const lbl = document.querySelector('#modal1 label[for="DisclosureAgree"]');
                    if (lbl) lbl.click(); else cb.click();
                }
                const btn = document.querySelector('#modal1 button[type="submit"]')
                           || document.querySelector('#modal1 .btn-primary');
                if (btn) btn.click();
            }
        """)
        log.info(f"[BOT] Submitted booking for court '{courtlabel}'")
        # Wait for either: modal closes (success) OR "Reservation Notice" popup appears (failure)
        try:
            page.wait_for_function(
                """() => {
                    const modal = document.querySelector('#modal1');
                    if (!modal || !modal.classList.contains('show')) return true;
                    const bodyText = document.body.innerText;
                    return bodyText.includes('Reservation Notice');
                }""",
                timeout=10000
            )
        except Exception:
            pass

        # Check for "Reservation Notice" error popup
        if page.locator('body').inner_text().find('Reservation Notice') != -1:
            log.warning(f"[BOT] Court '{courtlabel}' booking FAILED — 'Reservation Notice' appeared.")
            return 0

        # Confirm modal is actually gone
        if page.locator('#modal1.show').count() > 0:
            log.warning(f"[BOT] Court '{courtlabel}' booking FAILED — modal still open after submit.")
            return 0

        # ── Step 2: Payment form ───────────────────────────────────────────
        # SweetAlert2 popup có thể xuất hiện (server đổi court) → click OK để tiếp tục
        swal_btn = page.locator('button.swal2-confirm[data-testid="toast-success"]')
        if swal_btn.count() > 0:
            swal_msg = page.locator('#swal2-html-container').text_content(timeout=2000) or ""
            log.info(f"[BOT] SweetAlert2 popup: {swal_msg.strip()} — clicking OK")
            swal_btn.click()
            try:
                page.wait_for_selector('.swal2-container', state='hidden', timeout=3000)
            except Exception:
                pass

        log.info("[BOT] Step 1 done — waiting for Pay button...")
        try:
            page.wait_for_selector('#PayButton', state='visible', timeout=10000)
            # Đọc amount ở đây — total-value nằm trong Form 2 (payment)
            amount = ""
            try:
                amount = page.locator('[data-testid="total-value"]').first.text_content(timeout=0).strip()
                log.info(f"[BOT] Amount due: {amount}")
            except Exception:
                pass
            # Kiểm tra số courts trong cart — chỉ browser nào thấy đủ mới submit payment
            cart_rows = page.evaluate("""
                () => document.querySelectorAll(
                    '#kendo-table-grid tbody[data-testid="table-grid-body"] tr'
                ).length
            """)
            log.info(f"[BOT] Cart has {cart_rows} row(s), need {courts_total}")
            if cart_rows < courts_total:
                log.info(f"[BOT] Cart incomplete ({cart_rows}/{courts_total}) — skip payment, another browser will handle.")
                return "delegated"

            # Coordination: chỉ 1 browser được submit payment
            if payment_done is not None and payment_lock is not None:
                with payment_lock:
                    if payment_done[0]:
                        log.info("[BOT] Payment already claimed by another browser — skipping.")
                        return "delegated"
                    payment_done[0] = True

            enable_payment = load_global_cfg().get("enable_payment", True)
            if not enable_payment:
                log.info(f"[BOT] enable_payment=false — dừng tại form payment, KHÔNG submit. Amount: {amount}")
                return amount
            log.info("[BOT] Pay button found, clicking...")
            page.locator('#PayButton').click()
            try:
                page.wait_for_selector('#PayButton', state='hidden', timeout=8000)
            except Exception:
                pass
            log.info(f"[BOT] Court '{courtlabel}' BOOKED! Amount paid: {amount}")
            return amount or "paid"
        except Exception as e:
            log.info(f"[BOT] PayButton not found ({e}) — Step 1 succeeded, treating as BOOKED.")
            return "paid"
    except Exception:
        pass
    return 0


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


def _book_now_worker(rule, target_date, court_index, results,
                     courts_total, lock, claimed, scan_results, barrier, payment_done):
    """Phase-1: scan available courts. Phase-2: all-or-nothing assign + book."""
    start            = rule.get("start", "")
    dur              = _duration_label(rule)
    preferred_courts = rule.get("preferred_courts", [])
    loc_cfg          = load_location_cfg(rule["location"])
    test_mode        = loc_cfg.get("test_mode", False)
    booking_url      = loc_cfg["booking_url"]
    allowed          = loc_cfg.get("courts", None)
    p, browser, context, page = open_browser(loc_cfg, test_mode=test_mode)
    try:
        navigate_to_date(page, target_date, booking_url=booking_url)  # set cookie trước
        ensure_logged_in(page, context, booking_url, loc_cfg)  # load page 1 lần duy nhất
        if not verify_calendar_date(page, target_date):
            log.info(f"[BOT] Browser {court_index}: date not available yet, closing browser.")
            results[court_index] = (None, None, "")
            try:
                browser.close()
                p.stop()
            except Exception:
                pass
            try:
                barrier.abort()
            except Exception:
                pass
            return
        # Đợi scheduler render xong — chờ ít nhất 1 reserveBtn visible (không có class .hide)
        try:
            page.wait_for_selector('button[data-testid="reserveBtn"]:not(.hide)', state='attached', timeout=15000)
        except Exception:
            log.warning("[BOT] No visible reserveBtn found after navigate, courts may be empty.")
        # Snapshot toàn bộ courtlabels bằng JS một lần — tránh race condition khi DOM re-render
        all_labels = page.evaluate(f"""
            () => Array.from(document.querySelectorAll(
                'tr[data-testid="{start}"] button[data-testid="reserveBtn"]'
            )).filter(b => !b.classList.contains('hide'))
              .map(b => b.getAttribute('courtlabel'))
        """)
        available = [c for c in all_labels if allowed is None or any(_court_matches(c, a) for a in allowed)]
        with lock:
            scan_results[court_index] = available
        log.info(f"[BOT] Browser {court_index}: scanned courts: {available}")

        # ── Phase-2: wait for all threads then decide ──────────────────────
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            results[court_index] = (None, "Barrier broken — another thread failed", "")
            return

        with lock:
            # Flatten unique courts across all browsers
            seen, unique = set(), []
            for avail in scan_results.values():
                for c in avail:
                    if c not in seen:
                        seen.add(c)
                        unique.append(c)
            if len(unique) < courts_total:
                msg = "No slots available" if len(unique) == 0 else f"Only {len(unique)} of {courts_total} slots available"
                results[court_index] = (None, msg, "")
                return
            # Assign preferred → fallback → any unclaimed
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
                return
            claimed.add(courtlabel)

        log.info(f"[BOT] Browser {court_index}: booking court '{courtlabel}'...")
        ok = book_specific_court(page, start, courtlabel, dur, test_mode=test_mode, loc_cfg=loc_cfg,
                                 courts_total=courts_total, payment_done=payment_done, payment_lock=lock)
        results[court_index] = (courtlabel, None, ok if isinstance(ok, str) else "") if ok != 0 \
                               else (None, f"book_specific_court failed for '{courtlabel}'", "")
        if ok == "delegated":
            log.info(f"[BOT] Browser {court_index}: delegated — closing browser.")
            try:
                browser.close()
                p.stop()
            except Exception:
                pass
    except Exception as e:
        log.error(f"_book_now_worker [{court_index}] error: {e}", exc_info=True)
        try:
            barrier.abort()
        except Exception:
            pass
        results[court_index] = (None, str(e), "")
    finally:
        if load_global_cfg().get("close_after_book", False) and not loc_cfg.get("test_mode", False):
            try:
                browser.close()
                p.stop()
            except Exception:
                pass


def _watch_and_book_worker(rule, target_date, court_index, results,
                           courts_total, lock, claimed, scan_results, barrier, open_time, payment_done):
    """Phase-1: watch until open then scan. Phase-2: all-or-nothing assign + book."""
    start            = rule.get("start", "")
    dur              = _duration_label(rule)
    preferred_courts = rule.get("preferred_courts", [])
    loc_cfg          = load_location_cfg(rule["location"])
    test_mode        = loc_cfg.get("test_mode", False)
    booking_url      = loc_cfg["booking_url"]
    allowed          = loc_cfg.get("courts", None)
    p, browser, context, page = open_browser(loc_cfg, test_mode=test_mode)
    try:
        navigate_to_date(page, target_date, booking_url=booking_url)  # set cookie trước
        ensure_logged_in(page, context, booking_url, loc_cfg)  # load page 1 lần duy nhất
        if not verify_calendar_date(page, target_date):
            log.info(f"[BOT] Browser {court_index}: date not available yet, closing browser.")
            results[court_index] = (None, None, "")
            try:
                browser.close()
                p.stop()
            except Exception:
                pass
            try:
                barrier.abort()
            except Exception:
                pass
            return
        wait_for_slots_open(page, target_date, start, open_time, loc_cfg, context=context)
        # wait_for_slots_open đã MutationObserver đảm bảo reserveBtn:not(.hide) tồn tại → không cần wait_for_selector thêm
        # Snapshot toàn bộ courtlabels bằng JS một lần — tránh race condition khi DOM re-render
        all_labels = page.evaluate(f"""
            () => Array.from(document.querySelectorAll(
                'tr[data-testid="{start}"] button[data-testid="reserveBtn"]'
            )).filter(b => !b.classList.contains('hide'))
              .map(b => b.getAttribute('courtlabel'))
        """)
        available = [c for c in all_labels if allowed is None or any(_court_matches(c, a) for a in allowed)]
        with lock:
            scan_results[court_index] = available
        log.info(f"[BOT] Browser {court_index}: scanned courts: {available}")

        # ── Phase-2: wait for all threads then decide ──────────────────────
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            results[court_index] = (None, "Barrier broken — another thread failed", "")
            return

        with lock:
            seen, unique = set(), []
            for avail in scan_results.values():
                for c in avail:
                    if c not in seen:
                        seen.add(c)
                        unique.append(c)
            if len(unique) < courts_total:
                msg = "No slots available" if len(unique) == 0 else f"Only {len(unique)} of {courts_total} slots available"
                results[court_index] = (None, msg, "")
                return
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
                return
            claimed.add(courtlabel)

        log.info(f"[BOT] Browser {court_index}: booking court '{courtlabel}'...")
        ok = book_specific_court(page, start, courtlabel, dur, test_mode=test_mode, loc_cfg=loc_cfg,
                                 courts_total=courts_total, payment_done=payment_done, payment_lock=lock)
        results[court_index] = (courtlabel, None, ok if isinstance(ok, str) else "") if ok != 0 \
                               else (None, f"book_specific_court failed for '{courtlabel}'", "")
        if ok == "delegated":
            log.info(f"[BOT] Browser {court_index}: delegated — closing browser.")
            try:
                browser.close()
                p.stop()
            except Exception:
                pass
    except Exception as e:
        log.error(f"_watch_and_book_worker [{court_index}] error: {e}", exc_info=True)
        try:
            barrier.abort()
        except Exception:
            pass
        results[court_index] = (None, str(e), "")
    finally:
        if load_global_cfg().get("close_after_book", False) and not loc_cfg.get("test_mode", False):
            try:
                browser.close()
                p.stop()
            except Exception:
                pass


def job_book_now(rule, target_date, open_time=None, is_last=True):
    date_str  = target_date.strftime("%Y-%m-%d")
    courts    = rule.get("courts", 1)
    start     = rule.get("start", "")
    duration  = rule.get("duration", "")
    loc_cfg   = load_location_cfg(rule["location"])
    test_mode = loc_cfg.get("test_mode", False)
    if open_time is None:
        open_time = _open_times(loc_cfg)[0]
    log.info(f"=== JOB book_now | rule={rule['id']} | date={date_str} | {start} x{duration}h x{courts} | T={open_time} ===")
    if not test_mode and get_status(rule["id"], date_str, start) == BOOKED:
        log.info(f"[book_now] {rule['id']} {date_str} already BOOKED, skip.")
        return
    is_recurring = "date" not in rule
    meta = _rule_meta(rule, is_recurring)
    if not test_mode:
        upsert_record(rule["id"], date_str, start, BOOKING, f"booking in progress T={open_time}", extra=meta)
    results      = [None] * courts
    lock         = threading.Lock()
    claimed      = set()
    scan_results = {}
    barrier      = threading.Barrier(courts)
    payment_done = [False]
    threads = [threading.Thread(target=_book_now_worker,
                args=(rule, target_date, i, results, courts, lock, claimed, scan_results, barrier, payment_done))
               for i in range(courts)]
    for t in threads: t.start()
    for t in threads: t.join()
    courts_list = [court for court, _, _  in results if court]
    reasons     = list(dict.fromkeys(r for _, r, _ in results if r))  # deduplicated
    amounts     = [amt  for _, _, amt in results if amt and amt != "delegated"]
    total = len(courts_list)
    if test_mode:
        log.info(f"=== JOB book_now [TEST MODE] done — state NOT updated ===")
        return
    if total > 0:
        upsert_record(rule["id"], date_str, start, BOOKED, f"booked {total}/{courts}",
                      extra={**meta, "courts_booked": courts_list, "amount_paid": ", ".join(amounts)})
        if "date" in rule:
            remove_one_time_scheduled(rule["id"])
    elif is_last:
        upsert_record(rule["id"], date_str, start, FAILED, f"booked {total}/{courts}",
                      extra={**meta, "courts_booked": [], "reason": "; ".join(
                          r.splitlines()[0] if "\n" in r else r for r in reasons
                      )})
        if "date" in rule:
            remove_one_time_scheduled(rule["id"])
    else:
        log.info(f"=== JOB book_now T={open_time} no courts — next open_time slot will retry ===")
    log.info(f"=== JOB book_now done: {total}/{courts} courts booked ===")


def job_watch_and_book(rule, target_date, open_time=None, is_last=True):
    date_str  = target_date.strftime("%Y-%m-%d")
    courts    = rule.get("courts", 1)
    start     = rule.get("start", "")
    duration  = rule.get("duration", "")
    loc_cfg   = load_location_cfg(rule["location"])
    test_mode = loc_cfg.get("test_mode", False)
    if open_time is None:
        open_time = _open_times(loc_cfg)[0]
    log.info(f"=== JOB watch_and_book | rule={rule['id']} | date={date_str} | {start} x{duration}h x{courts} | T={open_time} ===")
    if not test_mode and get_status(rule["id"], date_str, start) == BOOKED:
        log.info(f"[watch_and_book] {rule['id']} {date_str} already BOOKED, skip.")
        return
    is_recurring = "date" not in rule
    meta = _rule_meta(rule, is_recurring)
    if not test_mode:
        upsert_record(rule["id"], date_str, start, BOOKING, f"booking in progress T={open_time}", extra=meta)
    results      = [None] * courts
    lock         = threading.Lock()
    claimed      = set()
    scan_results = {}
    barrier      = threading.Barrier(courts)
    payment_done = [False]
    threads = [threading.Thread(target=_watch_and_book_worker,
                args=(rule, target_date, i, results, courts, lock, claimed, scan_results, barrier, open_time, payment_done))
               for i in range(courts)]
    for t in threads: t.start()
    for t in threads: t.join()
    courts_list = [court for court, _, _  in results if court]
    reasons     = list(dict.fromkeys(r for _, r, _ in results if r))  # deduplicated
    amounts     = [amt  for _, _, amt in results if amt and amt != "delegated"]
    total = len(courts_list)
    if test_mode:
        log.info(f"=== JOB watch_and_book [TEST MODE] done — state NOT updated ===")
        return
    if total > 0:
        upsert_record(rule["id"], date_str, start, BOOKED, f"booked {total}/{courts}",
                      extra={**meta, "courts_booked": courts_list, "amount_paid": ", ".join(amounts)})
        if "date" in rule:
            remove_one_time_scheduled(rule["id"])
    elif is_last:
        upsert_record(rule["id"], date_str, start, FAILED, f"booked {total}/{courts}",
                      extra={**meta, "courts_booked": [], "reason": "; ".join(
                          r.splitlines()[0] if "\n" in r else r for r in reasons
                      )})
        if "date" in rule:
            remove_one_time_scheduled(rule["id"])
    else:
        log.info(f"=== JOB watch_and_book T={open_time} failed — next open_time slot will retry ===")
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
            rec["updated"] = datetime.now().isoformat(timespec="seconds")
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
            id=job_id_immed, replace_existing=True)
        return True

    added = 0
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
                id=job_id_watch, replace_existing=True)
        else:
            log.info(f"[WATCH] {label}\n        watch_and_book → {trigger_dt.strftime('%Y-%m-%d %H:%M')} (open {open_dt.strftime('%H:%M')})")
            upsert_record(rule_id, date_str, start, WATCHING, f"Watch at {trigger_dt} T={open_time}", extra=meta)
            scheduler.add_job(job_watch_and_book, "date", run_date=trigger_dt,
                args=[rule, target_date, open_time, is_last],
                id=job_id_watch, replace_existing=True)
        added += 1

    return added > 0


def cleanup_old_records(days=30):
    """Remove records whose date is older than `days` days."""
    records  = load_history()
    cutoff   = (datetime.now() - timedelta(days=days)).date()
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
