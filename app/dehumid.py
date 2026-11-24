"""Kitchen dehumidifier (Smart Life): dry clothes on the cheap rate, tank-full and clothes-dry alerts,
and the flat's indoor humidity for the learning (Hive doesn't measure humidity).

  Cheap-rate drying  Switched on in the evening (17:00 until the cheap rate starts)? It's paused and started
                     at the start of the cheap rate instead. Switch it on again within 10 minutes to override for tonight.
  Clothes dry        Running 90+ min and humidity has levelled off for 45 min → "clothes look dry". If the app
                     started it, it also switches it off.
  Tank full / fault  Notification as soon as it stops for that.
  Damp watch         Humidity 65%+ for 2 hours with it off → starts it on the cheap rate (or tells you in the day).
"""
import time
from datetime import datetime

from . import db, notify
from .tuya import tuya

DEFAULT_KW = 0.3          # typical clothes-drying dehumidifier when it doesn't report its power


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

        # Tank full / fault
        notify.once(f"dehum_fault_{d['id']}", bool(d.get("faults")), "dehumidifier",
                    f"{name}: {'tank full' if d.get('tank_full') else 'needs attention'}",
                    "Empty the water tank so it can carry on drying." if d.get("tank_full") else
                    f"It's reporting: {', '.join(d['faults'])}.")

        switched_on = d["on"] and not s.get("was_on")
        # Override: switched back on within 10 minutes of us pausing it → leave it alone tonight
        if switched_on and s.get("deferred_at") and ts - s["deferred_at"] < 600:
            s.update(deferred=False, override_day=str(now.date()))
            db.add_event("smart", {"dehumidifier": f"{name} switched back on – drying now as asked"})
        # Cheap-rate drying: evening switch-on → wait for the cheap rate
        elif (switched_on and f.get("dehum_cheap") and not night and _evening(now, night_start)
              and s.get("override_day") != str(now.date())):
            tuya.command(d["id"], "off")
            d["on"] = False
            s.update(deferred=True, deferred_at=ts)
            saving = DEFAULT_KW * 6 * (tariff["day_rate"] - tariff["night_rate"])
            notify.send("dehumidifier", f"{name} will start at {night_start}",
                        f"Drying on the cheap rate saves about {saving:.0f}p tonight. Need it now? Switch it on again within 10 minutes.")
            db.add_event("smart", {"dehumidifier": f"{name} paused until {night_start} – drying on the cheap rate"})
        elif switched_on:
            s.update(run_start=ts, dry=False, ours=False)

        # Cheap rate has started → go
        if night and s.get("deferred"):
            tuya.command(d["id"], "on")
            d["on"] = True
            s.update(deferred=False, run_start=ts, dry=False, ours=True)
            db.add_event("smart", {"dehumidifier": f"{name} started on the cheap rate"})

        # Damp watch: 65%+ for 2 hours with it off
        hist = s.get("hist", [])
        damp = (f.get("dehum_damp") and not d["on"] and len(hist) >= 20 and ts - hist[0][0] >= 2 * 3600 - 600
                and all(h >= 65 for t, h in hist if ts - t <= 2 * 3600))
        if damp and not s.get("deferred"):
            if night:
                tuya.command(d["id"], "on")
                d["on"] = True
                s.update(run_start=ts, dry=False, ours=True)
                db.add_event("smart", {"dehumidifier": f"humidity {hist[-1][1]:.0f}% for 2 hours – {name} started (cheap rate)"})
            else:
                s.update(deferred=True, deferred_at=0)
                notify.send("dehumidifier", f"Damp in the flat – {hist[-1][1]:.0f}% humidity",
                            f"It's been 65%+ for 2 hours (condensation / mould risk). {name} will start at {night_start}, "
                            "or switch it on now if you'd rather.")

        # Clothes dry: running 90+ min, humidity flat (±1%) for 45 min and near the setting
        if d["on"] and s.get("run_start") and not s.get("dry") and ts - s["run_start"] >= 90 * 60:
            last45 = [h for t, h in hist if ts - t <= 45 * 60]
            target = d.get("set_humidity") or 50
            if len(last45) >= 8 and max(last45) - min(last45) <= 2 and last45[-1] <= target + 8:
                s["dry"] = True
                hours = (ts - s["run_start"]) / 3600
                if s.get("ours"):
                    tuya.command(d["id"], "off")
                    d["on"] = False
                    db.add_event("smart", {"dehumidifier": f"clothes dry after {hours:.1f} h on the cheap rate – {name} switched off",
                                           "cheap_hours": round(hours, 2)})
                    notify.send("washing_done", "Clothes are dry", f"{name} ran {hours:.1f} hours on the cheap rate and has switched off.")
                else:
                    db.add_event("smart", {"dehumidifier": f"clothes look dry after {hours:.1f} h"})
                    notify.send("washing_done", "Clothes look dry", f"Humidity has levelled off at {last45[-1]:.0f}% – {name} can go off.")

        # End of cheap rate: log the hours it ran on the cheap rate
        if not night and s.get("ours") and s.get("run_start") and not s.get("dry"):
            hours = min((ts - s["run_start"]) / 3600, 7)
            db.add_event("smart", {"dehumidifier": f"{name} ran {hours:.1f} h on the cheap rate – still going", "cheap_hours": round(hours, 2)})
            s["ours"] = False

        if not d["on"] and s.get("was_on") and not s.get("deferred"):
            s.update(run_start=None, ours=False)
        s["was_on"] = d["on"]
    db.set_state("dehum", states)


def reading_extras() -> dict:
    """Indoor humidity (and whether it's running - it also gives off a bit of heat) for each 5-minute reading."""
    for d in tuya.devices():
        if d["kind"] == "dehumidifier" and not d["hidden"] and d.get("humidity") is not None:
            return {"indoor_humidity": d["humidity"], "dehum_on": bool(d["on"]), "kitchen_temp": d.get("temp")}
    return {}
