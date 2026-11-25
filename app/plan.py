"""The week ahead: what the heating plan would be from what's been learned, and why.

For each day, every 30-minute slot gets a "warm" or "low" decision:
  • someone usually home then?      - from the 5-minute readings on the same weekday (or weekdays/weekends)
  • kids usually up by then?         - from the Fire Stick / tablets / lights each morning
  • asleep?                          - 22:30 until the morning (kept low unless people prefer otherwise)
  • how warm                         - each person's learned comfort for that part of the day, together = compromise
Then the warm blocks are adjusted with the flat's learned heat-up / heat-loss:
  • start early by the time it takes to warm up at the forecast outdoor temperature (Early-On)
  • end early by how long the flat stays within ½° with the heating off (coasting)
It's shown next to the current Hive schedule. Nothing is sent to Hive from here - this is the preview that
phase 3 will run once the learning phase is done.
"""
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests

from . import db
from .config import LAT, LON, PEOPLE

LOW = 16.0                 # setback when out or asleep
BED = 22.5                 # 22:30
SLOT = 30                  # minutes
_wx = {"ts": 0, "temps": {}}


def _forecast() -> dict:
    """{ 'YYYY-MM-DDTHH:00': °C } for the next 7 days (Met Office UKV where available)."""
    if time.time() - _wx["ts"] < 3600 and _wx["temps"]:
        return _wx["temps"]
    for model in ("ukmo_seamless", None):
        try:
            params = {"latitude": LAT, "longitude": LON, "hourly": "temperature_2m", "forecast_days": 7, "timezone": "Europe/London"}
            if model:
                params["models"] = model
            j = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10).json()
            h = j["hourly"]
            temps = {t: v for t, v in zip(h["time"], h["temperature_2m"]) if v is not None}
            if temps:
                _wx.update(ts=time.time(), temps=temps)
                return temps
        except Exception:
            continue
    return _wx["temps"]


def _slot(d: datetime) -> int:
    return (d.hour * 60 + d.minute) // SLOT


def _occupancy() -> dict:
    """Per weekday and half hour, from the last 4 weeks, only the SAME weekday counts:
       (weekday, slot) -> {"days": set(dates), "n": samples, "Billy": hits, "Katie": hits, "both": hits, "empty": hits,
                           "kids": hits, "kids_n": samples}"""
    from .routine import _presence_events
    from .daytypes import excluded_set
    skip = excluded_set()
    since = time.time() - 28 * 86400
    acc = defaultdict(lambda: defaultdict(int))
    days = defaultdict(set)
    # Each person: replay their arrive/leave events (only from their first event - before that we don't know)
    ev = _presence_events(since - 86400)
    now = time.time()
    for person in PEOPLE:
        mine = [(ts, home) for ts, pp, home in ev if pp == person]
        if not mine:
            continue
        i, state = 0, mine[0][1]
        t = mine[0][0]
        while t < now:
            while i < len(mine) and mine[i][0] <= t:
                state = mine[i][1]
                i += 1
            d = datetime.fromtimestamp(t)
            if t >= since and d.date().isoformat() not in skip:
                k = (d.weekday(), _slot(d))
                acc[k][person + "_n"] += 1
                acc[k][person] += int(state)
            t += 300
    # Whole household: from the 5-minute readings
    with db._lock:
        rows = db._conn.execute("SELECT ts, people_home, kids_lights_on, hold, extra FROM readings WHERE ts>=?", (since,)).fetchall()
    for ts, ph, other, hold, extra in rows:
        d = datetime.fromtimestamp(ts)
        if d.date().isoformat() in skip:
            continue
        k = (d.weekday(), _slot(d))
        a = acc[k]
        days[k].add(d.date())
        a["n"] += 1
        a["both"] += int((ph or 0) >= len(PEOPLE))
        a["empty"] += int(not ph and not other and not hold)
        try:
            e = json.loads(extra) if extra else {}
        except ValueError:
            e = {}
        if "kids_active" in e:
            a["kids_n"] += 1
            a["kids"] += int(bool(e["kids_active"]))
    return {"acc": acc, "days": days}


def _pct(a: dict, key: str, n_key: str = "n"):
    n = a.get(n_key, 0)
    return round(100 * a.get(key, 0) / n) if n else None


def _fmt(mins: float) -> str:
    mins = int(round(mins)) % (24 * 60)
    return f"{mins // 60:02d}:{mins % 60:02d}"


def day_plan(day: datetime, occ: dict, temps: dict) -> dict:
    from . import comfort, thermal
    m = thermal.model()
    wake = thermal.wake_time(day)
    sched = [(s, t) for s, t in thermal._schedule_slots(day) if s.date() == day.date()]
    from .daytypes import planned_as
    wd, special = planned_as(day.date())
    plural = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"][wd]
    acc, days = occ["acc"], occ["days"]
    seen_days = set().union(*[days.get((wd, i), set()) for i in range(48)]) if days else set()

    # 1. Each half hour: no data / asleep / warm / low
    slots = []
    for i in range(24 * 60 // SLOT):
        h = i * SLOT / 60
        a = acc.get((wd, i), {})
        asleep = h >= BED or h < (wake if wake is not None else 6.5)
        if not a.get("n"):
            state = "nodata"
        elif asleep:
            state = "night"
        else:
            empty = a["empty"] / a["n"]
            state = "warm" if empty < 0.5 else "low"
        slots.append((state, a))

    # 2. Group runs of the same state into blocks and average who was in
    blocks = []
    for i, (state, a) in enumerate(slots):
        if blocks and blocks[-1]["state"] == state:
            blocks[-1]["end"] = (i + 1) * SLOT
            blocks[-1]["_slots"].append(a)
        else:
            blocks.append({"state": state, "start": i * SLOT, "end": (i + 1) * SLOT, "_slots": [a]})

    for b in blocks:
        tot = defaultdict(int)
        for a in b.pop("_slots"):
            for k, v in a.items():
                tot[k] += v
        b["from"], b["to"] = _fmt(b["start"]), _fmt(b["end"])
        b["days"] = len(set().union(*[days.get((wd, i), set()) for i in range(b["start"] // SLOT, b["end"] // SLOT)]))
        who = {p: _pct(tot, p, p + "_n") for p in PEOPLE}
        b["who"] = who
        b["kids"] = _pct(tot, "kids", "kids_n")
        b["all_in"], b["empty"] = _pct(tot, "both"), _pct(tot, "empty")
        b["warm"] = b["state"] == "warm"
        b["why"] = []
        if b["state"] == "nodata":
            b["target"] = None
            b["why"].append(f"No data for {plural} at this time yet")
            continue
        if b["state"] == "night":
            b["target"] = LOW
            b["why"].append("Night – everyone asleep" + (f" (kids usually up {_fmt(wake * 60)})" if wake is not None and b["start"] < 12 * 60
                                                         else " (kids' wake-up time still being learned)" if b["start"] < 12 * 60 else ""))
            continue
        if not b["warm"]:
            b["target"] = LOW
            b["why"].append(f"Flat empty {b['empty']}% of the time")
            continue
        # Warm: aim for the comfort of whoever is usually in
        usual = [p for p, v in who.items() if v is not None and v >= 50] or list(PEOPLE)
        c = comfort.for_people(usual, hour=int(b["start"] / 60))
        b["target"] = c["temp"]
        taps = lambda p: c["per_person"][p]["samples"]
        parts = [f"{p} {c['per_person'][p]['temp']}°" + ("" if c["per_person"][p]["confident"] else f" (learning, {taps(p)} so far)") for p in usual]
        b["why"].append(f"Aiming {c['temp']}° – what {' and '.join(usual)} find{'s' if len(usual) == 1 else ''} comfortable "
                        f"this {c['period']}: " + ", ".join(parts))
        if wake is not None and abs(b["start"] / 60 - wake) < 0.6:
            b["why"].append(f"Kids usually up by {_fmt(wake * 60)}")
        key = (day + timedelta(minutes=b["start"])).strftime("%Y-%m-%dT%H:00")
        out = temps.get(key)
        if out is not None and m.get("confident"):
            lead = thermal.heat_minutes(LOW + 1, b["target"], out, m)
            if lead:
                b["preheat_min"] = int(min(lead, 120))
                b["why"].append(f"Starts {b['preheat_min']} min early – takes that long to warm up at {out:.0f}° outside")
            end_key = (day + timedelta(minutes=b["end"])).strftime("%Y-%m-%dT%H:00")
            co = thermal.coast_minutes(b["target"], b["target"] - 0.5, temps.get(end_key, out), m)
            if co:
                b["coast_min"] = int(min(co, 60))
                b["why"].append(f"Goes down {b['coast_min']} min before the end – the flat holds its heat")
        elif not m.get("confident"):
            b["why"].append("Warm-up time still being learned – no early start yet")
        b["heat_from"] = _fmt(b["start"] - b.get("preheat_min", 0))
        b["heat_to"] = _fmt(b["end"] - b.get("coast_min", 0))

    warm_hours = sum((b["end"] - b["start"] + b.get("preheat_min", 0) - b.get("coast_min", 0)) / 60 for b in blocks if b["warm"])
    hive_blocks = []
    for i, (st, t) in enumerate(sched):
        e = sched[i + 1][0] if i + 1 < len(sched) else st.replace(hour=23, minute=59)
        hive_blocks.append({"from": f"{st:%H:%M}", "to": f"{e:%H:%M}", "target": t,
                            "start": st.hour * 60 + st.minute, "end": e.hour * 60 + e.minute})
    hive_hours = sum((b["end"] - b["start"]) / 60 for b in hive_blocks if b["target"] > LOW + 0.5)
    known = sum(b["end"] - b["start"] for b in blocks if b["state"] != "nodata") / 60
    lows = [v for k, v in temps.items() if k.startswith(day.strftime("%Y-%m-%d"))]
    return {"date": day.strftime("%Y-%m-%d"), "label": day.strftime("%a %d"), "plural": plural, "blocks": blocks, "special": special,
            "hive": hive_blocks, "warm_hours": round(warm_hours, 1), "hive_hours": round(hive_hours, 1),
            "days_seen": len(seen_days), "known_hours": round(known, 1), "people": list(PEOPLE),
            "outdoor_min": min(lows) if lows else None, "outdoor_max": max(lows) if lows else None}


def week() -> dict:
    from . import thermal
    occ, temps = _occupancy(), _forecast()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    # (each day uses only what was seen on that same weekday)
    days = [day_plan(today + timedelta(days=i), occ, temps) for i in range(7)]
    return {"days": days, "learning_days": round(thermal.learning_days(), 1), "learning_target": thermal.LEARNING_DAYS,
            "model_confident": bool(thermal.model().get("confident")), "low": LOW,
            "active": thermal.learning_done(), "recent": __import__("app.daytypes", fromlist=["x"]).recent()}
