"""Monthly report: what you used, what it cost, how the heating behaved - this month vs last month."""
import time
from datetime import date, datetime

from . import db
from .bright import tariff_settings, _in_window


def _month_bounds(y: int, m: int):
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), m % 12 + 1, 1)
    return int(start.timestamp()), int(end.timestamp())


def month(y: int, m: int) -> dict:
    s, e = _month_bounds(y, m)
    e = min(e, int(time.time()))
    t = tariff_settings()
    with db._lock:
        elec = db._conn.execute("SELECT ts, kwh FROM energy WHERE fuel='electricity' AND kwh>0 AND ts>=? AND ts<?", (s, e)).fetchall()
        have = {ts for ts, _ in elec}
        gas = [r for r in db._conn.execute("SELECT ts, kwh FROM energy WHERE fuel='gas' AND ts>=? AND ts<?", (s, e)).fetchall()
               if r[0] in have]
        rd = db._conn.execute("SELECT boiler_on, people_home, kids_lights_on, hold FROM readings WHERE ts>=? AND ts<?", (s, e)).fetchall()
    days = len({datetime.fromtimestamp(ts).date() for ts in have})
    night = sum(v for ts, v in elec if _in_window(ts, t["night_start"], t["night_end"]))
    total_e = sum(v for _, v in elec)
    cost_e = ((total_e - night) * t["day_rate"] + night * t["night_rate"]) / 100 + days * t["e7_standing"] / 100
    total_g = sum(v for _, v in gas)
    cost_g = total_g * t["gas_rate"] / 100 + days * t["gas_standing"] / 100
    step_h = 5 / 60
    boiler_h = sum(step_h for b, *_ in rd if b)
    empty_h = sum(step_h for b, ppl, k, h in rd if b and ppl == 0 and not k and not h)
    return {
        "label": datetime(y, m, 1).strftime("%B %Y"), "days_of_meter_data": days,
        "electricity_kwh": round(total_e, 1), "electricity_cost": round(cost_e, 2),
        "night_share": round(night / total_e, 3) if total_e else None,
        "gas_kwh": round(total_g, 1), "gas_cost": round(cost_g, 2),
        "boiler_hours": round(boiler_h, 1), "heating_empty_hours": round(empty_h, 1),
        "heating_days": round(len(rd) * step_h / 24, 1),
    }


def current_and_previous() -> dict:
    today = date.today()
    py, pm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return {"this": month(today.year, today.month), "last": month(py, pm)}


def summary_text(r: dict) -> str:
    parts = [f"{r['label']}:"]
    if r["days_of_meter_data"]:
        parts.append(f"electricity £{r['electricity_cost']:.0f}, gas £{r['gas_cost']:.0f}")
    if r["boiler_hours"]:
        parts.append(f"boiler ran {r['boiler_hours']:.0f}h")
        if r["heating_empty_hours"] >= 1:
            parts.append(f"{r['heating_empty_hours']:.0f}h with nobody home")
    return f"{parts[0]} " + " · ".join(parts[1:]) if len(parts) > 1 else f"{r['label']}: not enough data yet."
