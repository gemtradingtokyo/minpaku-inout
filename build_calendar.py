#!/usr/bin/env python3
"""Fetch reservations from the Hostex Open API (v3) and emit calendar.json.

Hostex aggregates every channel (Airbnb, Booking.com, ...), so one API call
covers all of them.

Environment (GitHub Actions secrets):
  HOSTEX_ACCESS_TOKEN  Hostex Open API token (Hostex > Settings > Open API)
  HOSTEX_ROOMS         one room per line:  ROOM|PROPERTY_ID   (e.g. 301U|123456)
                       blank lines and lines starting with '#' are ignored.

Run with --list-properties to print every Hostex property id and title
(used once to fill HOSTEX_ROOMS).

Output: calendar.json
{
  "generated_utc": "...",
  "source": "hostex",
  "rooms": {
    "301U": [ {"in": "2026-09-06", "out": "2026-09-08", "channel": "booking.com",
               "guests": 3, "adults": 2, "children": 1, "infants": 0}, ... ],
    ...
  },
  "errors": ["...", ...]
}
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.hostex.io/v3"
TIMEOUT = 30
PAGE = 100
PAST_DAYS = 7
FUTURE_DAYS = 365


def api_get(path, params):
    token = os.environ.get("HOSTEX_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("HOSTEX_ACCESS_TOKEN is empty")
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Hostex-Access-Token": token,
            "Accept": "application/json",
            "User-Agent": "minpaku-cal/2.0",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as ex:
            if ex.code == 429 or ex.code >= 500:
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
            raise
    if body.get("error_code") != 200:
        raise RuntimeError(f"Hostex error {body.get('error_code')}: {body.get('error_msg')}")
    return body.get("data") or {}


def paged(path, key, params):
    offset = 0
    while True:
        data = api_get(path, {**params, "offset": offset, "limit": PAGE})
        items = data.get(key) or []
        yield from items
        if len(items) < PAGE:
            return
        offset += PAGE


def load_rooms():
    raw = os.environ.get("HOSTEX_ROOMS", "").strip()
    rooms = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1].isdigit():
            print(f"skip malformed line: {line!r}", file=sys.stderr)
            continue
        rooms[int(parts[1])] = parts[0]
    return rooms


def list_properties():
    for p in paged("/properties", "properties", {}):
        chans = ", ".join(f"{c.get('channel_type')}:{c.get('listing_id')}" for c in p.get("channels") or [])
        print(f"{p.get('id')}\t{p.get('title')}\t{chans}")


def main():
    if "--list-properties" in sys.argv:
        list_properties()
        return

    rooms = load_rooms()
    if not rooms:
        print("ERROR: HOSTEX_ROOMS is empty or malformed", file=sys.stderr)
        sys.exit(1)

    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()
    params = {
        "status": "accepted",
        "start_check_out_date": (today - datetime.timedelta(days=PAST_DAYS)).isoformat(),
        "end_check_in_date": (today + datetime.timedelta(days=FUTURE_DAYS)).isoformat(),
        "order_by": "check_in_date",
    }

    result = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": "hostex",
        "rooms": {room: [] for room in rooms.values()},
        "errors": [],
    }

    try:
        reservations = list(paged("/reservations", "reservations", params))
    except Exception as ex:  # noqa: BLE001 - keep the previous calendar.json on any API failure
        print(f"ERROR: Hostex reservations fetch failed: {type(ex).__name__}: {ex}", file=sys.stderr)
        sys.exit(1)

    seen = set()
    for r in reservations:
        if r.get("status") != "accepted":
            continue
        key = r.get("stay_code") or r.get("reservation_code")
        if key in seen:
            continue
        seen.add(key)
        pid = r.get("property_id")
        room = rooms.get(pid)
        if room is None:
            if not pid or r.get("in_reservation_box"):
                result["errors"].append(
                    f"未割当予約: {r.get('channel_type')} {r.get('check_in_date')}~{r.get('check_out_date')}"
                )
            continue
        adults = r.get("number_of_adults") or 0
        children = r.get("number_of_children") or 0
        infants = r.get("number_of_infants") or 0
        result["rooms"][room].append(
            {
                "in": r.get("check_in_date"),
                "out": r.get("check_out_date"),
                "channel": r.get("channel_type"),
                "guests": (adults + children + infants) if adults else (r.get("number_of_guests") or 0),
                "adults": adults,
                "children": children,
                "infants": infants,
            }
        )

    for room in result["rooms"]:
        result["rooms"][room].sort(key=lambda b: (b["in"], b["out"]))

    with open("calendar.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
        f.write("\n")

    total = sum(len(v) for v in result["rooms"].values())
    print(f"wrote calendar.json: {len(rooms)} rooms, {total} reservations, {len(result['errors'])} warnings")
    for e in result["errors"]:
        print("  " + e, file=sys.stderr)


if __name__ == "__main__":
    main()
