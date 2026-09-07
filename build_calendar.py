#!/usr/bin/env python3
"""Fetch Airbnb per-listing iCal feeds and emit calendar.json.

Reads the listing list from the ICAL_URLS environment variable (a GitHub Actions
secret). One listing per line:  ROOM|LISTING_ID|TOKEN
Lines that are blank or start with '#' are ignored.

Output: calendar.json
{
  "generated_utc": "...",
  "rooms": {
    "301U": [ {"in": "2026-09-06", "out": "2026-09-08"}, ... ],   # sorted, real reservations only
    "405U": null,                                                 # null = this feed failed to fetch
    ...
  },
  "errors": ["405U: <reason>", ...]
}
"""
import json
import os
import sys
import datetime
import urllib.request

TIMEOUT = 30


def load_listings():
    raw = os.environ.get("ICAL_URLS", "").strip()
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
            print(f"skip malformed line: {line!r}", file=sys.stderr)
            continue
        out.append((parts[0], parts[1], parts[2]))
    return out


def _norm_date(v):
    v = v.strip()
    if "T" in v:
        v = v.split("T", 1)[0]
    if len(v) == 8 and v.isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return v


def parse_ical(text):
    """Return [(dtstart, dtend)] for VEVENTs that are real reservations.

    Airbnb marks real bookings with SUMMARY 'Reserved'. Auto blocks (booking
    cutoff etc.) use SUMMARY 'Airbnb (Not available)' and are skipped.
    """
    events = []
    cur = {}
    in_event = False
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {}
        elif line == "END:VEVENT":
            in_event = False
            s = cur.get("DTSTART")
            e = cur.get("DTEND")
            summ = cur.get("SUMMARY", "").strip().lower()
            if s and e and summ.startswith("reserved"):
                events.append((s, e))
        elif in_event and ":" in line:
            key, _, val = line.partition(":")
            key = key.split(";", 1)[0]
            if key in ("DTSTART", "DTEND"):
                cur[key] = _norm_date(val)
            elif key == "SUMMARY":
                cur["SUMMARY"] = val
    return events


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "minpaku-cal/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def main():
    listings = load_listings()
    if not listings:
        print("ERROR: ICAL_URLS is empty or malformed", file=sys.stderr)
        sys.exit(1)

    result = {
        "generated_utc": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "rooms": {},
        "errors": [],
    }

    for room, lid, token in listings:
        url = f"https://www.airbnb.jp/calendar/ical/{lid}.ics?t={token}"
        try:
            text = fetch(url)
            ev = sorted(set(parse_ical(text)))
            result["rooms"][room] = [{"in": s, "out": e} for s, e in ev]
        except Exception as ex:  # noqa: BLE001 - want any failure recorded, not fatal
            result["rooms"][room] = None
            result["errors"].append(f"{room}: {type(ex).__name__}: {ex}")

    with open("calendar.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
        f.write("\n")

    ok = sum(1 for v in result["rooms"].values() if v is not None)
    print(f"wrote calendar.json: {ok}/{len(listings)} feeds ok, {len(result['errors'])} errors")
    for e in result["errors"]:
        print("  " + e, file=sys.stderr)
    # Fail the job only if EVERY feed failed (keeps a stale-but-valid file otherwise)
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
