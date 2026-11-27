"""Bedtime and getting-up time, worked out automatically from when the flat goes quiet at night and wakes up
in the morning - no Sleep Focus (that runs on a fixed schedule, not when you actually sleep).

Every 5 minutes the reading records whether there's any sign of someone awake:
  • a phone in use (a phone on the home Wi-Fi moving data like browsing/scrolling - big steady transfers
    like overnight iCloud backups are ignored)
  • a Fire Stick streaming
  • a light switched or changed, the kids up and about, a heating change
Bedtime = the last sign of life in the evening followed by 90+ quiet minutes.
Getting up = the first sign of life in the morning after 3+ quiet hours.
Learned separately for weeknights and Friday/Saturday nights (median of the last 4 weeks, untypical days left
out). Until there are 3 nights, the plan assumes 22:30.
"""
import json
import statistics
import time
from datetime import datetime, timedelta

from . import db
from .config import PEOPLE

DEFAULT_BED = 22.5
PHONE_MIN_KBPS = 40          # someone using the phone
BACKUP_KBPS = 4000           # sustained heavy transfer = backup/update, not a person


def activity_now() -> dict:
    """What's showing signs of someone awake right now (stored with each 5-minute reading)."""
    from . import router
    devs, roles = db.get_state("router_devices", {}), db.get_state("router_roles", {})
    phones = {}
    for mac, role in roles.items():
        if role.startswith("person:"):
            kbps = round(devs.get(mac, {}).get("bps", 0) * 8 / 1000)
            phones[role[7:]] = kbps
    phone_use = any(PHONE_MIN_KBPS <= v < BACKUP_KBPS for v in phones.values())
    try:
        streaming = bool(router.streaming())
    except Exception:
        streaming = False
    act = db.get_state("light_activity", {})
    lights = any(time.time() - t < 10 * 60 for t in act.values())
    own = time.time() - db.get_state("hive_last_own_change", 0) < 10 * 60
    manual = any(e["kind"] == "manual" and e["ts"] > time.time() - 600 for e in db.recent_events(10))
    return {"awake": bool(phone_use or streaming or lights or (manual and not own)),
            "phones": phones, "stream": streaming, "lights": lights}


def _nights(weekend_night: bool, mornings: bool = False) -> list:
    from .daytypes import excluded_set
    skip = excluded_set()
    since = time.time() - 28 * 86400
    with db._lock:
        rows = db._conn.execute("SELECT ts, people_home, extra FROM readings WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    seq = []
    for ts, ph, extra in rows:
        try:
            e = json.loads(extra) if extra else {}
        except ValueError:
            e = {}
        if "awake" in e:
            seq.append((ts, bool(e["awake"]) or bool(e.get("kids_active")), bool(ph)))
    out = []
    days = sorted({datetime.fromtimestamp(t).date() for t, _, _ in seq})
    for d in days:
        if mornings:
            weekend = d.weekday() >= 5
            if weekend != weekend_night or d.isoformat() in skip:
                continue
            a = datetime(d.year, d.month, d.day, 1).timestamp()
            win = [(t, aw, ph) for t, aw, ph in seq if a <= t < a + 12 * 3600]       # 01:00-13:00
            quiet_since = None
            for t, aw, ph in win:
                if not aw:
                    quiet_since = quiet_since or t
                    continue
                h = datetime.fromtimestamp(t).hour + datetime.fromtimestamp(t).minute / 60
                if quiet_since and t - quiet_since >= 3 * 3600 and 4 <= h <= 11 and ph:
                    out.append(h)
                    break
                quiet_since = None
        else:
            weekend = d.weekday() in (4, 5)
            if weekend != weekend_night or d.isoformat() in skip:
                continue
            a = datetime(d.year, d.month, d.day, 19).timestamp()
            win = [(t, aw, ph) for t, aw, ph in seq if a <= t < a + 9 * 3600]        # 19:00-04:00
            if len(win) < 80 or not any(ph for _, _, ph in win):
                continue
            last = None
            for i, (t, aw, ph) in enumerate(win):
                if aw:
                    quiet = [x for x in win[i + 1:] if x[0] <= t + 90 * 60]
                    if quiet and not any(x[1] for x in quiet) and quiet[-1][0] - t >= 80 * 60:
                        last = t
                        break
            if last:
                lt = datetime.fromtimestamp(last)
                out.append(lt.hour + lt.minute / 60 + (24 if lt.hour < 12 else 0))
    return out


def bedtime(day: datetime) -> tuple:
    """(hour, nights learned) - when the flat usually goes quiet that night (Fri/Sat learned separately)."""
    hs = _nights(day.weekday() in (4, 5))
    return (round(statistics.median(hs), 2) % 24 if len(hs) >= 3 else DEFAULT_BED), len(hs)


def getting_up(day: datetime) -> tuple:
    """(hour, mornings learned) - the first sign of life (the earlier mornings, so it's warm in time)."""
    hs = sorted(_nights(day.weekday() >= 5, mornings=True))
    return (round(hs[len(hs) // 4], 2) if len(hs) >= 3 else None), len(hs)
