#!/usr/bin/env python3
"""Fetch reservations from the Hostex Open API (v3) and emit calendar.json.

Hostex aggregates every channel (Airbnb, Booking.com, ...), so one API call
covers all of them.

Environment (GitHub Actions secrets):
  HOSTEX_ACCESS_TOKEN  Hostex Open API token (Hostex > Settings > Open API)
  HOSTEX_ROOMS         one room per line:  ROOM|PROPERTY_ID   (e.g. 301U|123456)
                       blank lines and lines starting with '#' are ignored.
  ICAL_URLS            optional. One Airbnb listing per line: ROOM|LISTING_ID|TOKEN
                       Used only to cross-check Hostex against Airbnb's own feed,
                       so a reservation Hostex missed -- or kept on the wrong room
                       -- gets caught. Never used as the calendar itself.

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
  "errors": ["...", ...],
  "airbnb_check": {
    "checked": true,
    "rooms_ok": 29,
    "mismatches": [{"room": "601U", "in": "...", "out": "...",
                    "kind": "missing_in_hostex" | "missing_in_airbnb"}],
    "errors": ["601U: ...", ...]
  }
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
ICAL_BASE = "https://www.airbnb.jp/calendar/ical"
TIMEOUT = 30
PAGE = 100
PAST_DAYS = 7
FUTURE_DAYS = 170


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


# --- Airbnb iCal cross-check -------------------------------------------------
# Hostex stays the source of truth for the report, but Hostex can fall out of
# sync with Airbnb without saying so -- a room change made on Airbnb that Hostex
# never picks up leaves the new room looking empty, so no cleaning is ordered.
# Airbnb's own per-listing iCal feed is an independent view of the same
# reservations, so comparing the two catches exactly that.


def load_ical_listings():
    raw = os.environ.get("ICAL_URLS", "").strip()
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or not all(parts[:3]):
            print(f"skip malformed ICAL_URLS line: {line!r}", file=sys.stderr)
            continue
        out.append((parts[0], parts[1], parts[2]))
    return out


def _ical_date(v):
    v = v.strip()
    if "T" in v:
        v = v.split("T", 1)[0]
    if len(v) == 8 and v.isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return v


def parse_ical(text):
    """Return sorted [(check_in, check_out)] for real reservations.

    Airbnb marks bookings with SUMMARY 'Reserved'; host blocks use
    'Airbnb (Not available)' and are skipped. DTEND is the checkout date,
    which is what Hostex calls check_out_date.
    """
    events, cur, in_event = [], {}, False
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line == "BEGIN:VEVENT":
            in_event, cur = True, {}
        elif line == "END:VEVENT":
            in_event = False
            s, e = cur.get("DTSTART"), cur.get("DTEND")
            if s and e and cur.get("SUMMARY", "").strip().lower().startswith("reserved"):
                events.append((s, e))
        elif in_event and ":" in line:
            key, _, val = line.partition(":")
            key = key.split(";", 1)[0]
            if key in ("DTSTART", "DTEND"):
                cur[key] = _ical_date(val)
            elif key == "SUMMARY":
                cur["SUMMARY"] = val
    return sorted(set(events))


def fetch_ical(listing_id, token):
    url = f"{ICAL_BASE}/{listing_id}.ics?t={token}"
    req = urllib.request.Request(url, headers={"User-Agent": "minpaku-cal/2.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def airbnb_check(hostex_rooms, today, horizon):
    """Compare Airbnb's iCal feeds against what Hostex gave us."""
    listings = load_ical_listings()
    check = {"checked": False, "rooms_ok": 0, "mismatches": [], "errors": []}
    if not listings:
        return check
    check["checked"] = True

    def in_window(check_in, check_out):
        return check_out >= today and check_in <= horizon

    for room, lid, token in listings:
        if room not in hostex_rooms:
            check["errors"].append(f"{room}: ICAL_URLSにあるがHOSTEX_ROOMSにない")
            continue
        try:
            events = parse_ical(fetch_ical(lid, token))
        except Exception as ex:  # noqa: BLE001 - one bad feed must not sink the run
            check["errors"].append(f"{room}: {type(ex).__name__}: {ex}")
            continue
        check["rooms_ok"] += 1
        air = {(ci, co) for ci, co in events if in_window(ci, co)}
        hos = {
            (b["in"], b["out"])
            for b in hostex_rooms[room]
            if b.get("channel") == "airbnb" and in_window(b["in"], b["out"])
        }
        for ci, co in sorted(air - hos):
            check["mismatches"].append({"room": room, "in": ci, "out": co, "kind": "missing_in_hostex"})
        for ci, co in sorted(hos - air):
            check["mismatches"].append({"room": room, "in": ci, "out": co, "kind": "missing_in_airbnb"})

    check["mismatches"].sort(key=lambda m: (m["in"], m["room"]))
    return check


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
        "end_check_out_date": (today + datetime.timedelta(days=FUTURE_DAYS)).isoformat(),
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
                "code": key,
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

    result["airbnb_check"] = airbnb_check(
        result["rooms"],
        today.isoformat(),
        (today + datetime.timedelta(days=FUTURE_DAYS)).isoformat(),
    )

    with open("calendar.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
        f.write("\n")

    total = sum(len(v) for v in result["rooms"].values())
    chk = result["airbnb_check"]
    print(f"wrote calendar.json: {len(rooms)} rooms, {total} reservations, {len(result['errors'])} warnings")
    if chk["checked"]:
        print(
            f"airbnb cross-check: {chk['rooms_ok']} feeds ok, "
            f"{len(chk['mismatches'])} mismatches, {len(chk['errors'])} feed errors"
        )
    else:
        print("airbnb cross-check: skipped (ICAL_URLS not set)")
    for e in result["errors"] + chk["errors"]:
        print("  " + e, file=sys.stderr)
    for m in chk["mismatches"]:
        print(f"  MISMATCH {m['room']} {m['in']}~{m['out']} {m['kind']}", file=sys.stderr)


if __name__ == "__main__":
    main()
