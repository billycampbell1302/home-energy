"""Learns the temperature each person finds comfortable, and what works when you're both home.

Where the data comes from:
  - Tapping 🥶 / 👍 / 🥵 on the dashboard (it knows whose phone it is).
  - Heating changes made in the Hive app or on the thermostat: turning it up or boosting = "too cold at this
    temperature"; turning it down or cancelling a boost = "too warm". These count for whoever was home.

Each piece of evidence nudges a person's comfortable temperature. Recent evidence counts more (90-day half-life),
so it adapts to seasons and habits. Together = a compromise that never leaves the colder person more than
half a degree short, without heating to the warmer person's maximum.
"""
import math
import time

from . import db
from .config import PEOPLE

HALF_LIFE = 90 * 86400
# Comfort changes through the day - e.g. a bit cooler in the morning rush, warmer when relaxing in the evening.
PERIODS = {"morning": (5, 10), "day": (10, 17), "evening": (17, 22), "night": (22, 5)}


def period_of(hour: int) -> str:
    for name, (a, b) in PERIODS.items():
        if (a <= hour < b) if a < b else (hour >= a or hour < b):
            return name
    return "day"
DEFAULT = 20.0
MIN_T, MAX_T = 16.0, 23.0


def record(people: list, feeling: str, indoor: float | None, source: str, outdoor: float | None = None):
    """feeling: cold | ok | warm. `people` = whose comfort this tells us about."""
    if indoor is None or feeling not in ("cold", "ok", "warm") or not people:
        return
    from . import thermal
    if source == "hive" and thermal.sun_skewed():
        return                                     # thermostat reading is sun-warmed - not a real temperature
    if feeling == "cold":
        thermal.savings_feedback_cold()
    log = db.get_state("comfort_log", [])
    log.append({"ts": int(time.time()), "people": people, "feeling": feeling, "indoor": round(float(indoor), 1),
                "outdoor": outdoor, "source": source, "hour": time.localtime().tm_hour})
    db.set_state("comfort_log", log[-500:])
    db.add_event("comfort", {"who": ", ".join(people), "feeling": feeling, "indoor": indoor, "source": source})


def _implied(entry) -> float:
    """What each answer says about the comfortable temperature."""
    t = entry["indoor"]
    return {"cold": t + 0.75, "ok": t, "warm": t - 0.75}[entry["feeling"]]


def _estimate(entries: list, person: str, prior: float, prior_w: float):
    now = time.time()
    num = den = 0.0
    n = 0
    for e in entries:
        if person not in e["people"]:
            continue
        share = 1 / len(e["people"])                       # shared evidence counts less per person
        w = share * math.exp(-math.log(2) * (now - e["ts"]) / HALF_LIFE)
        if e["source"] == "dashboard":
            w *= 1.5                                       # a direct answer is the clearest signal
        num += w * _implied(e)
        den += w
        n += 1
    temp = (num + prior_w * prior) / (den + prior_w)
    return round(min(MAX_T, max(MIN_T, temp)) * 2) / 2, n


def estimates(period: str | None = None) -> dict:
    """person -> {"temp": °C, "samples": n, "confident": bool, "by_period": {...}}

    Overall comfort starts from 20° as a gentle prior so one tap can't swing it wildly. Each time-of-day
    period then starts from the person's overall figure and moves only as evidence for that period builds up.
    """
    log = db.get_state("comfort_log", [])
    out = {}
    for p in PEOPLE:
        overall, n = _estimate(log, p, DEFAULT, 1.0)
        by = {}
        for name in PERIODS:
            pe = [e for e in log if period_of(e.get("hour", 12)) == name]
            t, pn = _estimate(pe, p, overall, 1.5)
            by[name] = {"temp": t, "samples": pn}
        cur = by[period] if period else {"temp": overall, "samples": n}
        out[p] = {"temp": cur["temp"], "overall": overall, "samples": n, "confident": n >= 3, "by_period": by}
    return out


def for_people(home: list, hour: int | None = None) -> dict:
    """The temperature to aim for given who's home and the time of day."""
    period = period_of(time.localtime().tm_hour if hour is None else hour)
    est = estimates(period)
    who = [p for p in home if p in est] or list(est)
    temps = [est[p]["temp"] for p in who]
    if len(temps) == 1:
        t = temps[0]
    else:
        # Compromise: the average, but never more than 0.5° below what the coldest-feeling person wants.
        t = max(sum(temps) / len(temps), max(temps) - 0.5)
    from . import thermal
    off = thermal.savings_offset()
    t = max(18.0, t + off) if off else t
    return {"temp": round(t * 2) / 2, "savings_offset": off, "who": who, "period": period,
            "confident": all(est[p]["confident"] for p in who), "per_person": {p: est[p] for p in est}}
