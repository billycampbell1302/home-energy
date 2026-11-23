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
