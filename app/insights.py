"""Plain-English summary: what's going well, what you could do, and what needs doing.

Everything here is worked out from the data the app has collected - no manual input.
Each item: {"level": "todo" | "tip" | "good", "title": ..., "detail": ...}
"""
import time
from datetime import datetime

from . import db
from .bright import analysis, bright, contract_guess

GAS_PER_DEGREE = 0.10   # rule of thumb: each 1°C lower on the thermostat saves roughly 10% of heating gas


def _readings(days: int):
    since = int(time.time()) - days * 86400
    with db._lock:
        cur = db._conn.execute(
            "SELECT ts, indoor_temp, target_temp, boiler_on, people_home, kids_lights_on, hold FROM readings "
            "WHERE ts >= ? ORDER BY ts", (since,))
        return cur.fetchall()


def build(hive_ok: bool, tuya_ok: bool, errors: dict | None = None) -> list:
    items = []
    add = lambda level, title, detail="": items.append({"level": level, "title": title, "detail": detail})
    names = {"hive": "Hive", "tuya": "Smart Life", "bright": "Bright (smart meter)", "weather": "Weather", "smartthings": "SmartThings", "connectlife": "ConnectLife"}
    for k, msg in (errors or {}).items():
        add("todo", f"{names.get(k, k)} isn't responding", f"It'll keep retrying by itself. Last error: {msg[:120]}")

    # ---------- setup ----------
    if not hive_ok:
        add("todo", "Connect Hive", "Setup → Hive heating. Nothing can be learned about the heating until it's connected.")
    if not bright.is_configured():
        add("todo", "Connect your smart meter", "Setup → Smart meter (Bright).")
    with db._lock:
        presence_events = db._conn.execute("SELECT COUNT(*) FROM events WHERE kind='presence'").fetchone()[0]
    if presence_events == 0:
        add("todo", "Check the arrive/leave shortcuts", "No arrivals or departures recorded yet – turn Wi-Fi off and on to test.")

    # ---------- heating (last 7 days of readings) ----------
    rows = _readings(7)
    step = 5 * 60
    if rows:
        days_of_data = (rows[-1][0] - rows[0][0]) / 86400
        boiler_h = sum(step for r in rows if r[3]) / 3600
        empty_h = sum(step for r in rows if r[3] and r[4] == 0 and not r[5] and not r[6]) / 3600
        home_targets = [r[2] for r in rows if r[2] and r[4] and r[2] > 10]
        avg_target = sum(home_targets) / len(home_targets) if home_targets else None
        if empty_h >= 1:
            add("tip", f"Heating ran {empty_h:.0f} hours with nobody home this week",
                "That's the main saving the smart heating (phase 3) will go after – turning down automatically when you're both out.")
        elif days_of_data >= 2:
            add("good", "Heating isn't running while the flat's empty", f"Boiler ran {boiler_h:.0f} hours this week, all while someone was in.")
        if avg_target and avg_target > 20.5:
            add("tip", f"Heating is set to about {avg_target:.1f}° when you're home",
                f"Each 1° lower saves roughly {int(GAS_PER_DEGREE * 100)}% of heating gas. 19–20° is comfortable for most people.")
        if days_of_data < 14:
            add("tip", f"Learning: {days_of_data:.0f} of 14 days of heating data collected",
                "Once there's two weeks, the smart heating can be built from your real routine.")
        else:
            add("good", "Enough heating data collected", "Ready for the smart heating (phase 3) – ask Claude to build it.")

    # ---------- electricity / tariff ----------
    if bright.is_configured():
        a = analysis(90)
        el = a.get("electricity")
        if a.get("latest") and time.time() - a["latest"] > 3 * 86400:
            behind = int((time.time() - a["latest"]) / 86400)
            add("todo", f"Bright's meter data is {behind} days behind",
                "If it hasn't caught up by the weekend, email support@glowmarkt.com and ask for a manual data pull.")
        if el and el["total_kwh"] >= 5:
            saving = el["per_year_single"] - el["per_year_e7"]
            rough = " (rough – only a few weeks of data)" if el["days_covered"] < 28 else ""
            if saving >= 0:
                add("good", f"Economy 7 suits you – about £{saving:.0f}/year cheaper than single rate{rough}",
                    f"{round(el['night_share'] * 100)}% of your electricity is used in the cheap hours.")
            else:
                add("todo", f"Single rate would be about £{-saving:.0f}/year cheaper{rough}",
                    "Worth asking British Gas about switching tariff.")
            # Moving appliance loads to the night
            spread = a["tariff"]["day_rate"] - a["tariff"]["night_rate"]
            if spread > 0:
                per_load = 1.0 * spread / 100   # ~1 kWh per wash/dishwasher cycle
                add("tip", "Run the washing machine and dishwasher overnight",
                    f"Using the delay timer to start them in the cheap hours saves about {per_load * 100:.0f}p a load – "
                    f"around £{per_load * 7 * 52:.0f}/year at one load a day.")
            if el["baseload_w"] > 120:
                add("tip", f"{el['baseload_w']} W is on all the time (about £{el['baseload_cost_year']}/year)",
                    "Higher than typical for a flat – a smart plug with energy monitoring can find what it is.")
            elif el["baseload_w"] > 0:
                add("good", f"Always-on use is low ({el['baseload_w']} W)", "Smart plugs for standby wouldn't pay for themselves.")
        c = contract_guess()
        if c["type"] == "variable":
            add("todo", "You seem to be on a variable tariff", "Your prices follow the price cap. Compare fixed deals when a rise is announced.")
        elif c["type"] == "fixed":
            add("good", "You seem to be on a fixed tariff", "Price cap changes don't affect you until it ends.")
        elif c.get("next_check"):
            add("tip", "Fixed or variable tariff?", c["why"])

    order = {"todo": 0, "tip": 1, "good": 2}
    return sorted(items, key=lambda i: order[i["level"]])
