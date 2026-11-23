"""The household's routine, as learned from the data: when each person is usually in or out,
when the flat is empty, and when the heating actually runs. Grids are 7 days (Mon..Sun) x 24 hours,
each cell the fraction of time (0-1), or None if there's no data for that slot yet.
"""
import json
import time
from datetime import datetime

from . import db
from .config import PEOPLE

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
STEP = 15 * 60


def _empty():
    return [[[0, 0] for _ in range(24)] for _ in range(7)]   # [hits, samples]


def _finish(acc):
    return [[round(h / n, 2) if n else None for h, n in row] for row in acc]


def _presence_events(since):
    with db._lock:
        rows = db._conn.execute("SELECT ts, detail FROM events WHERE kind='presence' AND ts >= ? ORDER BY ts", (since,)).fetchall()
    out = []
    for ts, d in rows:
        try:
            j = json.loads(d)
            out.append((ts, j["person"], bool(j["home"])))
        except Exception:
            pass
    return out


def person_grid(person: str, events: list, now: int):
    """Replays the arrive/leave events into 15-minute samples (only from the first event onwards - before that we don't know)."""
    mine = [(ts, home) for ts, p, home in events if p == person]
    acc = _empty()
    if not mine:
        return _finish(acc), 0, len(mine)
    t, i, state = mine[0][0], 0, mine[0][1]
    while t < now:
        while i + 1 < len(mine) and mine[i + 1][0] <= t:
            i += 1
            state = mine[i][1]
        lt = datetime.fromtimestamp(t)
        cell = acc[lt.weekday()][lt.hour]
        cell[0] += int(state)
        cell[1] += 1
        t += STEP
    return _finish(acc), (now - mine[0][0]) / 86400, len(mine)


def readings_grid(since: int, test):
    acc = _empty()
    with db._lock:
        rows = db._conn.execute(
            "SELECT ts, boiler_on, people_home, kids_lights_on, hold FROM readings WHERE ts >= ?", (since,)).fetchall()
    for ts, boiler, people, kids, hold in rows:
        v = test(boiler, people, kids, hold)
        if v is None:
            continue
        lt = datetime.fromtimestamp(ts)
        acc[lt.weekday()][lt.hour][0] += int(v)
        acc[lt.weekday()][lt.hour][1] += 1
    return _finish(acc)


def _ranges(hours_on: list) -> str:
    """[True, False, ...] for 24 hours -> '08:00–17:00 and 19:00–20:00'."""
    out, start = [], None
    for h in range(25):
        on = h < 24 and hours_on[h]
        if on and start is None:
            start = h
        if not on and start is not None:
            out.append(f"{start:02d}:00–{h:02d}:00" if h < 24 else f"{start:02d}:00–midnight")
            start = None
    return " and ".join(out)


def describe(grid, what_true: str, what_false: str, min_days: int = 3, threshold: float = 0.5) -> list:
    """Turns a grid into sentences for weekdays and weekends, e.g. 'Weekdays: usually out 08:00–17:00'."""
    lines = []
    for label, days in (("Weekdays", range(0, 5)), ("Weekends", range(5, 7))):
        cols = []
        for h in range(24):
            vals = [grid[d][h] for d in days if grid[d][h] is not None]
            cols.append(sum(vals) / len(vals) if vals else None)
        known_days = sum(1 for d in days if any(v is not None for v in grid[d]))
        if known_days < min(min_days, len(days)) or all(c is None for c in cols):
            lines.append(f"{label}: still learning")
            continue
        false_hours = [c is not None and c < threshold for c in cols]
        if not any(false_hours):
            lines.append(f"{label}: usually {what_true} all day")
        else:
            lines.append(f"{label}: usually {what_false} {_ranges(false_hours)}")
    return lines


def build(weeks: int = 4) -> dict:
    now = int(time.time())
    since = now - weeks * 7 * 86400
    events = _presence_events(since)
    people = []
    for p in PEOPLE:
        grid, days, n = person_grid(p, events, now)
        people.append({"name": p, "grid": grid, "days": round(days, 1), "events": n,
                       "summary": describe(grid, "in", "out") if n else ["No arrivals/departures recorded yet"]})
    empty = readings_grid(since, lambda b, ppl, k, hold: None if ppl is None else (ppl == 0 and not k and not hold))
    heating = readings_grid(since, lambda b, ppl, k, hold: None if b is None else bool(b))
    with db._lock:
        first = db._conn.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
        n_readings = db._conn.execute("SELECT COUNT(*) FROM readings WHERE ts >= ?", (since,)).fetchone()[0]
    return {
        "days": DAYS,
        "people": people,
        "empty": {"grid": empty, "summary": describe([[None if v is None else 1 - v for v in row] for row in empty],
                                                     "occupied", "empty")},
        # The boiler cycles on and off, so an hour where it fires 20%+ of the time counts as "heating on".
        "heating": {"grid": heating, "summary": describe([[None if v is None else 1 - v for v in row] for row in heating],
                                                         "off", "on", threshold=0.8)},
        "collected": {"since": first, "readings": n_readings,
                      "presence_events": len(events)},
    }
