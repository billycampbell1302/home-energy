"""Bedtime and getting-up time, from each adult's iPhone Sleep Focus (accurate - it's when your phone goes to sleep).

An iPhone Shortcuts automation calls /hook/sleep when Sleep Focus turns on and off. Learned separately for
weekdays and weekends (untypical days left out). Until there are 3 nights, the plan assumes 22:30.
"""
import json
import statistics
import time
from datetime import datetime, timedelta

from . import db
from .config import PEOPLE

DEFAULT_BED = 22.5


def record(person: str, asleep: bool):
    db.add_event("sleep", {"person": person, "asleep": asleep})
    st = db.get_state("sleep_now", {})
    st[person] = {"asleep": asleep, "since": int(time.time())}
    db.set_state("sleep_now", st)


def asleep_now() -> dict:
    return {p: v["asleep"] for p, v in db.get_state("sleep_now", {}).items()}


def _times(asleep: bool, weekend_night: bool) -> list:
    """Hours (bedtime as 18-30 so after-midnight sorts right; wake as 0-14) for the last 4 weeks."""
    from .daytypes import excluded_set
    skip = excluded_set()
    with db._lock:
        rows = db._conn.execute("SELECT ts, detail FROM events WHERE kind='sleep' AND ts>=? ORDER BY ts",
                                (time.time() - 28 * 86400,)).fetchall()
    per_night = {}
    for ts, d in rows:
        d = json.loads(d)
        if d.get("asleep") != asleep:
            continue
        t = datetime.fromtimestamp(ts)
        if asleep:                                       # bedtime: belongs to the evening it started
            night = (t - timedelta(hours=12)).date()
            h = t.hour + t.minute / 60 + (24 if t.hour < 12 else 0)
            if not 18 <= h <= 30:
                continue
            weekend = night.weekday() in (4, 5)          # Fri & Sat nights
        else:                                            # getting up: that morning
            night = t.date()
            h = t.hour + t.minute / 60
            if not 4 <= h <= 13:
                continue
            weekend = night.weekday() >= 5
        if weekend != weekend_night or night.isoformat() in skip:
            continue
        per_night.setdefault(night, {}).setdefault(d["person"], h)      # each person's first that night/morning
    out = []
    for night, people in per_night.items():
        out.append(max(people.values()) if asleep else min(people.values()))
    return out


def bedtime(day: datetime) -> tuple:
    """(hour, nights learned) - when the last adult usually goes to sleep that night."""
    hs = _times(True, day.weekday() in (4, 5))
    return (round(statistics.median(hs), 2) if len(hs) >= 3 else DEFAULT_BED), len(hs)


def getting_up(day: datetime) -> tuple:
    hs = _times(False, day.weekday() >= 5)
    return (round(sorted(hs)[len(hs) // 4], 2) if len(hs) >= 3 else None), len(hs)
