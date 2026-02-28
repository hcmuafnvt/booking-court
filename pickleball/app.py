from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request

BASE_DIR = Path(__file__).resolve().parent
DATA_PATH = BASE_DIR / "scheduled_bookings.json"
BOOKED_PATH = BASE_DIR / "courts_booked.json"

app = Flask(__name__)


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


_LOCATION_DISPLAY = {
    "maspow": "Mas Pow",
    "mas pow": "Mas Pow",
    "dink": "Dink",
}

def _format_location(loc: str) -> str:
    return _LOCATION_DISPLAY.get(loc.strip().lower(), loc.strip())


def load_bookings() -> list[dict[str, str]]:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []

    for item in data.get("recurring", []):
        preferred = ", ".join(item.get("preferred_courts") or []) or "N/A"
        rows.append(
            {
                "id": item.get("id", ""),
                "booking_type": "recurring",
                "date": item.get("day", "-"),
                "type": "Recurring",
                "start_recurring": _format_date(item.get("startRecurring", "")),
                "start": _format_time(item.get("start", "-")),
                "end": _format_time(item.get("end", "-")),
                "courts": str(item.get("courts", "-")),
                "preferred_courts": preferred,
                "location": _format_location(item.get("location", "-")),
                "who": item.get("who", "-"),
                "enabled": bool(item.get("enabled", False)),
            }
        )

    for item in data.get("one_time", []):
        preferred = ", ".join(item.get("preferred_courts") or []) or "N/A"
        rows.append(
            {
                "id": item.get("id", ""),
                "booking_type": "one_time",
                "date": _format_date_long(item.get("date", "")),
                "type": "One-time",
                "start": _format_time(item.get("start", "-")),
                "end": _format_time(item.get("end", "-")),
                "courts": str(item.get("courts", "-")),
                "preferred_courts": preferred,
                "location": _format_location(item.get("location", "-")),
                "who": item.get("who", "-"),
                "enabled": bool(item.get("enabled", False)),
            }
        )

    return rows


def load_booked() -> list[dict[str, str]]:
    data = json.loads(BOOKED_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []
    for item in data:
        status = item.get("status", "-")
        if status not in ("BOOKED", "FAILED"):
            continue
        courts_booked = item.get("courts_booked") or []
        rows.append(
            {
                "type": item.get("type", "-"),
                "day": item.get("day", "-"),
                "date": _format_date_long(item.get("date", "")),
                "start": _format_time(item.get("start", "-")),
                "end": _format_time(item.get("end", "-")),
                "court": item.get("court", "-"),
                "courts_requested": str(item.get("courts_requested", "-")),
                "courts_booked": ", ".join(courts_booked) if courts_booked else "—",
                "location": _format_location(item.get("location", "-")),
                "who": item.get("who", "-"),
                "status": status,
                "note": item.get("note", ""),
                "reason": item.get("reason", ""),
            }
        )
    return rows


@app.route("/")
@app.route("/scheduled")
@app.route("/booked")
def index() -> str:
    return render_template("index.html")


@app.route("/api/scheduled")
def api_scheduled() -> str:
    rows = load_bookings()
    stat1 = sum(int(r["courts"]) for r in rows if r["courts"].isdigit())
    stat2 = len(rows)
    return render_template(
        "partials/scheduled.html",
        rows=rows,
        stat1=stat1,
        stat2=stat2,
    )


@app.route("/api/booked")
def api_booked() -> str:
    rows = load_booked()
    stat1 = sum(1 for r in rows if r["status"] == "BOOKED")
    stat2 = len(rows)
    return render_template(
        "partials/booked.html",
        rows=rows,
        stat1=stat1,
        stat2=stat2,
    )


@app.route("/create")
def create_form() -> str:
    return render_template("create.html", edit_mode=False, booking=None)


def find_booking(booking_id: str):
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    for item in data.get("recurring", []):
        if item.get("id") == booking_id:
            return dict(item), "recurring"
    for item in data.get("one_time", []):
        if item.get("id") == booking_id:
            return dict(item), "one_time"
    return None, None


@app.route("/edit/<booking_id>")
def edit_form(booking_id: str) -> str:
    item, booking_type = find_booking(booking_id)
    if item is None:
        return redirect("/scheduled")
    item["booking_type"] = booking_type
    return render_template("create.html", edit_mode=True, booking=item)


@app.route("/api/create", methods=["POST"])
def api_create():
    try:
        booking_type = request.form.get("type", "recurring")
        who = request.form.get("who", "").strip()
        location = request.form.get("location", "").strip()
        start = request.form.get("start", "").strip()
        end = request.form.get("end", "").strip()
        courts = int(request.form.get("courts", 1))
        preferred = request.form.getlist("preferred_courts")
        enabled = request.form.get("enabled") == "1"

        if not who or not start or not end:
            return jsonify({"ok": False, "error": "Who, Start and End are required."})

        new_entry: dict = {
            "id": str(uuid.uuid4()),
            "who": who,
            "location": location,
            "start": start,
            "end": end,
            "courts": courts,
            "preferred_courts": preferred,
            "enabled": enabled,
        }

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))

        if booking_type == "recurring":
            day = request.form.get("day", "Monday")
            start_recurring = request.form.get("startRecurring", "").strip()
            new_entry["day"] = day
            new_entry["startRecurring"] = start_recurring
            data["recurring"].append(new_entry)
        else:
            date = request.form.get("date", "").strip()
            if not date:
                return jsonify({"ok": False, "error": "Date is required for one-time booking."})
            new_entry["date"] = date
            data["one_time"].append(new_entry)

        DATA_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return jsonify({"ok": True})

    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/delete", methods=["POST"])
def api_delete():
    try:
        body = request.get_json(force=True)
        booking_id = (body or {}).get("id", "").strip()
        booking_type = (body or {}).get("type", "").strip()  # "recurring" or "one_time"

        if not booking_id or booking_type not in ("recurring", "one_time"):
            return jsonify({"ok": False, "error": "Invalid request"})

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        original = data[booking_type]
        data[booking_type] = [item for item in original if item.get("id") != booking_id]

        if len(data[booking_type]) == len(original):
            return jsonify({"ok": False, "error": "Item not found"})

        DATA_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return jsonify({"ok": True})

    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/api/update", methods=["POST"])
def api_update():
    try:
        booking_id = request.form.get("id", "").strip()
        booking_type = request.form.get("booking_type", "").strip()
        who = request.form.get("who", "").strip()
        location = request.form.get("location", "").strip()
        start = request.form.get("start", "").strip()
        end = request.form.get("end", "").strip()
        courts = int(request.form.get("courts", 1))
        preferred = request.form.getlist("preferred_courts")
        enabled = request.form.get("enabled") == "1"

        if not booking_id or booking_type not in ("recurring", "one_time"):
            return jsonify({"ok": False, "error": "Invalid request"})
        if not who or not start or not end:
            return jsonify({"ok": False, "error": "Who, Start and End are required."})

        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        found = False
        for item in data[booking_type]:
            if item.get("id") == booking_id:
                item["who"] = who
                item["location"] = location
                item["start"] = start
                item["end"] = end
                item["courts"] = courts
                item["preferred_courts"] = preferred
                item["enabled"] = enabled
                if booking_type == "recurring":
                    item["day"] = request.form.get("day", item.get("day", "Monday"))
                    item["startRecurring"] = request.form.get("startRecurring", item.get("startRecurring", "")).strip()
                else:
                    item["date"] = request.form.get("date", item.get("date", "")).strip()
                found = True
                break

        if not found:
            return jsonify({"ok": False, "error": "Booking not found"})

        DATA_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return jsonify({"ok": True})

    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)})


if __name__ == "__main__":
    app.run(debug=True)
