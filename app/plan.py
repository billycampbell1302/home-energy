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


def _occupancy() -> dict:
    """(weekday, slot) -> [fraction of readings with someone in, number of days seen]; plus weekday/weekend groups."""
    with db._lock:
        rows = db._conn.execute("SELECT ts, people_home, kids_lights_on, hold FROM readings WHERE ts>=?",
                                (time.time() - 28 * 86400,)).fetchall()
    acc = defaultdict(lambda: [0, 0, set()])
    for ts, p, k, h in rows:
        d = datetime.fromtimestamp(ts)
        slot = (d.hour * 60 + d.minute) // SLOT
        occ = 1 if (p or k or h) else 0
        for key in ((d.weekday(), slot), ("we" if d.weekday() >= 5 else "wd", slot)):
            a = acc[key]
            a[0] += occ
            a[1] += 1
            a[2].add(d.date())
    return {k: (v[0] / v[1], len(v[2])) for k, v in acc.items()}


def _p_home(occ: dict, day: datetime, slot: int):
    for key in ((day.weekday(), slot), ("we" if day.weekday() >= 5 else "wd", slot)):
        if key in occ and occ[key][1] >= 2:
            p, n = occ[key]
            return p, n, key[0] if isinstance(key[0], str) else "same"
    return None, 0, None


def _fmt(mins: float) -> str:
    mins = int(round(mins)) % (24 * 60)
    return f"{mins // 60:02d}:{mins % 60:02d}"


def day_plan(day: datetime, occ: dict, temps: dict) -> dict:
    from . import comfort, thermal
    m = thermal.model()
    wake = thermal.wake_time(day)
    sched = [(s, t) for s, t in thermal._schedule_slots(day) if s.date() == day.date()]
    dayname = day.strftime("%A")

    # 1. Warm or low for each half hour, with the reason
    slots = []
    for i in range(24 * 60 // SLOT):
        h = i * SLOT / 60
        p, n, src = _p_home(occ, day, i)
        asleep = h >= BED or h < (wake if wake is not None else 6.5)
        if p is None:
            # nothing learned yet for this time - follow the Hive schedule
            hive_t = next((t for s, t in reversed(sched) if s.hour + s.minute / 60 <= h), sched[-1][1] if sched else LOW)
            warm = hive_t > LOW + 0.5 and not asleep
            why = "not enough data yet – following your Hive schedule"
        else:
            warm = p >= 0.5 and not asleep
            who = f"{'' if src == 'same' else ('weekdays' if src == 'wd' else 'weekends')}"
            if asleep:
                why = "night – everyone asleep"
            elif warm:
                why = f"usually someone home ({p * 100:.0f}% of {dayname + 's' if not who else who})"
            else:
                why = f"flat usually empty ({(1 - p) * 100:.0f}% of {dayname + 's' if not who else who})"
        target = comfort.for_people(list(PEOPLE), hour=int(h))["temp"] if warm else LOW
        slots.append({"warm": warm, "target": target, "why": why})

    # 2. Group into blocks
    blocks = []
    for i, sl in enumerate(slots):
        if blocks and blocks[-1]["warm"] == sl["warm"] and blocks[-1]["target"] == sl["target"]:
            blocks[-1]["end"] = (i + 1) * SLOT
            if sl["why"] not in blocks[-1]["why"]:
                blocks[-1]["why"].append(sl["why"])
        else:
            blocks.append({"start": i * SLOT, "end": (i + 1) * SLOT, "warm": sl["warm"], "target": sl["target"], "why": [sl["why"]]})

    # 3. Reasons that apply to the whole warm block, and Early-On / coasting
    for b in blocks:
        if not b["warm"]:
            continue
        sh = b["start"] / 60
        if wake is not None and abs(sh - wake) < 0.6:
            b["why"].insert(0, f"kids usually up by {_fmt(wake * 60)}")
        c = comfort.for_people(list(PEOPLE), hour=int(sh))
        per = ", ".join(f"{p} {e['temp']}°" for p, e in c["per_person"].items())
        b["why"].append(f"comfortable {c['period']}: {per} → {c['temp']}° together"
                        + ("" if c["confident"] else " (still learning – using 20° until it knows)"))
        key = (day.replace(hour=0, minute=0) + timedelta(minutes=b["start"])).strftime("%Y-%m-%dT%H:00")
        out = temps.get(key)
        if out is not None and m.get("confident"):
            lead = thermal.heat_minutes(LOW + 1, b["target"], out, m)
            if lead:
                lead = min(lead, 120)
                b["preheat_min"] = int(lead)
                b["why"].append(f"heating starts {lead} min early – the flat takes that long to warm up when it's {out:.0f}° outside")
            end_key = (day.replace(hour=0, minute=0) + timedelta(minutes=b["end"])).strftime("%Y-%m-%dT%H:00")
            co = thermal.coast_minutes(b["target"], b["target"] - 0.5, temps.get(end_key, out), m)
            if co:
                co = min(co, 60)
                b["coast_min"] = int(co)
                b["why"].append(f"switches down {co} min before the end – it stays within ½° that long")
        elif out is not None and out <= 2:
            b["why"].append(f"cold forecast ({out:.0f}° outside)")
        b["heat_from"] = _fmt(b["start"] - b.get("preheat_min", 0))
        b["heat_to"] = _fmt(b["end"] - b.get("coast_min", 0))
    for b in blocks:
        b["from"], b["to"] = _fmt(b["start"]), _fmt(b["end"])

    warm_hours = sum((b["end"] - b["start"] + b.get("preheat_min", 0) - b.get("coast_min", 0)) / 60 for b in blocks if b["warm"])
    hive_blocks = []
    for i, (s, t) in enumerate(sched):
        e = sched[i + 1][0] if i + 1 < len(sched) else s.replace(hour=23, minute=59)
        hive_blocks.append({"from": f"{s:%H:%M}", "to": f"{e:%H:%M}", "target": t,
                            "start": s.hour * 60 + s.minute, "end": e.hour * 60 + e.minute})
    hive_hours = sum((b["end"] - b["start"]) / 60 for b in hive_blocks if b["target"] > LOW + 0.5)
    lows = [v for k, v in temps.items() if k.startswith(day.strftime("%Y-%m-%d"))]
    return {"date": day.strftime("%Y-%m-%d"), "label": day.strftime("%a %d"), "blocks": blocks, "hive": hive_blocks,
            "warm_hours": round(warm_hours, 1), "hive_hours": round(hive_hours, 1),
            "outdoor_min": min(lows) if lows else None, "outdoor_max": max(lows) if lows else None}


def week() -> dict:
    from . import thermal
    occ, temps = _occupancy(), _forecast()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    days = [day_plan(today + timedelta(days=i), occ, temps) for i in range(7)]
    return {"days": days, "learning_days": round(thermal.learning_days(), 1), "learning_target": thermal.LEARNING_DAYS,
            "model_confident": bool(thermal.model().get("confident")), "low": LOW,
            "active": thermal.learning_done()}
