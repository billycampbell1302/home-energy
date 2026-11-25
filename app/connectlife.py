"""ConnectLife (Hisense / Gorenje / ASKO) appliances - the washer-dryer.

Step 1: log in and record everything the machine reports, so we can see which fields matter for this model
(running/finished, programme, time left, energy). Only *changes* are logged, polled every 3 minutes to stay
well clear of ConnectLife's rate limits. Uses the community `connectlife` library (unofficial API).
"""
import json
import logging
import time

from . import db
from .config import load_secrets, save_secrets

log = logging.getLogger("home-energy")

with db._lock:
    db._conn.execute("CREATE TABLE IF NOT EXISTS appliance_log (ts INTEGER, puid TEXT, status TEXT)")
    db._conn.execute("CREATE INDEX IF NOT EXISTS appliance_log_ts ON appliance_log(ts)")
    db._conn.commit()


def is_configured() -> bool:
    return bool(load_secrets().get("connectlife", {}).get("username"))


def _api(username=None, password=None):
    from connectlife.api import ConnectLifeApi
    s = load_secrets().get("connectlife", {})
    return ConnectLifeApi(username or s["username"], password or s["password"])


async def login(username: str, password: str) -> list:
    api = _api(username, password)
    await api.login()
    save_secrets({"connectlife": {"username": username, "password": password}})
    db.add_event("setup", "ConnectLife connected")
    return await refresh(api)


async def refresh(api=None) -> list:
    api = api or _api()
    raw = await api.get_appliances_json()
    try:
        items = api._normalize_appliance_payloads(raw)
    except Exception:
        items = raw if isinstance(raw, list) else raw.get("data", raw)
    now = int(time.time())
    last = db.get_state("connectlife_last", {})
    out = []
    for a in items if isinstance(items, list) else []:
        puid = a.get("puid") or a.get("deviceId")
        status = a.get("statusList") or a.get("status_list") or {}
        entry = {
            "puid": puid,
            "name": a.get("deviceNickName") or a.get("deviceTypeName") or "Appliance",
            "type": a.get("deviceTypeName"),
            "type_code": a.get("deviceTypeCode"),
            "feature_code": a.get("deviceFeatureCode"),
            "offline": str(a.get("offlineState")) in ("0", "false", "False"),   # library: 1 = online
            "status": status,
            "updated": now,
        }
        sig = json.dumps(status, sort_keys=True, default=str)
        if last.get(puid) != sig:
            with db._lock:
                db._conn.execute("INSERT INTO appliance_log VALUES (?,?,?)", (now, puid, sig))
                db._conn.commit()
            last[puid] = sig
        out.append(entry)
    db.set_state("connectlife_last", last)
    db.set_state("connectlife_appliances", out)
    return out


def appliances() -> list:
    return db.get_state("connectlife_appliances", [])


def changed_fields(puid: str, hours: int = 48) -> list:
    """Which status fields have changed recently - these are the interesting ones (running state, time left...)."""
    since = int(time.time()) - hours * 3600
    with db._lock:
        rows = db._conn.execute("SELECT status FROM appliance_log WHERE puid=? AND ts>=? ORDER BY ts", (puid, since)).fetchall()
    seen, changed = {}, set()
    for (st,) in rows:
        for k, v in json.loads(st).items():
            if k in seen and seen[k] != v:
                changed.add(k)
            seen[k] = v
    return sorted(changed)


# ---------- Washer-dryer: what the fields mean (Hisense, from the readings it sent) ----------
def _num(st, *keys):
    for k in keys:
        if k in st:
            try:
                return float(st[k])
            except (TypeError, ValueError):
                pass
    return None


def wash_state(a: dict) -> dict:
    """Running / time left / finish time / drying, worked out from this machine's own field names."""
    st = a.get("status") or {}
    rem = _num(st, "Selected_program_remaining_time_in_minutes")
    total = _num(st, "Selected_program_total_time_in_minutes", "Selected_program_total_running_time_in_minutes")
    status = _num(st, "machine_status")
    phase = _num(st, "Current_program_phase")
    # Running: the machine says so, or the countdown has started
    running = bool((status not in (None, 0)) or (rem is not None and total and 0 < rem < total))
    kwh = None
    i, d = _num(st, "Electricit_consumption_int"), _num(st, "Electricit_consumption_decimal")
    if i is not None:
        kwh = i + (d or 0) / 100
    return {"running": running and not a.get("offline"), "remaining_min": int(rem) if rem is not None else None,
            "finish_ts": int(time.time() + rem * 60) if running and rem else None,
            "drying": (_num(st, "dry_time") or 0) > 0, "phase": phase, "status": status, "kwh": kwh,
            "door_open": _num(st, "Door_status") == 1, "delay_end": _num(st, "DelayEndTime")}


def track_runs():
    """Called after each refresh: notices a wash starting and finishing, logs it, and tells the dehumidifier
    to expect wet washing when a wash (without drying) ends."""
    from . import notify
    runs = db.get_state("wash_runs", [])
    cur = db.get_state("wash_current", {})
    now = int(time.time())
    for a in appliances():
        w = wash_state(a)
        c = cur.get(a["puid"])
        if w["running"] and not c:
            cur[a["puid"]] = {"start": now, "drying": w["drying"], "kwh0": w["kwh"]}
            db.add_event("device", {"name": a["name"], "kind": "washer", "state": "started"})
        elif w["running"] and c:
            c["drying"] = c["drying"] or w["drying"]
        elif not w["running"] and c and not a.get("offline"):
            mins = (now - c["start"]) / 60
            if mins >= 10:
                h = time.localtime(c["start"]).tm_hour
                from .bright import tariff_settings
                t = tariff_settings()
                ns, ne = (int(x.split(":")[0]) for x in (t.get("night_start", "00:30"), t.get("night_end", "07:30")))
                night = ns <= h < ne
                kwh = (w["kwh"] - c["kwh0"]) if (w["kwh"] is not None and c.get("kwh0") is not None and w["kwh"] >= c["kwh0"]) else None
                runs.append({"start": c["start"], "end": now, "mins": round(mins), "dried": c["drying"], "night": night, "kwh": kwh})
                db.set_state("wash_runs", runs[-100:])
                notify.send("washing_done", f"{a['name']} has finished",
                            "Clothes are dry – time to empty it." if c["drying"] else "Time to hang the washing out.")
                db.add_event("device", {"name": a["name"], "kind": "washer", "state": "finished", "mins": round(mins)})
                if not c["drying"]:
                    db.set_state("washing_expected_until", now + 3 * 3600)    # the dehumidifier will see wet washing soon
            cur.pop(a["puid"], None)
    db.set_state("wash_current", cur)


def wash_summary(days: int = 30) -> dict:
    """Washes on the day rate vs the cheap rate, and what moving them would save."""
    from .bright import tariff_settings
    since = time.time() - days * 86400
    runs = [r for r in db.get_state("wash_runs", []) if r["start"] >= since]
    day = [r for r in runs if not r["night"]]
    t = tariff_settings()
    spread = (t["day_rate"] - t["night_rate"]) / 100
    kwh_each = lambda r: r["kwh"] if r.get("kwh") else (3.5 if r["dried"] else 0.8)    # typical wash / wash+dry
    saving = sum(kwh_each(r) for r in day) * spread
    return {"runs": len(runs), "day_runs": len(day), "dried": sum(r["dried"] for r in runs),
            "saving_period": round(saving, 2), "saving_year": round(saving * 365 / days, 0)}
