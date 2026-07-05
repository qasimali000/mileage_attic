import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import quote

import requests
from flask import Flask, Response, request, send_from_directory

app = Flask(__name__, static_folder="static")

ORS_API_KEY = os.environ.get("ORS_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
DB_PATH = os.environ.get("CACHE_DB", "distance_cache.db")

RATE_PER_MILE = 0.45
WAITING_RATE_PER_HOUR = 13.40

SLEEP_BETWEEN_GEOCODE = 0.15
SLEEP_BETWEEN_ROUTE = 1.0
RETRIES = 3
RETRY_BACKOFF = 1.5

POSTCODES_IO_BASE = "https://api.postcodes.io/postcodes/"
ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car"

POSTCODE_RE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b(.*)$", re.I)
DATE_RE = re.compile(
    r"^(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?$",
    re.I,
)

db_lock = threading.Lock()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pairs ("
            "pc_from TEXT NOT NULL, pc_to TEXT NOT NULL, miles REAL NOT NULL, "
            "PRIMARY KEY (pc_from, pc_to))"
        )


def cache_get(pc_from, pc_to):
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT miles FROM pairs WHERE pc_from=? AND pc_to=?", (pc_from, pc_to)
        ).fetchone()
    return row[0] if row else None


def cache_put(pc_from, pc_to, miles):
    with db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pairs (pc_from, pc_to, miles) VALUES (?,?,?)",
            (pc_from, pc_to, miles),
        )


def parse_wait_minutes(text):
    if not text:
        return 0
    text = str(text).lower()
    total = 0
    h = re.search(r"(\d+)\s*h(?:ou)?r", text)
    m = re.search(r"(\d+)\s*min", text)
    if h:
        total += int(h.group(1)) * 60
    if m:
        total += int(m.group(1))
    return total


def normalise_postcode(outward, inward):
    return f"{outward.upper()} {inward.upper()}"


def parse_notes(raw):
    """Raw pasted notes -> (journeys, warnings).

    journeys: list of dicts {date, from, to, note}
    warnings: list of ignored/odd lines
    """
    journeys = []
    warnings = []
    current_date = None
    prev = None  # (postcode, note) of last stop in current chain

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            prev = None
            continue

        dm = DATE_RE.match(line)
        if dm:
            day = dm.group(1)
            month = dm.group(2).capitalize()
            current_date = f"{day} {month}"
            prev = None
            continue

        pm = POSTCODE_RE.match(line)
        if pm:
            pc = normalise_postcode(pm.group(1), pm.group(2))
            note = pm.group(3).strip(" -–—\t")
            if current_date is None:
                warnings.append(f"Postcode before any date header, skipped: {line}")
                continue
            if prev is not None:
                journeys.append(
                    {"date": current_date, "from": prev[0], "to": pc, "note": prev[1]}
                )
            prev = (pc, note)
            continue

        warnings.append(f"Ignored: {line}")

    return journeys, warnings


coords_cache = {}


def get_coordinates(pc):
    if pc in coords_cache:
        return coords_cache[pc]
    try:
        r = requests.get(POSTCODES_IO_BASE + quote(pc), timeout=10)
        j = r.json()
        if r.status_code == 200 and j.get("status") == 200 and j.get("result"):
            coords_cache[pc] = (j["result"]["longitude"], j["result"]["latitude"])
        else:
            coords_cache[pc] = None
    except Exception:
        coords_cache[pc] = None
    time.sleep(SLEEP_BETWEEN_GEOCODE)
    return coords_cache[pc]


def get_driving_distance_miles(coord_from, coord_to):
    headers = {"Authorization": ORS_API_KEY, "Content-Type": "application/json"}
    body = {"coordinates": [list(coord_from), list(coord_to)]}
    attempt = 0
    wait = 1.0
    while attempt < RETRIES:
        try:
            resp = requests.post(ORS_URL, json=body, headers=headers, timeout=30)
            data = resp.json() if resp.text else {}
            if resp.status_code == 200:
                routes = data.get("routes")
                if routes and routes[0].get("summary"):
                    meters = routes[0]["summary"].get("distance")
                    if meters is None:
                        return None, "no_distance_in_response"
                    return round(meters / 1609.34, 2), "ok"
                return None, "no_route"
            if resp.status_code in (429, 500, 502, 503, 504):
                attempt += 1
                time.sleep(wait)
                wait *= RETRY_BACKOFF
                continue
            return None, f"api_error_{resp.status_code}"
        except Exception:
            attempt += 1
            time.sleep(wait)
            wait *= RETRY_BACKOFF
    return None, "retries_exhausted"


def sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def csv_cell(c):
    c = str(c)
    if "," in c or '"' in c:
        c = '"' + c.replace('"', '""') + '"'
    return c


def build_csv(rows, summary=None, name="", month=""):
    lines = []
    if name:
        lines.append(f"Name,{csv_cell(name)}")
    if month:
        lines.append(f"Month,{csv_cell(month)}")
    if name or month:
        lines.append("")
    lines.append("Date,from,to,Notes,distance_miles,Amount,Waiting Time,Waiting Amount")
    for r in rows:
        cells = [
            r["date"], r["from"], r["to"], r["note"],
            "" if r["miles"] is None else f"{r['miles']}",
            r["amount"], r["wait_time"], r["wait_amount"],
        ]
        lines.append(",".join(csv_cell(c) for c in cells))
    if summary:
        wm = summary["total_wait_minutes"]
        wait_str = f"{wm // 60}h {wm % 60}m" if wm >= 60 else f"{wm}m"
        lines += [
            "",
            "Summary",
            f"Total Miles,{summary['total_miles']:.2f}",
            f"Mileage Amount (@ £{summary['rate_per_mile']:.2f}/mile),£{summary['total_amount']:.2f}",
            f"Waiting Time,{wait_str}",
            f"Waiting Amount (@ £{summary['waiting_rate_per_hour']:.2f}/hour),£{summary['total_wait_amount']:.2f}",
            f"Grand Total,£{summary['grand_total']:.2f}",
        ]
    return "\n".join(lines) + "\n"


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/calculate", methods=["POST"])
def calculate():
    body = request.get_json(silent=True) or {}
    if not APP_PASSWORD or body.get("password") != APP_PASSWORD:
        return Response(
            json.dumps({"error": "ACCESS DENIED"}), status=401, mimetype="application/json"
        )
    notes = body.get("notes", "")
    try:
        rate_per_mile = float(body.get("rate_per_mile") or RATE_PER_MILE)
        if not 0 < rate_per_mile < 100:
            rate_per_mile = RATE_PER_MILE
    except (TypeError, ValueError):
        rate_per_mile = RATE_PER_MILE
    claimant_name = str(body.get("name", "")).strip()[:80]
    claim_month = str(body.get("month", "")).strip()[:20]
    journeys, warnings = parse_notes(notes)

    def generate():
        for w in warnings:
            yield sse("warning", {"message": w})

        if not journeys:
            yield sse("done", {"rows": [], "daily": [], "summary": None, "csv": ""})
            return

        total = len(journeys)
        rows = []
        for i, j in enumerate(journeys, 1):
            miles = cache_get(j["from"], j["to"])
            status = "cached"
            if miles is None:
                cf = get_coordinates(j["from"])
                ct = get_coordinates(j["to"])
                if cf is None or ct is None:
                    bad = j["from"] if cf is None else j["to"]
                    status = f"invalid_postcode({bad})"
                    yield sse("warning", {"message": f"Postcode not found: {bad}"})
                else:
                    miles, status = get_driving_distance_miles(cf, ct)
                    if miles is not None:
                        cache_put(j["from"], j["to"], miles)
                    time.sleep(SLEEP_BETWEEN_ROUTE)

            wait_minutes = parse_wait_minutes(j["note"])
            wait_time = ""
            wait_amount = ""
            if wait_minutes:
                wt = round(wait_minutes / 60 * WAITING_RATE_PER_HOUR, 2)
                wait_time = (
                    f"{wait_minutes // 60}h {wait_minutes % 60}m"
                    if wait_minutes >= 60
                    else f"{wait_minutes}m"
                )
                wait_amount = f"£{wt:.2f}"

            rows.append(
                {
                    "date": j["date"], "from": j["from"], "to": j["to"],
                    "note": j["note"], "miles": miles, "status": status,
                    "wait_time": wait_time, "wait_amount": wait_amount, "amount": "",
                }
            )
            yield sse(
                "progress",
                {"i": i, "total": total, "from": j["from"], "to": j["to"],
                 "miles": miles, "status": status},
            )

        # daily totals, amount on first row of each date
        daily = []
        seen = {}
        for r in rows:
            if r["date"] not in seen:
                seen[r["date"]] = {"date": r["date"], "miles": 0.0, "first": r}
                daily.append(seen[r["date"]])
            if r["miles"]:
                seen[r["date"]]["miles"] += r["miles"]
        for d in daily:
            d["miles"] = round(d["miles"], 2)
            d["amount"] = round(d["miles"] * rate_per_mile, 2)
            d["first"]["amount"] = f"£{d['amount']:.2f}"
            del d["first"]

        total_miles = round(sum(d["miles"] for d in daily), 2)
        total_amount = round(total_miles * rate_per_mile, 2)
        total_wait_minutes = sum(parse_wait_minutes(r["note"]) for r in rows)
        total_wait_amount = round(total_wait_minutes / 60 * WAITING_RATE_PER_HOUR, 2)

        summary = {
            "total_miles": total_miles,
            "total_amount": total_amount,
            "total_wait_minutes": total_wait_minutes,
            "total_wait_amount": total_wait_amount,
            "grand_total": round(total_amount + total_wait_amount, 2),
            "rate_per_mile": rate_per_mile,
            "waiting_rate_per_hour": WAITING_RATE_PER_HOUR,
        }

        yield sse("done", {"rows": rows, "daily": daily, "summary": summary,
                           "csv": build_csv(rows, summary, claimant_name, claim_month)})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)), threaded=True)
