"""Works out when to send a notification. Runs after every 5-minute poll."""
import time
from datetime import datetime

from . import db, notify

DOWN_AFTER = 30 * 60


def _error_since(errors: dict) -> dict:
    since = db.get_state("error_since", {})
    now = int(time.time())
    for k in errors:
        since.setdefault(k, now)
    for k in list(since):
        if k not in errors:
            since.pop(k)
    db.set_state("error_since", since)
    return since


def check(errors: dict):
    now = int(time.time())

    # 1. Heating running while the flat's empty for 30 minutes straight
    with db._lock:
        rows = db._conn.execute(
            "SELECT boiler_on, people_home, kids_lights_on, hold FROM readings WHERE ts>=? ORDER BY ts", (now - 30 * 60,)).fetchall()
    empty_heating = len(rows) >= 5 and all(b and p == 0 and not k and not h for b, p, k, h in rows)
    notify.once("heating_empty", empty_heating, "heating_empty", "Heating's on – nobody's home",
                "The boiler's been running for 30 minutes with the flat empty. Tap to turn it down.")

    # 2. Something stopped working (only after 30 minutes, so brief blips don't nag)
    names = {"hive": "Hive heating", "tuya": "Smart Life", "bright": "Smart meter (Bright)", "connectlife": "Washer-dryer"}
    since = _error_since({k: v for k, v in errors.items() if k in names})
    for k, name in names.items():
        notify.once(f"down_{k}", k in since and now - since[k] > DOWN_AFTER, "service_down",
                    f"{name} isn't responding", "It'll keep retrying by itself. Open the app for details.")
    from . import router
    last = db.get_state("router_last_report")
    notify.once("down_router", bool(last) and now - last > DOWN_AFTER, "service_down",
                "Router stopped reporting", "Phone and Fire Stick detection is paused. Check the router's scheduled task.")

    # 3. Washer-dryer finished: handled by connectlife.track_runs() (it knows this machine's fields)

    # 4. Flat getting cold while someone's in (below 16° for 30 minutes)
    with db._lock:
        temps = db._conn.execute(
            "SELECT indoor_temp, people_home, kids_lights_on, hold FROM readings WHERE ts>=? ORDER BY ts", (now - 30 * 60,)).fetchall()
    cold = len(temps) >= 5 and all(t is not None and t < 16 and (p or k or h) for t, p, k, h in temps)
    notify.once("cold_home", cold, "cold_home", "The flat's getting cold",
                f"It's {temps[-1][0] if temps else '?'}° inside and someone's home. Tap to boost the heating.")

    # 5. Lights left on with everyone out for 30 minutes
    from .presence import lights_signal
    try:
        from .main import all_devices, presence as _presence
        pres = _presence()
        if not any(v["home"] for v in pres.values()) and pres and now - max(v["since"] for v in pres.values()) > 30 * 60:
            left = lights_signal(all_devices(), pres).get("ignored", [])
        else:
            left = []
    except Exception:
        left = []
    notify.once("lights_left", bool(left), "lights_left", "Lights left on",
                f"{', '.join(left)} {'is' if len(left) == 1 else 'are'} on and nobody's home. Tap to switch off.")

    d = datetime.now()

    # 6. Frost tonight - one heads-up at 6pm
    if d.hour == 18 and not db.get_state(f"frost_{d.date()}"):
        try:
            from . import weather
            low = weather.current().get("overnight_low")
            if low is not None and low <= 1:
                notify.send("frost", "Frost tonight", f"Down to {low:.0f}° overnight. The heating may need to start earlier in the morning.")
            db.set_state(f"frost_{d.date()}", True)
        except Exception:
            pass

    # 7. Unusually high electricity day (checked once a day, after the smart meter data arrives)
    from .bright import bright
    if bright.is_configured() and d.hour >= 10 and not db.get_state(f"highuse_{d.date()}"):
        with db._lock:
            rows = db._conn.execute(
                "SELECT date(ts,'unixepoch','localtime') d, SUM(kwh) FROM energy WHERE fuel='electricity' AND kwh>0 "
                "AND ts>=? GROUP BY d ORDER BY d", (now - 16 * 86400,)).fetchall()
        full = [(day, v) for day, v in rows if v > 0.5]
        if len(full) >= 8:
            last_day, last = full[-1]
            avg = sum(v for _, v in full[:-1]) / len(full[:-1])
            if last > avg * 1.5 and last - avg > 2:
                notify.send("high_use", "Electricity use was high", f"{last:.1f} kWh on {last_day} – about {last / avg:.1f}× your usual {avg:.1f} kWh.")
        db.set_state(f"highuse_{d.date()}", True)

    # 8. Fixed deal: early warning ~7 weeks before the likely end (when the renewal letter is due)
    try:
        from .bright import contract_info
        c = contract_info()
        if c.get("likely_end") and c["type"] != "variable":
            end = datetime.fromisoformat(c["likely_end"])
            days = (end - d).days
            key = f"contract_warn_{c['likely_end']}"
            if 0 < days <= 49 and not db.get_state(key):
                notify.send("contract", "Your fixed deal is likely ending soon",
                            f"It started {c['start']}, so a 12-month fix ends around {end:%d %b %Y}. British Gas should send your "
                            "renewal letter about now – a good time to compare deals.")
                db.set_state(key, True)
    except Exception:
        pass

    # 8b. Daily report at 8am
    if d.hour >= 8 and not db.get_state(f"daily_{d.date()}"):
        from . import report
        title, body = report.daily_text()
        notify.send("daily", title, body)
        db.set_state(f"daily_{d.date()}", True)

    # 9. Weekly summary, Sunday 7pm
    if d.weekday() == 6 and d.hour == 19 and not db.get_state(f"weekly_{d.date()}"):
        with db._lock:
            e7 = db._conn.execute("SELECT SUM(kwh) FROM energy WHERE fuel='electricity' AND ts>=?", (now - 7 * 86400,)).fetchone()[0] or 0
            g7 = db._conn.execute("SELECT SUM(kwh) FROM energy WHERE fuel='gas' AND ts>=?", (now - 7 * 86400,)).fetchone()[0] or 0
            rd = db._conn.execute("SELECT boiler_on, people_home, kids_lights_on, hold FROM readings WHERE ts>=?", (now - 7 * 86400,)).fetchall()
        boiler = sum(5 for b, *_ in rd if b) / 60
        empty = sum(5 for b, p, k, h in rd if b and not p and not k and not h) / 60
        body = f"Boiler ran {boiler:.0f}h" + (f" ({empty:.0f}h with nobody home)" if empty >= 1 else "") + \
               (f" · electricity {e7:.0f} kWh · gas {g7:.0f} kWh" if e7 else "")
        try:
            from . import thermal
            sr = thermal.report(7)
            if sr["saved_total"] >= 0.05:
                body += f" · smart heating saved ~£{sr['saved_total']:.2f}"
        except Exception:
            pass
        notify.send("weekly", "Your week at home", body)
        db.set_state(f"weekly_{d.date()}", True)

    # 9. Monthly report on the 1st, after 9am
    d = datetime.now()
    if d.day == 1 and d.hour >= 9:
        from . import report
        key = f"monthly_{d.year}_{d.month}"
        if not db.get_state(key):
            r = report.current_and_previous()["last"]
            notify.send("monthly", f"Your {r['label']} energy report", report.summary_text(r))
            db.set_state(key, True)
