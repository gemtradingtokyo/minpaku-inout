#!/usr/bin/env python3
"""Build the daily OUT/IN report text from calendar.json.

  python3 report.py [--mode morning|evening] [--now 2026-10-12T19:10]

Default mode: morning before 14:00 JST, evening otherwise.
evening: tomorrow's OUT/IN list + linen additions.
morning: changes since yesterday 17:00 JST (git history of calendar.json)
         that touch today's or tomorrow's IN/OUT.
First output line is "SUBJECT: ...", then a blank line, then the body.
"""
import argparse
import datetime
import json
import subprocess
import sys

JST = datetime.timezone(datetime.timedelta(hours=9))
WEEKDAYS = "月火水木金土日"
STALE_HOURS = 12
LINEN_DEFAULT = {r: 5 for r in "401U 403U 405U 501U 502U 503U 505U 601U 602U 605U 701U 702U".split()}
LINEN_DEFAULT["705U"] = 6
SIGN_EVENING = "以上となります。ご確認よろしくお願いします。"
SIGN_MORNING = "よろしくお願いいたします。"


def d2s(d):
    return d.isoformat()


def md(s):
    d = datetime.date.fromisoformat(s)
    return f"{d.month}/{d.day}"


def md_wd(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def span(b):
    i, o = datetime.date.fromisoformat(b["in"]), datetime.date.fromisoformat(b["out"])
    tail = f"{o.day}" if (i.year, i.month) == (o.year, o.month) else f"{o.month}/{o.day}"
    return f"{i.month}/{i.day}-{tail}"


def num(room):
    return room[:-1] if room.endswith("U") else room[1:]


def room_key(room):
    return (0, int(num(room))) if room.endswith("U") else (1, int(num(room)))


def building(room):
    return "上田ビル" if room.endswith("U") else "アークシティ"


def flatten(cal):
    out = []
    for room, bookings in (cal.get("rooms") or {}).items():
        for b in bookings or []:
            out.append({**b, "room": room})
    return out


def stale_note(cal, now):
    gen = cal.get("generated_utc")
    try:
        g = datetime.datetime.strptime(gen, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return f"※データが古い可能性（generated_utc: {gen}）"
    if now - g > datetime.timedelta(hours=STALE_HOURS):
        return f"※データが古い可能性（generated_utc: {gen}）"
    return None


def linen_items(in_bookings):
    extras, unknown = [], []
    for b in sorted(in_bookings, key=lambda b: room_key(b["room"])):
        default = LINEN_DEFAULT.get(b["room"])
        if default is None:
            continue
        g = b.get("guests")
        if not g:
            unknown.append(f"{num(b['room'])}（人数要確認）")
        elif g > default:
            extras.append(f"{num(b['room'])} +{g - default}")
    items = extras + unknown
    return "、".join(items) if items else "なし"


def linen_line(in_bookings):
    return "上田ビル：" + linen_items(in_bookings)


def ymd(s):
    return s.replace("-", "/")


def alert_blocks(cal):
    """One ★ line per Hostex/Airbnb desync, plus the rooms they concern.

    Hostex is what the cleaners work from, so a reservation Airbnb knows about
    and Hostex does not means no cleaning gets ordered. That goes at the top,
    not in a footnote.
    """
    chk = cal.get("airbnb_check") or {}
    missing = [m for m in chk.get("mismatches") or [] if m["kind"] == "missing_in_hostex"]
    extra = [m for m in chk.get("mismatches") or [] if m["kind"] == "missing_in_airbnb"]
    blocks, rooms = [], []
    for m in missing:
        # the same dates sitting on another room in Hostex means the room changed
        moved = next((e for e in extra if (e["in"], e["out"]) == (m["in"], m["out"])), None)
        span_s = f"{ymd(m['in'])} ~ {ymd(m['out'])}"
        rooms.append(m["room"])
        if moved:
            extra.remove(moved)
            blocks.append(
                f"★{m['room']}　{span_s} のAirbnb予約がHostexでは{moved['room']}のままです。ご確認ください。"
            )
        else:
            blocks.append(
                f"★{m['room']}　{span_s} のAirbnb予約がHostexに同期されていない可能性があります。ご確認ください。"
            )
    for e in extra:
        rooms.append(e["room"])
        blocks.append(
            f"★{e['room']}　{ymd(e['in'])} ~ {ymd(e['out'])} のHostexの予約がAirbnbにありません。ご確認ください。"
        )
    for err in chk.get("errors") or []:
        blocks.append(f"★Airbnbとの照合ができませんでした　{err}")
    return blocks, rooms


def alert_suffix(blocks, rooms):
    if rooms:
        return " ★" + "・".join(dict.fromkeys(rooms)) + " 要確認"
    return " ★照合エラー" if blocks else ""


def notes(cal, now):
    out = []
    s = stale_note(cal, now)
    if s:
        out.append(s)
    for room, v in sorted((cal.get("rooms") or {}).items()):
        if v is None:
            out.append(f"取得失敗: {room}（要目視）")
    for e in cal.get("errors") or []:
        out.append(f"※{e}")
    return out


def evening(cal, now):
    D = (now + datetime.timedelta(days=1)).date()
    ds = d2s(D)
    outs = [b for b in flatten(cal) if b["out"] == ds]
    ins = [b for b in flatten(cal) if b["in"] == ds]
    blocks, alert_rooms = alert_blocks(cal)

    lines = blocks + ["", ""] if blocks else []

    in_rooms = {b["room"] for b in ins}
    lines.append(f"■ {md_wd(D)} OUT")
    if outs:
        for b in sorted(outs, key=lambda b: room_key(b["room"])):
            lines.append(f"　{b['room']}" + ("　→ 同日IN" if b["room"] in in_rooms else ""))
    else:
        lines.append("　なし")
    lines.append("")

    lines.append(f"■ {md_wd(D)} IN")
    if ins:
        for b in sorted(ins, key=lambda b: room_key(b["room"])):
            g = b.get("guests")
            lines.append(f"　{b['room']}　{md(b['in'])} → {md(b['out'])}　" + (f"{g}名" if g else "人数要確認"))
    else:
        lines.append("　なし")
    lines.append("")
    lines += [f"　リネン追加：{linen_items(ins)}", ""]

    n = notes(cal, now)
    if n:
        lines += n + [""]
    lines.append(SIGN_EVENING)
    return f"【民泊】{md_wd(D)} OUT/IN確認{alert_suffix(blocks, alert_rooms)}", "\n".join(lines)


def load_baseline(now):
    y = (now - datetime.timedelta(days=1)).date()
    since = f"{d2s(y)} 17:00:00 +0900"
    sha = subprocess.run(
        ["git", "log", "-1", "--format=%H", f"--before={since}", "--", "calendar.json"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not sha:
        return None
    blob = subprocess.run(["git", "show", f"{sha}:calendar.json"], capture_output=True, text=True, check=True).stdout
    return json.loads(blob)


def ident(b):
    return (b["room"], b["in"], b["out"])


def diff(old, new):
    """Return (added, removed, changed[(old,new)]) between two booking lists."""
    old_c = {b["code"]: b for b in old if b.get("code")}
    new_c = {b["code"]: b for b in new if b.get("code")}
    added, removed, changed = [], [], []
    for c in new_c.keys() & old_c.keys():
        if ident(old_c[c]) != ident(new_c[c]):
            changed.append((old_c[c], new_c[c]))
    rest_old = [b for b in old if b.get("code") not in new_c or not b.get("code")]
    rest_new = [b for b in new if b.get("code") not in old_c or not b.get("code")]
    old_ids = {ident(b) for b in rest_old}
    new_ids = {ident(b) for b in rest_new}
    added = [b for b in rest_new if ident(b) not in old_ids]
    removed = [b for b in rest_old if ident(b) not in new_ids]
    return added, removed, changed


def touches(b, days):
    return b["in"] in days or b["out"] in days


def morning(cal, base, now):
    T = now.date()
    T1 = T + datetime.timedelta(days=1)
    days = {d2s(T), d2s(T1)}
    label = {d2s(T): "本日", d2s(T1): "明日"}
    blocks, alert_rooms = alert_blocks(cal)
    subject = f"【民泊】{md_wd(T)} 夜間変更報告{alert_suffix(blocks, alert_rooms)}"
    head = ["おはようございます。"]
    if blocks:
        head += [""] + [x for blk in blocks for x in (blk, "")]
    tail_notes = notes(cal, now)
    if base is None:
        body = head + ["前日17時時点のカレンダーが見つからず、夜間の変更を比較できませんでした。目視でご確認ください。", ""]
        body += tail_notes + ([""] if tail_notes else []) + [SIGN_MORNING]
        return subject, "\n".join(body)

    added, removed, changed = diff(flatten(base), flatten(cal))
    added = sorted((b for b in added if touches(b, days)), key=lambda b: (b["in"], room_key(b["room"])))
    removed = sorted((b for b in removed if touches(b, days)), key=lambda b: (b["in"], room_key(b["room"])))
    changed = sorted(
        ((o, n) for o, n in changed if touches(o, days) or touches(n, days)),
        key=lambda p: (p[1]["in"], room_key(p[1]["room"])),
    )

    if not (added or removed or changed):
        body = head + ["昨日の夜から今朝にかけて、今日と明日のinoutの追加・変更はありません。"]
        if tail_notes:
            body += [""] + tail_notes
        body.append(SIGN_MORNING)
        return subject, "\n".join(body)

    lines = list(head)
    if not (removed or changed):
        lines.append(f"昨日の夜から今朝にかけて、今日明日のinout追加{len(added)}件です。")
    else:
        lines.append("昨日の夜から今朝にかけて、以下の変更がありました。")
    lines.append("")
    if added:
        lines += [f"{span(b)} {b['room']}" for b in added]
        lines.append("")
    for b in removed:
        if b["in"] in days:
            lines.append(
                f"{label[b['in']]}の{b['room']} in がキャンセルになりました。"
                "（基本的に清掃は完了済みのため当日の影響はありませんが、今後の清掃スケジュールが変わるため共有します）"
            )
        else:
            lines.append(f"{b['room']} {span(b)} の予約がなくなりました。ご確認ください。")
    warn = []
    for o, n in changed:
        if o["room"] != n["room"] and (o["in"], o["out"]) == (n["in"], n["out"]):
            lines.append(f"{span(n)} の予約が {o['room']} → {n['room']} に部屋変更になりました。")
        elif o["room"] != n["room"]:
            lines.append(f"{o['room']} {span(o)} → {n['room']} {span(n)} に変更になりました。")
        else:
            lines.append(f"{n['room']} が {span(o)} → {span(n)} に変更になりました。")
        if n["out"] == d2s(T) and (o["room"], o["out"]) != (n["room"], n["out"]):
            warn.append(f"※本日 {n['room']} out になっています。ご確認ください。")
    if removed or changed:
        lines.append("")
    in_bookings = [b for b in added if b["in"] in days] + [n for o, n in changed if n["in"] in days]
    if in_bookings:
        lines += ["リネン追加", linen_line(in_bookings), ""]
    if warn:
        lines += warn + [""]
    if tail_notes:
        lines += tail_notes + [""]
    lines.append(SIGN_MORNING)
    return subject, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["morning", "evening"])
    ap.add_argument("--now", help="JST datetime for testing, e.g. 2026-10-12T19:10")
    a = ap.parse_args()
    now = (
        datetime.datetime.fromisoformat(a.now).replace(tzinfo=JST)
        if a.now
        else datetime.datetime.now(JST)
    )
    mode = a.mode or ("morning" if now.hour < 14 else "evening")
    with open("calendar.json", encoding="utf-8") as f:
        cal = json.load(f)
    if mode == "evening":
        subject, body = evening(cal, now)
    else:
        subject, body = morning(cal, load_baseline(now), now)
    sys.stdout.write(f"SUBJECT: {subject}\n\n{body}\n")


if __name__ == "__main__":
    main()
