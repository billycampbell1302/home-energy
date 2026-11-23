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

    # 3. Washer-dryer finished (time-remaining field counts down to zero)
    from . import connectlife
    if connectlife.is_configured():
        prev = db.get_state("appliance_remaining", {})
        for a in connectlife.appliances():
            rem = None
            for k, v in (a.get("status") or {}).items():
                if "remain" in k.lower() or "time_left" in k.lower():
                    try:
                        rem = float(v)
                    except (TypeError, ValueError):
                        pass
            p = prev.get(a["puid"])
            if rem is not None:
                notify.once(f"wash_{a['puid']}", p is not None and p > 0 and rem == 0, "washing_done",
                            f"{a['name']} has finished", "Time to empty it.")
                prev[a["puid"]] = rem
        db.set_state("appliance_remaining", prev)

    # 4. Monthly report on the 1st, after 9am
    d = datetime.now()
    if d.day == 1 and d.hour >= 9:
        from . import report
        key = f"monthly_{d.year}_{d.month}"
        if not db.get_state(key):
            r = report.current_and_previous()["last"]
            notify.send("monthly", f"Your {r['label']} energy report", report.summary_text(r))
            db.set_state(key, True)
