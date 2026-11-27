"""Import past half-hourly gas and electricity from Octopus Energy (your previous supplier) to fill gaps.

Uses Octopus's free customer API (API key + account number from the Octopus account: Account → Personal
details → API access). Only fills half-hours that are missing (or zero) - Bright's data is never overwritten.
Gas on SMETS2 meters comes back in cubic metres: converted to kWh (× volume correction 1.02264 × calorific value
39.2 ÷ 3.6). If Bright has the same days, the two are compared to pick the right unit automatically.
"""
import time
from datetime import datetime, timezone

import requests

from . import db
from .config import load_secrets, save_secrets

API = "https://api.octopus.energy/v1"
M3_TO_KWH = 1.02264 * 39.2 / 3.6


def _get(url, key, params=None):
    r = requests.get(url, auth=(key, ""), params=params, timeout=30)
    if r.status_code == 401:
        raise ValueError("Octopus didn't accept that API key")
    if r.status_code == 404:
        raise ValueError("Octopus couldn't find that account number")
    r.raise_for_status()
    return r.json()


def meters(key: str, account: str) -> list:
    acc = _get(f"{API}/accounts/{account.strip().upper()}/", key)
    out = []
    for prop in acc.get("properties", []):
        for mp in prop.get("electricity_meter_points", []):
            if mp.get("is_export"):
                continue
            for m in mp.get("meters", []):
                out.append(("electricity", f"{API}/electricity-meter-points/{mp['mpan']}/meters/{m['serial_number']}/consumption/"))
        for mp in prop.get("gas_meter_points", []):
            for m in mp.get("meters", []):
                out.append(("gas", f"{API}/gas-meter-points/{mp['mprn']}/meters/{m['serial_number']}/consumption/"))
    return out


def _consumption(url: str, key: str) -> list:
    rows, params = [], {"page_size": 25000, "order_by": "period",
                        "period_from": datetime.fromtimestamp(time.time() - 400 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    while url:
        j = _get(url, key, params)
        for r in j.get("results", []):
            ts = int(datetime.fromisoformat(r["interval_start"].replace("Z", "+00:00")).timestamp())
            rows.append((ts, float(r["consumption"])))
        url, params = j.get("next"), None
    return rows


def _gas_factor(rows: list) -> float:
    """Octopus gas: m³ (SMETS2) or kWh (SMETS1)? Compare with Bright where both have data."""
    with db._lock:
        have = dict(db._conn.execute("SELECT ts, kwh FROM energy WHERE fuel='gas' AND kwh>0").fetchall())
    a = sum(v for ts, v in rows if ts in have and v > 0)
    b = sum(have[ts] for ts, v in rows if ts in have and v > 0)
    if a > 0.5 and b > 0.5:
        return M3_TO_KWH if b / a > 5 else 1.0
    return M3_TO_KWH            # no overlap: modern (SMETS2) meters report m³


def run(key: str | None = None, account: str | None = None) -> dict:
    s = load_secrets()
    key = key or s.get("octopus_key")
    account = account or s.get("octopus_account")
    if not key or not account:
        raise ValueError("Add the Octopus API key and account number first")
    db.set_state("octopus_status", {"state": "running", "ts": int(time.time())})
    try:
        ms = meters(key, account)
        if not ms:
            raise ValueError("No meters found on that Octopus account")
        save_secrets({"octopus_key": key, "octopus_account": account.strip().upper()})
        added = {"electricity": 0, "gas": 0}
        first, last = None, None
        for fuel, url in ms:
            rows = _consumption(url, key)
            if not rows:
                continue
            f = _gas_factor(rows) if fuel == "gas" else 1.0
            with db._lock:
                have = {ts for (ts,) in db._conn.execute("SELECT ts FROM energy WHERE fuel=? AND kwh>0", (fuel,))}
            new = [(ts, fuel, round(v * f, 4)) for ts, v in rows if ts not in have and v >= 0]
            db.add_energy(new)
            added[fuel] += len(new)
            first = min(first or rows[0][0], rows[0][0])
            last = max(last or rows[-1][0], rows[-1][0])
        st = {"state": "done", "ts": int(time.time()), "added": added,
              "from": datetime.fromtimestamp(first).strftime("%d %b %Y") if first else None,
              "to": datetime.fromtimestamp(last).strftime("%d %b %Y") if last else None}
        db.set_state("octopus_status", st)
        db.add_event("smart", {"octopus": f"imported {added['electricity']} electricity and {added['gas']} gas half-hours from Octopus"})
        return st
    except Exception as e:
        st = {"state": "error", "ts": int(time.time()), "error": str(e)}
        db.set_state("octopus_status", st)
        raise
