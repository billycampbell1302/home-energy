"""Home tab money card: what gas and electricity cost a day, this month's likely bill, and what's been saved
since the app started.

  Per day      average of the last 30 days of smart-meter data, at your current prices incl. standing charges
  This month   gas scaled to the weather: gas use is fitted against how cold each day was (degree days), then
               applied to this month's actual + forecast temperatures; electricity at its daily average
  Saved        days since the app started vs what the same days would have cost before it, at today's prices
               (so price changes don't count). Gas is weather-adjusted: a colder week isn't counted as a loss.
               Until meter data covers days since the start, it shows the smart features' own estimate.
"""
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import db

BASE = 15.5          # degree-day base temperature (°C)


def _days() -> dict:
    """date -> {"day": kWh, "night": kWh, "gas": kWh} for days with electricity data (zero electricity = missing)."""
    from .bright import tariff_settings, _in_window
    t = tariff_settings()
    with db._lock:
        rows = db._conn.execute("SELECT ts, fuel, kwh FROM energy WHERE ts>=?", (time.time() - 400 * 86400,)).fetchall()
    have, out = set(), defaultdict(lambda: {"day": 0.0, "night": 0.0, "gas": 0.0, "n": 0})
    for ts, fuel, kwh in rows:
        if fuel == "electricity" and kwh > 0:
            have.add(ts)
    for ts, fuel, kwh in rows:
        if ts not in have:
            continue
        d = out[datetime.fromtimestamp(ts).date()]
        if fuel == "electricity":
            d["night" if _in_window(ts, t["night_start"], t["night_end"]) else "day"] += kwh
            d["n"] += 1
        elif fuel == "gas":
            d["gas"] += kwh
    return {k: v for k, v in out.items() if v["n"] >= 40}        # at least 20 hours of the day


def _mean_temps() -> dict:
    """date -> mean outdoor °C (weather history, topped up from the live readings)."""
    from . import backfill  # noqa: F401  (creates the weather_history table)
    acc = defaultdict(list)
    with db._lock:
        for ts, t in db._conn.execute("SELECT ts, outdoor_temp FROM weather_history WHERE outdoor_temp IS NOT NULL"):
            acc[datetime.fromtimestamp(ts).date()].append(t)
        for ts, t in db._conn.execute("SELECT ts, outdoor_temp FROM readings WHERE outdoor_temp IS NOT NULL AND ts>=?",
                                      (time.time() - 60 * 86400,)):
            acc[datetime.fromtimestamp(ts).date()].append(t)
    return {d: statistics.mean(v) for d, v in acc.items() if len(v) >= 12}


def _fit(points):
    """Least squares y = a + b·x."""
    if len(points) < 15:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return (my - b * mx, max(0.0, b))


def _cost(d: dict, t: dict) -> tuple:
    elec = (d["day"] * t["day_rate"] + d["night"] * t["night_rate"] + t["e7_standing"]) / 100
    gas = (d["gas"] * t["gas_rate"] + t["gas_standing"]) / 100
    return elec, gas


def summary() -> dict:
    from .bright import tariff_settings
    from . import thermal
    t = tariff_settings()
    days = _days()
    if not days:
        return {"ready": False}
    last = max(days)
    recent = [d for d in days if d > last - timedelta(days=30)]
    e_day = statistics.mean(_cost(days[d], t)[0] for d in recent)
    g_day = statistics.mean(_cost(days[d], t)[1] for d in recent)

    # Gas vs weather
    temps = _mean_temps()
    start = date.fromtimestamp(time.time() - thermal.learning_days() * 86400)
    before = [(max(0, BASE - temps[d]), days[d]["gas"]) for d in days if d in temps and d < start]
    fit = _fit(before)

    # This month's likely bill: actual temperatures so far + forecast/recent for the rest
    today = date.today()
    first = today.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    n_days = (nxt - first).days
    month_gas = g_day * n_days
    how = "average of your last 30 days of meter data"
    if fit:
        recent_t = [temps[d] for d in temps if today - timedelta(days=7) <= d < today]
        fill = statistics.mean(recent_t) if recent_t else None
        kwh = 0.0
        for i in range(n_days):
            d = first + timedelta(days=i)
            m = temps.get(d, fill)
            if m is None:
                break
            kwh += fit[0] + fit[1] * max(0, BASE - m)
        else:
            month_gas = kwh * t["gas_rate"] / 100 + n_days * t["gas_standing"] / 100
            how = "gas adjusted for this month's weather"
    month = e_day * n_days + month_gas

    # Saved since the app started (meter-measured, weather-adjusted, at today's prices)
    since = [d for d in days if d >= start]
    pre = [d for d in days if start - timedelta(days=60) <= d < start]
    saved = None
    if since and pre and fit:
        base_e = statistics.mean(_cost(days[d], t)[0] for d in pre)
        actual = expected = 0.0
        for d in since:
            e, g = _cost(days[d], t)
            actual += e + g
            hdd = max(0, BASE - temps[d]) if d in temps else None
            exp_g = ((fit[0] + fit[1] * hdd) * t["gas_rate"] + t["gas_standing"]) / 100 if hdd is not None else g
            expected += base_e + exp_g
        saved = {"amount": round(expected - actual, 2), "days": len(since), "measured": True,
                 "per_month": round((expected - actual) / len(since) * 30.4, 2)}
    if saved is None:
        est = thermal.report(max(1, int(thermal.learning_days()) + 1))
        saved = {"amount": est["saved_total"], "days": int(thermal.learning_days()) + 1, "measured": False}

    return {"ready": True, "gas_day": round(g_day, 2), "elec_day": round(e_day, 2), "month": round(month, 0),
            "month_label": today.strftime("%B"), "how": how, "data_to": last.strftime("%d %b"),
            "stale": (today - last).days > 3, "since": start.strftime("%d %b"), "saved": saved}
