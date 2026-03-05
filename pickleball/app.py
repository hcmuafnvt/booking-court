from __future__ import annotations

import json
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from contextlib import asynccontextmanager
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

DATA_PATH = BASE_DIR / "scheduled_bookings.json"
BOOKED_PATH = BASE_DIR / "courts_booked.json"
CONFIG_PATH = BASE_DIR / "config.json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_start_bot, daemon=True, name="booking-bot").start()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _format_date(date_str: str) -> str:
    if not date_str:
        return "-"
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        return parsed.strftime("%b %d, %Y")
    except ValueError:
        return date_str


def _format_date_long(date_str: str) -> str:
    """Return e.g. 'Fri, Feb 27, 2026' from '2026-02-27'."""
    if not date_str:
        return "-"
    try:
        parsed = datetime.strptime(date_str, "%Y-%m-%d")
        return parsed.strftime("%a, %b %d, %Y")
    except ValueError:
        return date_str


def _format_time(time_str: str) -> str:
    """Convert '8:00 PM' → '8 PM', '12:00 AM' → '12 AM', etc."""
    return time_str.replace(":00", "") if time_str else "-"


def _format_time_range(start_str: str, duration) -> str:
    """Return e.g. '8 PM - 10 PM' from '8:00 PM' and duration '2'."""
    def _fmt(dt: datetime) -> str:
        return dt.strftime("%-I:%M %p").replace(":00", "")
    try:
        d = int(duration)
        t = datetime.strptime(start_str.strip(), "%I:%M %p")
        end = t.replace(hour=(t.hour + d) % 24)
        return f"{_fmt(t)} - {_fmt(end)}"
    except Exception:
        return _format_time(start_str)


def _format_duration(d) -> str:
    """Return e.g. '2h' from '2' or 2."""
    if d in (None, "", "-"):
        return "-"
    return f"{d}h"


_LOCATION_DISPLAY = {
    "maspow": "Mas Pow",
    "mas pow": "Mas Pow",
    "dink": "Dink",
}

def _format_location(loc: str) -> str:
    return _LOCATION_DISPLAY.get(loc.strip().lower(), loc.strip())


def _slot_is_open(location: str, date_str: str) -> bool:
    """True nếu cửa sổ đặt sân của date_str đã mở theo config location (check cả giờ)."""
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        loc_cfg = config.get(location, {})
        open_days_before = int(loc_cfg.get("open_days_before", 14))
        open_time = loc_cfg.get("open_time", "20:00")
        target = datetime.strptime(date_str, "%Y-%m-%d").date()
        open_date = target - timedelta(days=open_days_before)
        h, m = map(int, open_time.split(":"))
        open_dt = datetime(open_date.year, open_date.month, open_date.day, h, m)
        return datetime.now() >= open_dt
    except Exception:
        return False


def load_bookings() -> list[dict[str, str]]:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []

    recurring_sorted = sorted(
        data.get("recurring", []),
        key=lambda x: x.get("created_at", "") or "",
        reverse=True,
    )
    for item in recurring_sorted:
        preferred = ", ".join(item.get("preferred_courts") or []) or "N/A"
        rows.append(
            {
                "id": item.get("id", ""),
                "booking_type": "recurring",
                "date": item.get("day", "-"),
                "type": "Recurring",
                "start_recurring": _format_date(item.get("startRecurring", "")),
                "start": _format_time(item.get("start", "-")),
                "duration": _format_duration(item.get("duration")),
                "time_range": _format_time_range(item.get("start", ""), item.get("duration")),
                "courts": str(item.get("courts", "-")),
                "preferred_courts": preferred,
                "location": _format_location(item.get("location", "-")),
                "who": item.get("who", "-"),
                "enabled": bool(item.get("enabled", False)),
                "created_at": item.get("created_at", ""),
                "updated_at": item.get("updated_at", ""),
            }
        )

    one_time_sorted = sorted(
        data.get("one_time", []),
        key=lambda x: x.get("created_at", "") or "",
        reverse=True,
    )
    for item in one_time_sorted:
        preferred = ", ".join(item.get("preferred_courts") or []) or "N/A"
        rows.append(
            {
                "id": item.get("id", ""),
                "booking_type": "one_time",
                "date": _format_date_long(item.get("date", "")),
                "type": "One-time",
                "start": _format_time(item.get("start", "-")),
                "duration": _format_duration(item.get("duration")),
                "time_range": _format_time_range(item.get("start", ""), item.get("duration")),
                "courts": str(item.get("courts", "-")),
                "preferred_courts": preferred,
                "location": _format_location(item.get("location", "-")),
                "who": item.get("who", "-"),
                "enabled": bool(item.get("enabled", False)),
                "created_at": item.get("created_at", ""),
                "updated_at": item.get("updated_at", ""),
            }
        )

    return rows


def load_booked() -> list[dict[str, str]]:
    data = json.loads(BOOKED_PATH.read_text(encoding="utf-8"))
    # Sort by updated_at desc (fall back to updated or created_at for legacy records)
    data = sorted(
        data,
        key=lambda x: x.get("updated_at", x.get("updated", x.get("created_at", ""))) or "",
        reverse=True,
    )
    rows: list[dict[str, str]] = []
    today = datetime.now().date()
    for item in data:
        status = item.get("status", "-")
        if status not in ("BOOKED", "FAILED", "BOOKING"):
            continue
        try:
            item_date = datetime.strptime(item.get("date", ""), "%Y-%m-%d").date()
        except ValueError:
            item_date = None
        if item_date is None or item_date < today:
            continue
        courts_booked = item.get("courts_booked") or []
        rows.append(
            {
                "id": item.get("id", ""),
                "type": item.get("type", "-"),
                "day": item.get("day", "-"),
                "date": _format_date_long(item.get("date", "")),
                "start": _format_time(item.get("start", "-")),
                "duration": _format_duration(item.get("duration")),
                "time_range": _format_time_range(item.get("start", ""), item.get("duration")),
                "court": item.get("court", "-"),
                "courts_requested": str(item.get("courts_requested", "-")),
                "courts_booked": ", ".join(courts_booked) if courts_booked else "0",
                "location": _format_location(item.get("location", "-")),
                "who": item.get("who", "-"),
                "status": status,
                "note": item.get("note", ""),
                "reason": item.get("reason", ""),
                "amount_paid": item.get("amount_paid", ""),
            }
        )
    return rows


@app.get("/", response_class=HTMLResponse)
@app.get("/scheduled", response_class=HTMLResponse)
@app.get("/booked", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/scheduled", response_class=HTMLResponse)
async def api_scheduled(request: Request) -> HTMLResponse:
    rows = load_bookings()
    stat1 = sum(int(r["courts"]) for r in rows if r["courts"].isdigit())
    stat2 = len(rows)
    return templates.TemplateResponse(
        "partials/scheduled.html",
        {"request": request, "rows": rows, "stat1": stat1, "stat2": stat2},
    )


@app.get("/api/booked", response_class=HTMLResponse)
async def api_booked(request: Request) -> HTMLResponse:
    rows = load_booked()
    stat1 = sum(1 for r in rows if r["status"] == "BOOKED")
    stat2 = len(rows)
    admin = request.query_params.get("admin") == "true"
    return templates.TemplateResponse(
        "partials/booked.html",
        {"request": request, "rows": rows, "stat1": stat1, "stat2": stat2, "admin": admin},
    )


@app.post("/api/delete_booked")
async def api_delete_booked(request: Request):
    try:
        body = await request.json()
        record_id = (body or {}).get("id", "").strip()
        if not record_id:
            return JSONResponse({"ok": False, "error": "Invalid request"})
        data = json.loads(BOOKED_PATH.read_text(encoding="utf-8"))
        original_len = len(data)
        data = [item for item in data if item.get("id") != record_id]
        if len(data) == original_len:
            return JSONResponse({"ok": False, "error": "Record not found"})
        BOOKED_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@app.get("/create", response_class=HTMLResponse)
async def create_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("create.html", {"request": request, "edit_mode": False, "booking": None})


def find_booking(booking_id: str):
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    for item in data.get("recurring", []):
        if item.get("id") == booking_id:
            return dict(item), "recurring"
    for item in data.get("one_time", []):
        if item.get("id") == booking_id:
            return dict(item), "one_time"
    return None, None


@app.get("/edit/{booking_id}", response_class=HTMLResponse)
async def edit_form(request: Request, booking_id: str) -> HTMLResponse:
    item, booking_type = find_booking(booking_id)
    if item is None:
        return RedirectResponse("/scheduled")
    item["booking_type"] = booking_type
    item["location"] = item.get("location", "").strip().lower().replace(" ", "")
    return templates.TemplateResponse("create.html", {"request": request, "edit_mode": True, "booking": item})


@app.post("/api/create")
async def api_create(
    type: str = Form(default="recurring"),
    who: str = Form(default=""),
    location: str = Form(default=""),
    start: str = Form(default=""),
    duration: str = Form(default=""),
    courts: int = Form(default=1),
    preferred_courts: list[str] = Form(default=[]),
    enabled: str = Form(default="0"),
    day: str = Form(default="Monday"),
    startRecurring: str = Form(default=""),
    date: str = Form(default=""),
):
    try:
        booking_type = type
        who = who.strip()
        location = location.strip()
        start = start.strip()
        duration = duration.strip()
        _enabled = enabled == "1"

        if not who or not start or not duration:
            return JSONResponse({"ok": False, "error": "Who, Start and Duration are required."})

        now_iso = datetime.now().isoformat(timespec="seconds")
        new_entry: dict = {
            "id": str(uuid.uuid4()),
            "who": who,
            "location": location,
            "start": start,
            "duration": duration,
            "courts": courts,
            "preferred_courts": preferred_courts,
            "enabled": _enabled,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))

        if booking_type == "recurring":
            new_entry["day"] = day
            new_entry["startRecurring"] = startRecurring.strip()
            data["recurring"].append(new_entry)
        else:
            date = date.strip()
            if not date:
                return JSONResponse({"ok": False, "error": "Date is required for one-time booking."})
            new_entry["date"] = date

            if _slot_is_open(location, date):
                try:
                    from book_court import job_book_now  # lazy import
                    target_dt = datetime.strptime(date, "%Y-%m-%d").date()
                    threading.Thread(
                        target=job_book_now,
                        args=(new_entry, target_dt),
                        daemon=True,
                    ).start()
                    return JSONResponse({"ok": True, "immediate": True,
                                        "message": "Slot is open — booking now in background!"})
                except Exception as exc:
                    return JSONResponse({"ok": False, "error": f"Could not trigger booking: {exc}"})
            else:
                data["one_time"].append(new_entry)
                DATA_PATH.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                return JSONResponse({"ok": True})

        DATA_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return JSONResponse({"ok": True})

    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@app.post("/api/delete")
async def api_delete(request: Request):
    try:
        body = await request.json()
        booking_id = (body or {}).get("id", "").strip()
        booking_type = (body or {}).get("type", "").strip()

        if not booking_id or booking_type not in ("recurring", "one_time"):
            return JSONResponse({"ok": False, "error": "Invalid request"})

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        original = data[booking_type]
        data[booking_type] = [item for item in original if item.get("id") != booking_id]

        if len(data[booking_type]) == len(original):
            return JSONResponse({"ok": False, "error": "Item not found"})

        DATA_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return JSONResponse({"ok": True})

    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@app.post("/api/update")
async def api_update(
    id: str = Form(default=""),
    booking_type: str = Form(default=""),
    who: str = Form(default=""),
    location: str = Form(default=""),
    start: str = Form(default=""),
    duration: str = Form(default=""),
    courts: int = Form(default=1),
    preferred_courts: list[str] = Form(default=[]),
    enabled: str = Form(default="0"),
    day: str = Form(default="Monday"),
    startRecurring: str = Form(default=""),
    date: str = Form(default=""),
):
    try:
        booking_id = id.strip()
        booking_type = booking_type.strip()
        who = who.strip()
        location = location.strip()
        start = start.strip()
        duration = duration.strip()
        _enabled = enabled == "1"

        if not booking_id or booking_type not in ("recurring", "one_time"):
            return JSONResponse({"ok": False, "error": "Invalid request"})
        if not who or not start or not duration:
            return JSONResponse({"ok": False, "error": "Who, Start and Duration are required."})

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        found = False
        for item in data[booking_type]:
            if item.get("id") == booking_id:
                item["who"] = who
                item["location"] = location
                item["start"] = start
                item["duration"] = duration
                item["courts"] = courts
                item["preferred_courts"] = preferred_courts
                item["enabled"] = _enabled
                item["updated_at"] = datetime.now().isoformat(timespec="seconds")
                if booking_type == "recurring":
                    item["day"] = day
                    item["startRecurring"] = startRecurring.strip()
                else:
                    item["date"] = date.strip()
                found = True
                break

        if not found:
            return JSONResponse({"ok": False, "error": "Booking not found"})

        DATA_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return JSONResponse({"ok": True})

    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


def _start_bot() -> None:
    """Run the booking bot inside a daemon thread."""
    try:
        from book_court import main as bot_main
        bot_main()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[BOT] crashed: {exc}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5021, reload=False, access_log=False)
