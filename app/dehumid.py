"""Kitchen dehumidifier (Smart Life): dry clothes on the cheap rate - and dry by the time you need them.

  Learns          How long a load takes to dry (from each run: switched on → humidity levels off), and what time
                  you usually take the clothes (when someone switches it off in the morning).
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
def learned() -> dict:
    runs = [r["hours"] for r in db.get_state("dehum_runs", []) if r.get("clothes", True)][-10:]
    picks = db.get_state("dehum_collect", [])[-10:]
    hours = round(statistics.median(runs), 1) if len(runs) >= 2 else DEFAULT_HOURS
    ready = statistics.median(picks) - 0.25 if len(picks) >= 3 else DEFAULT_READY   # 15 min before you usually take them
    return {"dry_hours": hours, "loads": len(runs), "ready_by": round(ready, 2),
            "ready_by_text": f"{int(ready):02d}:{int(round(ready % 1 * 60)) % 60:02d}", "collect_samples": len(picks)}


def _plan(now: datetime, night_start: str) -> dict:
    """When to start an evening load so it's dry by collection time, using the cheap rate where possible."""
    L = learned()
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

        # Someone switched it off in the morning after a run = when you take the clothes
        if switched_off and ts - (s.get("off_by_us") or 0) > 600 and 4 <= now.hour < 12 and s.get("run_start"):
            picks = db.get_state("dehum_collect", [])
            db.set_state("dehum_collect", (picks + [round(now.hour + now.minute / 60, 2)])[-20:])

        if switched_on and s.get("deferred_at") and ts - s["deferred_at"] < 600:
            # Switched back on within 10 minutes of us pausing it → dry now, leave it alone tonight
            s.update(deferred=False, override_day=str(now.date()), run_start=ts, dry=False, ours=False, damp=False)
            db.add_event("smart", {"dehumidifier": f"{name} switched back on – drying now as asked"})
        elif (switched_on and f.get("dehum_cheap") and not night and _evening(now, night_start)
              and s.get("override_day") != str(now.date())):
            p = _plan(now, night_start)
            if p["start"] - now > timedelta(minutes=20):
                _off(d, s)
                s.update(deferred=True, deferred_at=ts, start_at=int(p["start"].timestamp()), damp=False)
                cheap_h = max(0.0, min(p["hours"], (p["ready"] - p["start"]).total_seconds() / 3600))
                saving = DEFAULT_KW * cheap_h * (tariff["day_rate"] - tariff["night_rate"])
                notify.send("dehumidifier", f"{name} will start at {p['start']:%H:%M}",
                            f"Loads take about {p['hours']:g} h, so it'll be dry by about {p['ready']:%H:%M}"
                            f"{f' and save ~{saving:.0f}p on the cheap rate' if saving >= 1 else ''}. "
                            "Need it sooner? Switch it on again within 10 minutes.")
                db.add_event("smart", {"dehumidifier": f"{name} paused until {p['start']:%H:%M} – dry by {p['ready']:%H:%M}"})
            else:
                s.update(run_start=ts, dry=False, ours=False, damp=False)    # needs to start now to be dry in time
        elif switched_on:
            s.update(run_start=ts, dry=False, ours=False, damp=False)

        # Planned start time reached
        if s.get("deferred") and ts >= s.get("start_at", 0):
            tuya.command(d["id"], "on")
            d["on"] = True
            s.update(deferred=False, run_start=ts, dry=False, ours=True, late_told=False)
            db.add_event("smart", {"dehumidifier": f"{name} started{' on the cheap rate' if night else ''}"})

        # Damp watch: 65%+ for 2 hours with it off
        damp = (f.get("dehum_damp") and not d["on"] and len(hist) >= 20 and ts - hist[0][0] >= 2 * 3600 - 600
                and all(h >= 65 for t, h in hist if ts - t <= 2 * 3600))
        if damp and not s.get("deferred"):
            if night:
                tuya.command(d["id"], "on")
                d["on"] = True
                s.update(run_start=ts, dry=False, ours=True, damp=True)
                db.add_event("smart", {"dehumidifier": f"humidity {hist[-1][1]:.0f}% for 2 hours – {name} started (cheap rate)"})
            else:
                s.update(deferred=True, deferred_at=0, start_at=int(_next_at(now, _mins(night_start)).timestamp()), damp=True)
                notify.send("dehumidifier", f"Damp in the flat – {hist[-1][1]:.0f}% humidity",
                            f"It's been 65%+ for 2 hours (condensation / mould risk). {name} will start at {night_start}, "
                            "or switch it on now if you'd rather.")

        # Clothes dry: running 90+ min, humidity flat (±1%) for 45 min and near the setting
        if d["on"] and s.get("run_start") and not s.get("dry") and ts - s["run_start"] >= 90 * 60:
            last45 = [h for t, h in hist if ts - t <= 45 * 60]
            target = d.get("set_humidity") or 50
            if len(last45) >= 8 and max(last45) - min(last45) <= 2 and last45[-1] <= target + 8:
                s["dry"] = True
                hours = (ts - s["run_start"]) / 3600 - 0.6        # it levelled off ~35 min before we could be sure
                runs = db.get_state("dehum_runs", [])
                db.set_state("dehum_runs", (runs + [{"ts": ts, "hours": round(hours, 2), "clothes": not s.get("damp")}])[-30:])
                if s.get("ours"):
                    _off(d, s)
                    db.add_event("smart", {"dehumidifier": f"clothes dry after {hours:.1f} h – {name} switched off",
                                           "cheap_hours": round(hours, 2) if night else 0})
                    notify.send("washing_done", "Clothes are dry", f"Took {hours:.1f} hours. {name} has switched off.")
                else:
                    db.add_event("smart", {"dehumidifier": f"clothes look dry after {hours:.1f} h"})
                    notify.send("washing_done", "Clothes look dry", f"Took {hours:.1f} hours – humidity has levelled off at {last45[-1]:.0f}%.")

        # Collection time and still not dry → say how much longer
        L = learned()
        if (d["on"] and s.get("run_start") and not s.get("dry") and not s.get("damp") and not s.get("late_told")
                and abs(now.hour + now.minute / 60 - L["ready_by"]) < 0.1):
            left = max(0.25, L["dry_hours"] - (ts - s["run_start"]) / 3600)
            notify.send("washing_done", "Clothes not quite dry yet",
                        f"Loads usually take about {L['dry_hours']:g} h – roughly {left * 60:.0f} more minutes. It'll keep going.")
            s["late_told"] = True

        # Cheap rate over and still going: log the cheap hours for the report
        if not night and s.get("ours") and s.get("run_start") and not s.get("dry"):
            hours = min((ts - s["run_start"]) / 3600, 7)
            db.add_event("smart", {"dehumidifier": f"{name} ran {hours:.1f} h on the cheap rate – still going", "cheap_hours": round(hours, 2)})
            s["ours"] = False

        if switched_off and not s.get("deferred"):
            s.update(run_start=None, ours=False)
        s["was_on"] = d["on"]
    db.set_state("dehum", states)


def reading_extras() -> dict:
    """Indoor humidity (and whether it's running - it also gives off a bit of heat) for each 5-minute reading."""
    for d in tuya.devices():
        if d["kind"] == "dehumidifier" and not d["hidden"] and d.get("humidity") is not None:
            return {"indoor_humidity": d["humidity"], "dehum_on": bool(d["on"]), "kitchen_temp": d.get("temp")}
    return {}
