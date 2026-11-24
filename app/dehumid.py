"""Kitchen dehumidifier (Smart Life): dry clothes on the cheap rate - and dry by the time you need them.

  Loads           It's left on most of the time, so a load of washing is spotted from the humidity jumping up
                  (wet clothes hung out), not from it being switched on.
  Learns          How long a load takes to dry (humidity jump → back down and level), and when the clothes are
                  needed: 30 min before the first person usually leaves in the morning (from the router),
                  separately for weekdays and weekends. Default 07:00 until it knows.
  Cheap-rate      Switched on in the evening? It works out the latest safe start: cheap rate if that leaves
                  enough time (plus a 20% margin) to be dry by collection time, otherwise earlier - or straight away.
                  Switch it on again within 10 minutes to override for tonight.
  Clothes dry     Humidity levelled off → "clothes are dry". If the app started it, it switches it off.
  Not dry yet     Still drying at collection time → tells you roughly how much longer.
  Tank full       Notification as soon as it stops for that.
  Damp watch      Humidity 65%+ for 2 hours with it off → starts it on the cheap rate (or tells you in the day).
"""
import statistics
import time
from datetime import datetime, timedelta

from . import db, notify
from .tuya import tuya

DEFAULT_KW = 0.3           # typical clothes-drying dehumidifier when it doesn't report its power
DEFAULT_HOURS = 6.0        # until it has learned your loads
DEFAULT_READY = 7.0        # 07:00 until it has learned when you take the clothes
MARGIN = 1.2               # 20% extra time so a bigger load still finishes


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _night(now: datetime) -> tuple:
    from .bright import tariff_settings
    t = tariff_settings()
    a, b, m = _mins(t.get("night_start", "00:30")), _mins(t.get("night_end", "07:30")), now.hour * 60 + now.minute
    inside = (a <= m < b) if a < b else (m >= a or m < b)
    return inside, t.get("night_start", "00:30"), t


def _evening(now: datetime, night_start: str) -> bool:
    m, a = now.hour * 60 + now.minute, _mins(night_start)
    return m >= 17 * 60 or (a < 17 * 60 and m < a)


def _next_at(now: datetime, minutes: float) -> datetime:
    """Next time it's `minutes` past midnight."""
    t = datetime(now.year, now.month, now.day) + timedelta(minutes=minutes)
    return t if t > now else t + timedelta(days=1)


# ---------- what it has learned ----------
def _first_departures(weekend: bool) -> list:
    """Hour of the first phone leaving each morning (05:00–11:00) over the last 4 weeks."""
    import json
    with db._lock:
        rows = db._conn.execute("SELECT ts, detail FROM events WHERE kind='presence' AND ts>=? ORDER BY ts",
                                (time.time() - 28 * 86400,)).fetchall()
    first = {}
    for ts, detail in rows:
        try:
            dd = json.loads(detail)
        except (TypeError, ValueError):
            continue
        t = datetime.fromtimestamp(ts)
        if dd.get("home") is False and 5 <= t.hour < 11 and (t.weekday() >= 5) == weekend:
            first.setdefault(t.date(), t.hour + t.minute / 60)
    return list(first.values())


def learned(when: datetime | None = None) -> dict:
    runs = [r["hours"] for r in db.get_state("dehum_runs", []) if r.get("clothes", True)][-10:]
    hours = round(statistics.median(runs), 1) if len(runs) >= 2 else DEFAULT_HOURS
    when = when or datetime.now()
    weekend = (when + timedelta(hours=12)).weekday() >= 5          # the morning the clothes are for
    leaves = _first_departures(weekend) or ([] if not weekend else [])
    picks = db.get_state("dehum_collect", [])[-10:]                  # if it does get switched off in the morning
    if len(leaves) >= 3:
        ready, why = statistics.median(leaves) - 0.5, f"30 min before the first of you usually leaves{' at weekends' if weekend else ''}"
    elif len(picks) >= 3:
        ready, why = statistics.median(picks) - 0.25, "15 min before you usually switch it off"
    else:
        ready, why = DEFAULT_READY, "07:00 until it learns when you leave"
    ready = min(max(ready, 5.5), 10.0)
    return {"dry_hours": hours, "loads": len(runs), "ready_by": round(ready, 2), "why": why,
            "ready_by_text": f"{int(ready):02d}:{int(round(ready % 1 * 60)) % 60:02d}", "collect_samples": len(leaves) or len(picks)}


def _plan(now: datetime, night_start: str) -> dict:
    """When to start an evening load so it's dry by collection time, using the cheap rate where possible."""
    L = learned(now)
    ready = _next_at(now, L["ready_by"] * 60)
    latest = ready - timedelta(hours=L["dry_hours"] * MARGIN)
    cheap = _next_at(now, _mins(night_start))
    start = min(cheap, latest)
    return {"start": start, "ready": ready, "hours": L["dry_hours"], "cheap": start >= cheap - timedelta(minutes=1)}


def _off(d, s, by_us=True):
    tuya.command(d["id"], "off")
    d["on"] = False
    s["off_by_us"] = int(time.time()) if by_us else 0


def check():
    from .thermal import features
    f = features()
    now, ts = datetime.now(), int(time.time())
    night, night_start, tariff = _night(now)
    states = db.get_state("dehum", {})
    for d in tuya.devices():
        if d["kind"] != "dehumidifier" or d["hidden"] or not d.get("online", True):
            continue
        s = states.setdefault(d["id"], {"was_on": d["on"], "hist": []})
        name = d["name"]
        if d.get("humidity") is not None:
            s["hist"] = [x for x in s.get("hist", []) if ts - x[0] < 3 * 3600] + [[ts, d["humidity"]]]
        hist = s.get("hist", [])

        # Tank full / fault
        notify.once(f"dehum_fault_{d['id']}", bool(d.get("faults")), "dehumidifier",
                    f"{name}: {'tank full' if d.get('tank_full') else 'needs attention'}",
                    "Empty the water tank so it can carry on drying." if d.get("tank_full") else
                    f"It's reporting: {', '.join(d['faults'])}.")

        switched_on = d["on"] and not s.get("was_on")
        switched_off = not d["on"] and s.get("was_on")

        # If someone does switch it off in the morning, that's when the clothes were taken
        if switched_off and ts - (s.get("off_by_us") or 0) > 600 and 4 <= now.hour < 12:
            picks = db.get_state("dehum_collect", [])
            db.set_state("dehum_collect", (picks + [round(now.hour + now.minute / 60, 2)])[-20:])

        # A load of washing hung out: humidity jumps 8+ points above where it's been sitting
        load = s.get("load")
        if not load and len(hist) >= 12 and d.get("humidity") is not None:
            before = [h for t, h in hist if ts - t > 75 * 60]
            base = statistics.median(before) if len(before) >= 6 else None
            last40 = [h for t, h in hist if ts - t <= 40 * 60]
            # Sustained for 40 min - cooking steam or the kettle clears quicker than a rack of wet washing
            if (base is not None and d["humidity"] >= base + 8 and d["humidity"] >= (d.get("set_humidity") or 50) + 5
                    and len(last40) >= 7 and min(last40) >= base + 6):
                onset = next((t for t, h in hist if ts - t <= 75 * 60 and h >= base + 2), ts)
                load = s["load"] = {"onset": onset, "base": base, "was_on": bool(s.get("was_on") or d["on"])}
                s.update(dry=False, damp=False, late_told=False)
                db.add_event("smart", {"dehumidifier": f"washing spotted – humidity up from {base:.0f}% to {d['humidity']:.0f}%"})
        if switched_on and not load:
            # Switched on from off: treat it as a load too (in case the humidity jump was missed)
            load = s["load"] = {"onset": ts, "base": d.get("set_humidity") or 50, "was_on": False}
            s.update(dry=False, damp=False, late_told=False)

        if switched_on and s.get("deferred_at") and ts - s["deferred_at"] < 600:
            # Switched back on within 10 minutes of us pausing it → dry now, leave it alone tonight
            s.update(deferred=False, override_day=str(now.date()), ours=False)
            db.add_event("smart", {"dehumidifier": f"{name} switched back on – drying now as asked"})
        elif (load and not load.get("planned") and f.get("dehum_cheap") and not night and _evening(now, night_start)
              and s.get("override_day") != str(now.date())):
            load["planned"] = True
            p = _plan(now, night_start)
            if p["start"] - now > timedelta(minutes=20):
                if d["on"]:
                    _off(d, s)
                s.update(deferred=True, deferred_at=ts, start_at=int(p["start"].timestamp()))
                cheap_h = max(0.0, min(p["hours"], (p["ready"] - p["start"]).total_seconds() / 3600))
                saving = DEFAULT_KW * cheap_h * (tariff["day_rate"] - tariff["night_rate"])
                notify.send("dehumidifier", f"Washing out – {name} will start at {p['start']:%H:%M}",
                            f"Loads take about {p['hours']:g} h, so it'll be dry by about {p['ready']:%H:%M}"
                            f"{f' and save ~{saving:.0f}p on the cheap rate' if saving >= 1 else ''}. "
                            "Need it sooner? Switch it on again within 10 minutes.")
                db.add_event("smart", {"dehumidifier": f"{name} paused until {p['start']:%H:%M} – dry by {p['ready']:%H:%M}"})

        # Planned start time reached
        if s.get("deferred") and ts >= s.get("start_at", 0):
            tuya.command(d["id"], "on")
            d["on"] = True
            s.update(deferred=False, ours=True)
            if s.get("load"):
                s["load"]["started"] = ts
            db.add_event("smart", {"dehumidifier": f"{name} started{' on the cheap rate' if night else ''}"})

        # Damp watch: 65%+ for 2 hours with it off
        damp = (f.get("dehum_damp") and not d["on"] and len(hist) >= 20 and ts - hist[0][0] >= 2 * 3600 - 600
                and all(h >= 65 for t, h in hist if ts - t <= 2 * 3600))
        if damp and not s.get("deferred") and not s.get("load"):
            if night:
                tuya.command(d["id"], "on")
                d["on"] = True
                s.update(ours=True, damp=True, load={"onset": ts, "base": 55, "was_on": False, "started": ts})
                db.add_event("smart", {"dehumidifier": f"humidity {hist[-1][1]:.0f}% for 2 hours – {name} started (cheap rate)"})
            else:
                s.update(deferred=True, deferred_at=0, start_at=int(_next_at(now, _mins(night_start)).timestamp()), damp=True,
                         load={"onset": ts, "base": 55, "was_on": False, "planned": True})
                notify.send("dehumidifier", f"Damp in the flat – {hist[-1][1]:.0f}% humidity",
                            f"It's been 65%+ for 2 hours (condensation / mould risk). {name} will start at {night_start}, "
                            "or switch it on now if you'd rather.")

        # Clothes dry: humidity back down near where it was and not moving (within 1%) for 45 min
        load = s.get("load")
        if load and d["on"] and not s.get("deferred") and ts - load.get("started", load["onset"]) >= 60 * 60:
            last45 = [h for t, h in hist if ts - t <= 45 * 60]
            floor = max(load["base"], d.get("set_humidity") or 50)
            if len(last45) >= 8 and max(last45) - min(last45) <= 1 and last45[-1] <= floor + 4:
                start = load.get("started", load["onset"])
                hours = max(0.5, (ts - start) / 3600 - 0.6)          # it levelled off ~35 min before we could be sure
                if not s.get("damp"):
                    runs = db.get_state("dehum_runs", [])
                    db.set_state("dehum_runs", (runs + [{"ts": ts, "hours": round(hours, 2), "clothes": True}])[-30:])
                if s.get("ours") and not load.get("was_on"):
                    _off(d, s)                                        # it was off before - put it back off
                    after = f" {name} has switched off."
                else:
                    after = f" {name} is back to keeping the flat at {d.get('set_humidity') or 50:.0f}%."
                db.add_event("smart", {"dehumidifier": f"clothes dry after {hours:.1f} h", "cheap_hours": round(hours, 2) if night and s.get("ours") else 0})
                notify.send("washing_done", "Clothes are dry", f"Took {hours:.1f} hours.{after}")
                s.update(load=None, ours=False, dry=True)

        # Collection time and still not dry → say how much longer
        load = s.get("load")
        L = learned(now)
        if load and not s.get("damp") and not load.get("late_told") and abs(now.hour + now.minute / 60 - L["ready_by"]) < 0.1:
            left = max(0.25, L["dry_hours"] - (ts - load.get("started", load["onset"])) / 3600)
            notify.send("washing_done", "Clothes not quite dry yet",
                        f"Loads usually take about {L['dry_hours']:g} h – roughly {left * 60:.0f} more minutes. It'll keep going.")
            load["late_told"] = True

        # Cheap rate over and still going: log the cheap hours for the report
        if not night and s.get("ours") and load:
            hours = min((ts - load.get("started", load["onset"])) / 3600, 7)
            db.add_event("smart", {"dehumidifier": f"{name} ran {hours:.1f} h on the cheap rate – still going", "cheap_hours": round(hours, 2)})
            s["ours"] = False

        # Forget a load nobody's drying (switched off by someone and not waiting for us) after 12 hours
        if load and not d["on"] and not s.get("deferred") and ts - load["onset"] > 12 * 3600:
            s["load"] = None

        s["was_on"] = d["on"]
    db.set_state("dehum", states)


def reading_extras() -> dict:
    """Indoor humidity (and whether it's running - it also gives off a bit of heat) for each 5-minute reading."""
    for d in tuya.devices():
        if d["kind"] == "dehumidifier" and not d["hidden"] and d.get("humidity") is not None:
            return {"indoor_humidity": d["humidity"], "dehum_on": bool(d["on"]), "kitchen_temp": d.get("temp")}
    return {}
