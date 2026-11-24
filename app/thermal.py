"""How the flat heats up and cools down - the Nest-style smarts built on top of it.

Model (learned from the 5-minute readings, re-fitted every hour):
    change in indoor temp per hour = a × (boiler on)  −  k × (indoor − outdoor)  +  g × (sunshine / 100 W/m²)
  a = how fast the boiler warms the flat, k = how fast the flat loses heat, g = free heat from the sun.

Features built on it:
  Early-On      start the heating just early enough to be warm when the schedule says (not 40 minutes late).
  Coasting      the flat holds its heat, so drop to the next (lower) schedule temperature a little early.
  Open window   temperature falling fast while heating → pause heating for 20 minutes, then carry on.
  Heads-up      heating calling for an hour but the flat isn't warming → likely low boiler pressure / fault.
  Sunblock      sun on the thermostat makes it read warm → flagged and kept out of the learning.
  Savings nudge Nest's "Seasonal Savings": every 2 weeks with no "too cold" complaints, aim ½° lower (max 1°).
"""
import json
import math
import statistics
import time
from datetime import datetime, timedelta

from . import db

GAP = 11 * 60            # readings further apart than this break a run
FEATURES = {
    "early_on": "Early-On – warm by the time the schedule says",
    "coasting": "Coasting – switch down early, the flat holds its heat",
    "window": "Open-window pause",
    "savings": "Savings nudge – try ½° lower every 2 weeks if nobody's cold",
    "suggestions": "Heating-off suggestions from your routine",
}


LEARNING_DAYS = 14
PHASE3 = {"early_on", "coasting", "savings", "suggestions"}   # built now, switch on after the learning phase


def learning_days() -> float:
    with db._lock:
        first = db._conn.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
    return (time.time() - first) / 86400 if first else 0.0


def learning_done() -> bool:
    return learning_days() >= LEARNING_DAYS


def features() -> dict:
    saved = db.get_state("thermal_features", {})
    done = learning_done()
    return {k: saved.get(k, True) and (done or k not in PHASE3) for k in FEATURES}


def wanted() -> dict:
    """The switches as set (before the learning-phase lock)."""
    saved = db.get_state("thermal_features", {})
    return {k: saved.get(k, True) for k in FEATURES}


# ---------- learning ----------
def _rows(days: int = 60):
    with db._lock:
        rows = db._conn.execute(
            "SELECT ts, indoor_temp, boiler_on, outdoor_temp, extra FROM readings WHERE ts>=? AND indoor_temp IS NOT NULL "
            "AND outdoor_temp IS NOT NULL AND boiler_on IS NOT NULL ORDER BY ts", (time.time() - days * 86400,)).fetchall()
    out = []
    for ts, t, b, o, extra in rows:
        try:
            e = json.loads(extra) if extra else {}
        except ValueError:
            e = {}
        out.append({"ts": ts, "t": t, "on": bool(b), "out": o, "solar": e.get("solar") or 0, "wind": e.get("wind") or 0})
    return out


def _runs(rows):
    """Split readings into unbroken stretches of boiler-on / boiler-off."""
    runs, cur = [], []
    for r in rows:
        if cur and (r["on"] != cur[-1]["on"] or r["ts"] - cur[-1]["ts"] > GAP):
            runs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        runs.append(cur)
    return runs


def _slope(run):
    """Least-squares °C per hour."""
    xs = [(r["ts"] - run[0]["ts"]) / 3600 for r in run]
    ys = [r["t"] for r in run]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def learn() -> dict:
    rows = _rows()
    runs = _runs(rows)
    ks, ks_calm, ks_windy, sun = [], [], [], []
    for run in runs:
        if run[0]["on"]:
            continue
        run = [r for r in run if r["ts"] - run[0]["ts"] >= 20 * 60]    # radiators still warm for the first 20 min
        if len(run) < 9:
            continue
        dt = statistics.mean(r["t"] - r["out"] for r in run)
        if dt < 4:
            continue
        s = _slope(run)
        if s < -3:                                                      # window open - not normal heat loss
            continue
        solar = statistics.mean(r["solar"] for r in run)
        if solar < 50:
            k = -s / dt
            if 0 < k < 1:
                ks.append(k)
                (ks_windy if statistics.mean(r["wind"] for r in run) > 15 else ks_calm).append(k)
        else:
            sun.append((s, dt, solar))
    k = statistics.median(ks) if len(ks) >= 3 else None

    heats, lags = [], []
    if k:
        for run in runs:
            if not run[0]["on"] or len(run) < 4:
                continue
            hours = (run[-1]["ts"] - run[0]["ts"]) / 3600
            rise = run[-1]["t"] - run[0]["t"]
            if hours < 0.3 or rise <= 0 or rise / hours > 6:
                continue
            dt = statistics.mean(r["t"] - r["out"] for r in run)
            heats.append(rise / hours + k * dt)
            first = next((r for r in run if r["t"] >= run[0]["t"] + 0.2), None)
            if first:
                lags.append((first["ts"] - run[0]["ts"]) / 60)
    g = None
    if k and len(sun) >= 3:
        gains = [(s + k * dt) / (solar / 100) for s, dt, solar in sun]
        g = max(0.0, statistics.median(gains))
    m = {
        "k": round(k, 4) if k else None,
        "a": round(statistics.median(heats), 2) if len(heats) >= 3 else None,
        "lag_min": round(statistics.median(lags)) if len(lags) >= 3 else None,
        "g": round(g, 3) if g is not None else None,
        "wind_factor": round(statistics.median(ks_windy) / statistics.median(ks_calm), 2)
        if len(ks_windy) >= 3 and len(ks_calm) >= 3 else None,
        "cool_runs": len(ks), "heat_runs": len(heats), "updated": int(time.time()),
    }
    m["confident"] = bool(m["k"] and m["a"] and m["cool_runs"] >= 5 and m["heat_runs"] >= 5)
    db.set_state("thermal_model", m)
    return m


def model() -> dict:
    m = db.get_state("thermal_model")
    if not m or time.time() - m.get("updated", 0) > 3600:
        m = learn()
    return m


# ---------- predictions ----------
def heat_minutes(start: float, target: float, outdoor: float, m: dict | None = None) -> int | None:
    """Minutes for the boiler to take the flat from `start` to `target`."""
    m = m or model()
    if not (m.get("a") and m.get("k")) or start >= target:
        return 0 if start >= target else None
    t, mins = start, 0
    while t < target and mins < 480:
        t += (m["a"] - m["k"] * (t - outdoor)) / 60
        mins += 1
    return mins if t >= target else None


def coast_minutes(start: float, floor: float, outdoor: float, m: dict | None = None) -> int | None:
    """Minutes the flat stays above `floor` with the heating off."""
    m = m or model()
    if not m.get("k") or floor <= outdoor or start <= floor:
        return None if not m.get("k") else 0
    return int(math.log((start - outdoor) / (floor - outdoor)) / m["k"] * 60)


def _schedule_slots(day: datetime):
    """[(start datetime, target)] from the Hive schedule for `day` and the day after."""
    from .hive import hive
    heat = hive._product("heating") or {}
    sched = heat.get("state", {}).get("schedule") or {}
    out = []
    for d in (day, day + timedelta(days=1)):
        for slot in sched.get(d.strftime("%A").lower(), []) or []:
            try:
                start = int(slot.get("start"))
                target = float((slot.get("value") or {}).get("target"))
            except (TypeError, ValueError, AttributeError):
                continue
            out.append((datetime(d.year, d.month, d.day) + timedelta(minutes=start), target))
    return sorted(out)


def _next_change(now: datetime):
    slots = _schedule_slots(now)
    cur = None
    for start, target in slots:
        if start <= now:
            cur = target
        elif cur is not None and target != cur:
            return start, cur, target
    return None


# ---------- run every poll ----------
def act(s: dict, home_now: list, hold: bool):
    """Early-On, coasting and the open-window pause. Only in SCHEDULE mode, so Hive's own schedule
    always takes back over at the next change - nothing can get stuck."""
    from .hive import hive
    from . import weather, notify
    f = features()
    now = datetime.now()
    indoor, target, mode = s.get("indoor_temp"), s.get("target_temp"), s.get("mode")
    if indoor is None:
        return
    try:
        outdoor = weather.current()["temp"]
    except Exception:
        return
    m = model()

    # Open window: end the pause after 20 minutes
    pause = db.get_state("window_pause")
    if pause and time.time() - pause["ts"] > 20 * 60:
        db.set_state("window_pause", None)
        if mode == "SCHEDULE" and target == pause["paused_to"]:
            hive.set_target(pause["target"])
            db.add_event("smart", {"window": f"pause over – back to {pause['target']}°"})
        return
    if pause:
        return

    # Open window: temperature dropping fast while heating should be warming
    rows = db.readings_since(16 * 60)
    temps = [r["indoor_temp"] for r in rows if r.get("indoor_temp") is not None]
    if len(temps) >= 3 and target and indoor - outdoor > 3:
        drop = max(temps) - indoor
        calling = target > indoor
        window = drop >= 0.8 and calling and temps[-1] == indoor
        notify.once("window_open", window, "window_open", "Window open?",
                    f"The temperature dropped {drop:.1f}° in 15 minutes with the heating on." +
                    (" Heating paused for 20 minutes." if f["window"] and mode == "SCHEDULE" else ""))
        if window and f["window"] and mode == "SCHEDULE" and not pause:
            db.set_state("window_pause", {"ts": int(time.time()), "target": target, "paused_to": 12.0})
            hive.set_target(12)
            db.add_event("smart", {"window": f"temperature fell {drop:.1f}° – heating paused 20 min"})
            return

    # Accepted suggestions: in those hours, if the flat's empty, keep the heating down (until the next schedule change)
    if f["suggestions"] and mode == "SCHEDULE" and not home_now and not hold and target and target > 16:
        for sg in db.get_state("suggestions_accepted", []):
            if _in_window(now, sg):
                hive.set_target(16)
                db.add_event("smart", {"suggestion": f"flat empty {sg['label']} – heating down to 16°"})
                break

    if mode != "SCHEDULE" or not m.get("confident"):
        return
    nxt = _next_change(now)
    if not nxt:
        return
    start, cur_target, next_target = nxt
    mins_left = (start - now).total_seconds() / 60
    key = f"smart_{start:%Y%m%d%H%M}"
    if db.get_state(key):
        return

    # Early-On: warmer period coming up and someone's in
    if f["early_on"] and next_target >= cur_target + 0.5 and (home_now or hold) and indoor < next_target - 0.3:
        need = heat_minutes(indoor, next_target, outdoor, m)
        if need and 0 < mins_left <= min(need + 5, 90):          # never more than 1½ hours early
            hive.boost(max(15, int(mins_left) + 5), next_target)
            db.set_state(key, True)
            db.add_event("smart", {"early_on": f"started {int(mins_left)} min early to reach {next_target}° by {start:%H:%M}"})
        return

    # Coasting: cooler period coming up - the flat stays within ½° for a while with heating off
    if f["coasting"] and next_target <= cur_target - 1 and indoor >= cur_target - 0.3:
        coast = coast_minutes(indoor, cur_target - 0.5, outdoor, m)
        if coast and 0 < mins_left <= min(coast, 60):
            hive.set_target(next_target)
            db.set_state(key, True)
            db.add_event("smart", {"coasting": f"dropped to {next_target}° {int(mins_left)} min early – the flat holds its heat"})


def detect(s: dict):
    """Sunblock + heating heads-up. Called every poll after the reading is saved."""
    from . import notify
    rows = db.readings_since(65 * 60)
    m = db.get_state("thermal_model") or {}

    # Sun on the thermostat: warming fast with the boiler off in strong sunshine
    with db._lock:
        cur = db._conn.execute("SELECT ts, indoor_temp, boiler_on, extra FROM readings WHERE ts>=? AND indoor_temp IS NOT NULL "
                               "ORDER BY ts", (time.time() - 20 * 60,))
        recent = [dict(zip(("ts", "indoor_temp", "boiler_on", "extra"), r)) for r in cur.fetchall()]
    if len(recent) >= 3 and not any(r.get("boiler_on") for r in recent):
        extra = recent[-1].get("extra") or {}
        if isinstance(extra, str):
            extra = json.loads(extra or "{}")
        hours = (recent[-1]["ts"] - recent[0]["ts"]) / 3600
        rate = (recent[-1]["indoor_temp"] - recent[0]["indoor_temp"]) / hours if hours else 0
        if (extra.get("solar") or 0) >= 250 and rate > 1.0:
            if not db.get_state("sun_skew_until") or db.get_state("sun_skew_until") < time.time():
                db.add_event("smart", {"sunblock": f"sun warming the thermostat ({rate:.1f}°/hour) – readings ignored for learning"})
            db.set_state("sun_skew_until", int(time.time()) + 45 * 60)

    # Heads-up: calling for heat for an hour, well below target, but not warming
    calling = [r for r in rows if r.get("indoor_temp") is not None]
    stuck = (len(calling) >= 11 and all(r.get("boiler_on") for r in calling)
             and all((r.get("target_temp") or 0) - r["indoor_temp"] >= 0.8 for r in calling)
             and calling[-1]["indoor_temp"] - calling[0]["indoor_temp"] < 0.2
             and not db.get_state("window_pause"))
    notify.once("heating_fault", stuck, "heating_fault", "Heating isn't warming the flat",
                "It's been calling for heat for an hour but the temperature hasn't risen. Check the boiler "
                "pressure (should be about 1–1.5 bar) and the boiler display for a fault code.")


def sun_skewed() -> bool:
    return (db.get_state("sun_skew_until") or 0) > time.time()


# ---------- Seasonal Savings ----------
def savings_offset() -> float:
    return float(db.get_state("savings_offset", 0.0)) if features()["savings"] else 0.0


def savings_step():
    """Once a day. Every 14 days with no 'too cold', aim ½° lower (max 1°). Needs everyone's comfort learned first."""
    from . import comfort
    if not features()["savings"]:
        return
    st = db.get_state("savings", {"changed": int(time.time()), "frozen_until": 0})
    now = time.time()
    est = comfort.estimates()
    if not est or not all(e["confident"] for e in est.values()) or now < st.get("frozen_until", 0):
        return
    cold = [e for e in db.get_state("comfort_log", []) if e["feeling"] == "cold" and e["ts"] >= st["changed"]]
    off = float(db.get_state("savings_offset", 0.0))
    if not cold and now - st["changed"] >= 14 * 86400 and off > -1.0:
        db.set_state("savings_offset", off - 0.5)
        st["changed"] = int(now)
        db.add_event("smart", {"savings": f"nobody's been cold for 2 weeks – now aiming {abs(off - 0.5)}° below your usual comfort"})
    db.set_state("savings", st)


def savings_feedback_cold():
    """Someone said they're cold: undo the last ½° step if it was recent, and stop nudging for 2 months."""
    off = float(db.get_state("savings_offset", 0.0))
    st = db.get_state("savings", {"changed": int(time.time()), "frozen_until": 0})
    if off < 0 and time.time() - st.get("changed", 0) < 21 * 86400:
        db.set_state("savings_offset", min(0.0, off + 0.5))
        st.update(changed=int(time.time()), frozen_until=int(time.time()) + 60 * 86400)
        db.set_state("savings", st)
        db.add_event("smart", {"savings": "someone was cold – went back up ½° and paused the nudging for 2 months"})


# ---------- for the app ----------
def info(s: dict | None = None) -> dict:
    from . import weather
    m = model()
    out = {"model": m, "features": features(), "wanted": wanted(), "labels": FEATURES, "phase3": sorted(PHASE3),
           "learning_days": round(learning_days(), 1), "learning_target": LEARNING_DAYS, "savings_offset": savings_offset(),
           "sun_skew": sun_skewed(), "window_pause": bool(db.get_state("window_pause"))}
    try:
        outdoor = weather.current()["temp"]
    except Exception:
        outdoor = None
    if outdoor is not None and m.get("k"):
        out["examples"] = {
            "outdoor": round(outdoor),
            "heat_17_to_20": heat_minutes(17, 20, outdoor, m),
            "hold_half_degree_from_20": coast_minutes(20, 19.5, outdoor, m),
            "loss_per_hour_at_20": round(m["k"] * (20 - outdoor), 1),
        }
    if s and outdoor is not None and s.get("indoor_temp") is not None and s.get("target_temp") and s["target_temp"] > s["indoor_temp"] + 0.2:
        out["eta_min"] = heat_minutes(s["indoor_temp"], s["target_temp"], outdoor, m)
    return out


# ---------- Schedule suggestions (Nest's "Smart Schedule": proposed, you accept) ----------
def _in_window(now: datetime, sg: dict) -> bool:
    weekend = now.weekday() >= 5
    return sg["days"] == ("weekends" if weekend else "weekdays") and sg["from"] <= now.hour < sg["to"]


def _boiler_cost_per_hour() -> float:
    from .bright import tariff_settings
    now = time.time()
    with db._lock:
        gas = db._conn.execute("SELECT SUM(kwh) FROM energy WHERE fuel='gas' AND kwh>0 AND ts>=?", (now - 30 * 86400,)).fetchone()[0]
        on = db._conn.execute("SELECT COUNT(*) FROM readings WHERE boiler_on=1 AND ts>=?", (now - 30 * 86400,)).fetchone()[0]
    kw = gas / (on * 5 / 60) if gas and on > 60 else 8.0           # measured, or a typical combi on heating
    return min(kw, 20) * tariff_settings()["gas_rate"] / 100


def suggestions() -> list:
    """Hours the heating regularly runs with the flat empty, grouped into weekday / weekend windows."""
    with db._lock:
        rows = db._conn.execute(
            "SELECT ts, boiler_on, people_home, kids_lights_on, hold FROM readings WHERE ts>=?", (time.time() - 28 * 86400,)).fetchall()
    days, waste = {}, {}
    for ts, b, p, k, h in rows:
        d = datetime.fromtimestamp(ts)
        g = "weekends" if d.weekday() >= 5 else "weekdays"
        days.setdefault(g, set()).add(d.date())
        if b and not p and not k and not h:
            waste.setdefault((g, d.hour), set()).add(d.date())
            waste.setdefault(("min", g, d.hour), []).append(5)
    cost = _boiler_cost_per_hour()
    out, accepted = [], {(a["days"], a["from"], a["to"]) for a in db.get_state("suggestions_accepted", [])}
    for g in ("weekdays", "weekends"):
        n = len(days.get(g, ()))
        if n < (6 if g == "weekdays" else 3):
            continue
        hours = [h for h in range(24) if len(waste.get((g, h), ())) >= 0.5 * n]
        while hours:                                                     # join neighbouring hours into windows
            a = b = hours.pop(0)
            while hours and hours[0] == b + 1:
                b = hours.pop(0)
            mins = sum(sum(waste.get(("min", g, h), [])) for h in range(a, b + 1)) / n   # wasted minutes per day
            per_year = mins / 60 * cost * (261 if g == "weekdays" else 104) * 0.5        # heating season ≈ half the year
            if per_year < 5:
                continue
            label = f"{a:02d}:00–{b + 1:02d}:00 on {g}"
            out.append({"days": g, "from": a, "to": b + 1, "label": label, "per_year": round(per_year),
                        "why": f"The flat was empty with the heating on at these times on most {g} over the last 4 weeks.",
                        "accepted": (g, a, b + 1) in accepted})
    return out


def accept_suggestion(days: str, start: int, end: int, on: bool):
    acc = [a for a in db.get_state("suggestions_accepted", []) if (a["days"], a["from"], a["to"]) != (days, start, end)]
    if on:
        acc.append({"days": days, "from": start, "to": end, "label": f"{start:02d}:00–{end:02d}:00 on {days}"})
    db.set_state("suggestions_accepted", acc)
    db.add_event("smart", {"suggestion": ("accepted " if on else "removed ") + f"{start:02d}:00–{end:02d}:00 on {days}"})
