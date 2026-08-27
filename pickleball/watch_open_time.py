"""
Theo dõi sân mở đặt trước bao nhiêu ngày và mở vào giờ nào.

NGUYÊN LÝ
    Trang đặt sân nhúng sẵn ngày xa nhất đặt được, dạng:
        "max": new Date(2026,8,11,23,59,0,0)     (JavaScript đếm tháng từ 0 => tháng 9)
    Nghĩa là "hôm nay đặt được xa nhất tới hết 11/09/2026".

    Mỗi ngày tới một giờ nào đó họ nhả thêm 1 ngày, con số này nhảy lên 1.
    Khoảnh khắc nó nhảy chính là giờ mở. Lấy nó trừ hôm nay ra số ngày mở trước.

    Đọc con số này chỉ cần 1 request HTTP thường (~500ms): không mở trình duyệt,
    không gọi API, không cần token. Chỉ cần cookie đăng nhập.

    Nếu cookie hỏng, con số tụt xuống mức khách vãng lai (+7 ngày). Dùng chính
    dấu hiệu đó để biết lúc nào phải đăng nhập lại, và KHÔNG ghi sự kiện giả.

FILE SINH RA
    pickleball/open_watch.jsonl     — file log duy nhất (đã có trong .gitignore)
    Cookie không lưu riêng: lấy thẳng từ browser lúc khởi động, dùng lại
    session sẵn có của book_court_api (toan_session.json).

CÁC LỆNH
    ./venv/bin/python pickleball/watch_open_time.py probe        # đọc 1 lần rồi thoát
    ./venv/bin/python pickleball/watch_open_time.py watch        # chạy nền, 10 phút/lần
    ./venv/bin/python pickleball/watch_open_time.py report       # in bảng tổng hợp
    thêm --location maspow|dink|zero    (mặc định maspow)
    thêm --interval 600                 (giây, mặc định 600 = 10 phút)

CHẠY TRÊN EC2
    Giờ hệ thống EC2 thường là UTC, nhưng file này luôn quy về giờ Vancouver
    (book_court_api.TZ), không dùng date.today() ở đâu cả.
    Cần headless_mode = true trong config.json.
"""
import sys
import os
import re
import json
import time
import logging
import argparse
from datetime import datetime, timedelta, date

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)

import book_court_api as B  # dùng lại open_browser / ensure_logged_in / TZ

TZ       = B.TZ                                     # America/Vancouver
LOG_FILE = os.path.join(DIR, "open_watch.jsonl")

# Ngày xa nhất mà KHÁCH CHƯA ĐĂNG NHẬP nhìn thấy. Đọc ra <= mức này nghĩa là
# cookie hỏng chứ không phải sân đóng bớt.
GUEST_HORIZON_DAYS = 7

# Chỉ '.AspNet.ApplicationCookie' là bắt buộc. Giữ thêm 2 cái kia phòng Cloudflare.
NEEDED_COOKIES = [".AspNet.ApplicationCookie", "cf_clearance", "ASP.NET_SessionId"]

# "max":new Date(2026,8,11,23,59,0,0)
RX_MAX = re.compile(r'"max"\s*:\s*new Date\((\d+),(\d+),(\d+)')

# Log ra stdout để systemd/journalctl bắt được. Không dùng logger của
# book_court_api vì khi debug_mode=false nó ghi vào file bookbot.log.
log = logging.getLogger("OpenWatch")
if not log.handlers:
    log.setLevel(logging.INFO)
    log.propagate = False
    _h = logging.StreamHandler(sys.stdout)

    class _VanFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            return datetime.fromtimestamp(record.created, tz=TZ).strftime(
                datefmt or "%Y-%m-%d %H:%M:%S %Z")

    _h.setFormatter(_VanFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(_h)


def now():
    """Giờ Vancouver, CÓ kèm offset. Không bao giờ dùng giờ hệ thống."""
    return datetime.now(TZ)


# ── Ghi log ────────────────────────────────────────────────────────────────
def write_event(**kw):
    kw.setdefault("at", now().isoformat(timespec="seconds"))
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")


def read_events():
    if not os.path.exists(LOG_FILE):
        return []
    out = []
    for line in open(LOG_FILE):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # dòng hỏng do bị kill giữa chừng — bỏ qua
    return out


# ── Lấy cookie: mở browser, đăng nhập nếu cần ──────────────────────────────
def bootstrap(loc):
    """Mở browser, đảm bảo đã đăng nhập, lấy cookie + user-agent.
    User-agent phải lấy từ chính browser: Cloudflare gắn cf_clearance với UA,
    hardcode sai UA là cookie mất tác dụng."""
    cfg = B.load_location_cfg(loc)
    log.info(f"Mở browser lấy cookie cho '{loc}'...")
    p, browser, context, page = B.open_browser(cfg)
    try:
        B.ensure_logged_in(page, context, cfg["booking_url"], cfg)
        cookies = [c for c in context.cookies() if c["name"] in NEEDED_COOKIES]
        ua = page.evaluate("() => navigator.userAgent")
    finally:
        B._close_browser(browser, p)

    if not any(c["name"] == ".AspNet.ApplicationCookie" for c in cookies):
        raise RuntimeError("Không lấy được cookie đăng nhập")
    log.info(f"Đã lấy {len(cookies)} cookie.")
    return cookies, ua


def make_session(cookies, ua):
    import requests
    s = requests.Session()
    s.headers.update({"user-agent": ua, "accept-language": "en-US,en;q=0.9"})
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    return s


# ── Đọc ngày xa nhất ───────────────────────────────────────────────────────
def read_horizon(sess, booking_url):
    """Trả về (ngày_xa_nhất | None, số_ms, http_status | None)."""
    t = time.time()
    try:
        r = sess.get(booking_url, timeout=30)
    except Exception as e:
        log.warning(f"Lỗi mạng: {e}")
        return None, int((time.time() - t) * 1000), None
    ms = int((time.time() - t) * 1000)
    if r.status_code == 403:
        log.warning("HTTP 403 — Cloudflare chặn.")
        return None, ms, 403
    m = RX_MAX.search(r.text)
    if not m:
        return None, ms, r.status_code
    # JavaScript đếm tháng từ 0
    return date(int(m.group(1)), int(m.group(2)) + 1, int(m.group(3))), ms, r.status_code


def horizon_or_relogin(loc, box, booking_url):
    """Đọc ngày xa nhất. Nếu hỏng hoặc ra mức khách vãng lai thì đăng nhập lại
    rồi thử lần nữa. Trả về ngày, hoặc None nếu vẫn không được."""
    def _ok(mx):
        return mx is not None and (mx - now().date()).days > GUEST_HORIZON_DAYS

    if box["s"] is not None:
        mx, ms, st = read_horizon(box["s"], booking_url)
        if _ok(mx):
            log.debug(f"max={mx} [{ms}ms]")
            return mx
        log.warning(f"Đọc ra max={mx} (http={st}) — cookie hỏng, đăng nhập lại.")
        write_event(type="relogin", reason=f"max={mx} http={st}", location=loc)

    try:
        cookies, ua = bootstrap(loc)
        box["s"] = make_session(cookies, ua)
    except Exception as e:
        log.error(f"Đăng nhập lại thất bại: {e}")
        box["s"] = None
        return None

    mx, ms, st = read_horizon(box["s"], booking_url)
    if not _ok(mx):
        log.error(f"Sau khi đăng nhập lại vẫn đọc ra max={mx} (http={st}).")
        box["s"] = None
        return None
    return mx


# ── Đoán giờ mở: bắt mốc :00 / :30 nằm trong khoảng nghi ngờ ───────────────
def half_hour_marks(after, until):
    """Mọi mốc :00 và :30 trong khoảng (after, until]. Sân chỉ mở vào các mốc
    này, nên nếu khoảng chỉ chứa đúng 1 mốc thì xác định được chính xác."""
    t = after.replace(second=0, microsecond=0)
    t = t.replace(minute=(0 if t.minute < 30 else 30))
    marks = []
    while t <= until:
        if t > after:
            marks.append(t)
        t += timedelta(minutes=30)
    return marks


# ── Khôi phục trạng thái từ chính file log ─────────────────────────────────
def last_known_max(loc):
    for e in reversed(read_events()):
        if e.get("location") == loc and e.get("new_max"):
            return date.fromisoformat(e["new_max"])
    return None


# ── Vòng lặp chính ─────────────────────────────────────────────────────────
def watch(loc, interval):
    cfg = B.load_location_cfg(loc)
    url = cfg["booking_url"]

    prev_max = last_known_max(loc)
    prev_at  = None          # chưa biết lần đọc trước lúc nào (vừa khởi động)
    box      = {"s": None}   # session HTTP, None = phải bootstrap

    log.info(f"Theo dõi '{loc}', {interval}s/lần. Ngày xa nhất đã biết: {prev_max}")
    write_event(type="start", location=loc, interval_sec=interval,
                resumed_from=prev_max.isoformat() if prev_max else None)

    while True:
        t_now = now()
        mx = horizon_or_relogin(loc, box, url)

        if mx is None:
            # Không đọc được -> KHÔNG ghi sự kiện mở, giữ nguyên prev_max và
            # prev_at để khoảng nghi ngờ nới rộng cho đúng ở lần sau.
            log.warning("Bỏ qua lượt này.")
            write_event(type="error", location=loc, note="không đọc được max")
        else:
            days = (mx - t_now.date()).days
            if prev_max is None:
                log.info(f"Lần đọc đầu tiên: max={mx} (+{days} ngày)")
                write_event(type="baseline", location=loc,
                            new_max=mx.isoformat(), days_ahead=days)
            elif mx > prev_max:
                marks = half_hour_marks(prev_at, t_now) if prev_at else []
                est = marks[0].strftime("%H:%M") if len(marks) == 1 else None
                write_event(type="open", location=loc,
                            prev_probe_at=prev_at.isoformat(timespec="seconds") if prev_at else None,
                            window_sec=int((t_now - prev_at).total_seconds()) if prev_at else None,
                            old_max=prev_max.isoformat(), new_max=mx.isoformat(),
                            days_ahead=days, weekday=mx.strftime("%A"),
                            open_time_estimate=est,
                            candidates=[m.strftime("%H:%M") for m in marks])
                log.info(f"★ MỞ THÊM NGÀY: {prev_max} -> {mx} (+{days} ngày), "
                         f"giờ mở ≈ {est or ('/'.join(m.strftime('%H:%M') for m in marks) or '?')}")
            elif mx < prev_max:
                log.warning(f"Ngày xa nhất LÙI LẠI: {prev_max} -> {mx} — bất thường.")
                write_event(type="anomaly", location=loc, anomaly="REGRESS",
                            old_max=prev_max.isoformat(), new_max=mx.isoformat(),
                            days_ahead=days)
            else:
                log.debug(f"không đổi: max={mx} (+{days} ngày)")

            prev_max, prev_at = mx, t_now

        # ngủ tới mốc kế tiếp, canh theo đồng hồ thật để không bị trôi dần
        time.sleep(interval - (time.time() % interval))


# ── Đọc 1 lần ──────────────────────────────────────────────────────────────
def probe(loc, interval=None):
    cfg = B.load_location_cfg(loc)
    box = {"s": None}
    mx = horizon_or_relogin(loc, box, cfg["booking_url"])
    today = now().date()
    print(f"giờ Vancouver  : {now():%Y-%m-%d %H:%M:%S %Z}")
    print(f"giờ hệ thống   : {datetime.now():%Y-%m-%d %H:%M:%S} "
          f"({time.tzname[0]})")
    print(f"ngày xa nhất   : {mx}")
    print(f"=> mở trước {(mx - today).days} ngày" if mx else "=> đọc không được")


# ── Bảng tổng hợp ──────────────────────────────────────────────────────────
def report(loc, interval=None):
    evs = [e for e in read_events() if e.get("location") == loc]
    opens = [e for e in evs if e.get("type") == "open"]
    if not opens:
        base = [e for e in evs if e.get("type") == "baseline"]
        print("Chưa ghi nhận lần mở nào." +
              (f" Mốc ban đầu: {base[-1]['new_max']} (+{base[-1]['days_ahead']} ngày)"
               if base else ""))
        return

    print(f"\n{'ngày sân mới':<14} {'mở trước':>9} {'giờ mở':>8} {'sai số':>8}  "
          f"{'phát hiện lúc':<21} thứ")
    print("-" * 78)
    for e in opens:
        est = e.get("open_time_estimate") or ("?" + "/".join(e.get("candidates") or []))
        w = e.get("window_sec")
        print(f"{e['new_max']:<14} {str(e['days_ahead']) + ' ngày':>9} {est:>8} "
              f"{(str(round(w / 60)) + ' phút') if w else '?':>8}  "
              f"{e['at'][:19]:<21} {e.get('weekday', '')}")

    times = sorted({e["open_time_estimate"] for e in opens if e.get("open_time_estimate")})
    days  = sorted({e["days_ahead"] for e in opens})
    print(f"\nGiờ mở   : {times or 'chưa xác định chắc chắn'}"
          + ("   ⚠️ KHÔNG cố định" if len(times) > 1 else ""))
    print(f"Mở trước : {days} ngày"
          + ("   ⚠️ KHÔNG cố định" if len(days) > 1 else ""))

    for t, label in [("anomaly", "Bất thường"), ("relogin", "Đăng nhập lại"),
                     ("error", "Lỗi đọc")]:
        n = sum(1 for e in evs if e.get("type") == t)
        if n:
            print(f"{label:<14}: {n} lần")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["probe", "watch", "report"])
    ap.add_argument("--location", default="maspow")
    ap.add_argument("--interval", type=int, default=600,
                    help="giây giữa 2 lần đọc, mặc định 600 = 10 phút")
    a = ap.parse_args()
    try:
        {"probe": probe, "watch": watch, "report": report}[a.mode](a.location, a.interval)
    except KeyboardInterrupt:
        log.info("Dừng.")
