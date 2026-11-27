"""Tariff comparison - every deal open to you, priced on your OWN half-hourly usage (like the Tariffs tab in
Dad's app). Read-only: it never switches anything.

Sources: the public price lists of Octopus, EDF and E.ON Next (no login). British Gas and others don't publish
their prices, so they can't be compared automatically.
For each deal:
  electricity  single-rate, Economy 7 (day/night, using your meter's 00:30-07:30) or half-hourly (Agile, Cosy...)
               priced on your real half-hours from the last 30 complete days, plus the standing charge
  gas          priced on your real daily gas use, plus the standing charge
Deals that need things you don't have (an electric car, heat pump, solar + battery, storage heaters) or that
are special offers are listed separately. Your current British Gas deal is priced the same way (with PeakSave
Sundays if you've joined), so the comparison is like for like. A switch also costs any exit fee on your fix.
"""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from . import db

SOURCES = [
    {"id": "octopus", "name": "Octopus", "base": "https://api.octopus.energy/v1", "query": "brand=OCTOPUS_ENERGY&"},
    {"id": "edf", "name": "EDF", "base": "https://api.edfgb-kraken.energy/v1", "query": ""},
    {"id": "eonnext", "name": "E.ON Next", "base": "https://api.eonnext-kraken.energy/v1", "query": ""},
]
DAYS = 30
_lock = threading.Lock()


def _get(url, params=None):
    for k in range(3):
        try:
            r = requests.get(url, params=params, timeout=30, headers={"accept": "application/json"})
            if r.status_code == 404:
                return None
            if r.ok:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(1.5 * (k + 1))
    return None


def region() -> str:
    """Grid region letter from the postcode (Tiptree is Eastern England, _A, if unknown)."""
    st = db.get_state("tariff_region")
    if st:
        return st
    pc = db.get_state("postcode")
    if pc:
        j = _get("https://api.octopus.energy/v1/industry/grid-supply-points/", {"postcode": pc})
        try:
            g = j["results"][0]["group_id"]
            db.set_state("tariff_region", g)
            return g
        except (TypeError, KeyError, IndexError):
            pass
    return "_A"


def _open(p, now):
    t = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None
    return (not p.get("is_prepay") and not p.get("is_business") and p.get("direction") != "EXPORT"
            and (not p.get("available_to") or t(p["available_to"]) > now) and (not p.get("available_from") or t(p["available_from"]) <= now))


def _latest(prods):
    """Only the newest version of each deal."""
    def key(p):
        n = str(p.get("display_name") or p.get("full_name") or p["code"]).lower()
        n = re.sub(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}\b", "", n)
        n = re.sub(r"\bv\d+(\.\d+)*\b", "", n)
        return re.sub(r"\s+", " ", n).strip()
    best = {}
    for p in prods:
        k, f = key(p), p.get("available_from") or ""
        if k not in best or f > (best[k].get("available_from") or ""):
            best[k] = p
    return list(best.values())


def _blocked(p) -> str:
    nm = f"{p.get('display_name', '')} {p.get('full_name', '')} {p.get('description', '')}"
    why = []
    if p.get("is_restricted"):
        why.append("restricted offer")
    if re.search(r"\bzero\b|staff|employee|pilot|trial|loyalty|existing customer", nm, re.I):
        why.append("special offer, not open to everyone")
    if re.search(r"intelligent|\bgo\b|\bEV\b|electric (car|vehicle|driver)|charger|drive", nm, re.I):
        why.append("needs an electric car or charger")
    if re.search(r"heat ?pump|cosy", nm, re.I):
        why.append("needs a heat pump")
    if re.search(r"\bsnug\b|storage heat", nm, re.I):
        why.append("for electric storage heaters")
    if re.search(r"flux|solar|battery|export", nm, re.I):
        why.append("needs solar and a battery")
    return "; ".join(why)


def _rates(url, frm, to):
    """[(from_ts, to_ts, p/kWh)] for direct debit."""
    rows, params = [], {"page_size": 1500, "period_from": frm, "period_to": to}
    pages = 0
    while url and pages < 20:
        j = _get(url, params)
        if not j:
            return None
        for r in j.get("results", []):
            if r.get("payment_method") not in (None, "DIRECT_DEBIT"):
                continue
            a = datetime.fromisoformat(r["valid_from"].replace("Z", "+00:00")).timestamp()
            b = datetime.fromisoformat(r["valid_to"].replace("Z", "+00:00")).timestamp() if r.get("valid_to") else 9e12
            rows.append((a, b, float(r["value_inc_vat"])))
        url, params, pages = j.get("next"), None, pages + 1
    return sorted(rows)


def _at(rows, t):
    v = None
    for a, b, p in rows:
        if a <= t < b:
            v = p
    return v


def _usage():
    """Last 30 complete days: electricity half-hours, gas per day."""
    end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=45)
    with db._lock:
        rows = db._conn.execute("SELECT ts, fuel, kwh FROM energy WHERE ts>=? AND ts<?", (start.timestamp(), end.timestamp())).fetchall()
    elec, gas = {}, {}
    for ts, fuel, kwh in rows:
        d = datetime.fromtimestamp(ts).date()
        if fuel == "electricity":
            elec.setdefault(d, []).append((ts, kwh))
        else:
            gas[d] = gas.get(d, 0) + kwh
    days = sorted(d for d, v in elec.items() if sum(1 for _, k in v if k > 0) >= 44)[-DAYS:]
    return days, elec, gas


def _night(ts, a="00:30", b="07:30"):
    t = datetime.fromtimestamp(ts)
    m = t.hour * 60 + t.minute
    ma, mb = int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:])
    return ma <= m < mb if ma < mb else (m >= ma or m < mb)


def scan() -> dict:
    """Fetch every open deal and price it on your usage. Takes a minute or two; run in the background."""
    from .bright import tariff_settings
    if not _lock.acquire(blocking=False):
        return db.get_state("tariff_scan", {})
    try:
        db.set_state("tariff_scan_state", {"state": "running", "ts": int(time.time())})
        now = time.time()
        reg = region()
        days, elec, gas = _usage()
        if len(days) < 7:
            raise ValueError("Needs at least 7 full days of meter data")
        frm = datetime.combine(days[0], datetime.min.time()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        to = (datetime.combine(days[-1], datetime.min.time()) + timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        n = len(days)
        slots = [(ts, k) for d in days for ts, k in elec[d] if k > 0]
        gas_days = [d for d in days if gas.get(d, 0) > 0]
        gas_kwh = sum(gas[d] for d in gas_days)
        t = tariff_settings()
        ns, ne = t.get("night_start", "00:30"), t.get("night_end", "07:30")
        day_kwh = sum(k for ts, k in slots if not _night(ts, ns, ne))
        night_kwh = sum(k for ts, k in slots if _night(ts, ns, ne))
        scale = 365 / n

        # Your current deal, priced the same way (PeakSave Sundays: half price 11:00-16:00 on Sundays)
        ps = sum(k for ts, k in slots if datetime.fromtimestamp(ts).weekday() == 6 and 11 <= datetime.fromtimestamp(ts).hour < 16
                 and not _night(ts, ns, ne)) if db.get_state("peaksave") else 0
        cur_e = (day_kwh * t["day_rate"] + night_kwh * t["night_rate"] + n * t["e7_standing"] - ps * t["day_rate"] / 2) / 100
        cur_g = (gas_kwh * t["gas_rate"] + len(gas_days) * t["gas_standing"]) / 100 if gas_days else None
        current = {"name": "British Gas (your current deal)", "elec": round(cur_e * scale, 2), "gas": round(cur_g * 365 / len(gas_days), 2) if cur_g else None,
                   "peaksave": bool(db.get_state("peaksave"))}

        elec_deals, gas_deals = [], []

        def price_product(src, p):
            d = _get(f"{src['base']}/products/{p['code']}/")
            if not d:
                return [], []
            e_out, g_out = [], []
            name = p.get("display_name") or p.get("full_name") or p["code"]
            base = {"supplier": src["name"], "name": name, "code": p["code"], "fixed": not p.get("is_variable"),
                    "term": p.get("term"), "blocked": _blocked(p), "tracker": bool(p.get("is_tracker"))}
            pay = lambda reg_tariffs: (reg_tariffs or {}).get(reg, {}).get("direct_debit_monthly") if reg_tariffs else None
            s1, s2, g1 = pay(d.get("single_register_electricity_tariffs")), pay(d.get("dual_register_electricity_tariffs")), pay(d.get("single_register_gas_tariffs"))
            url = f"{src['base']}/products/{p['code']}"
            if s1 and s1.get("code"):
                r = _rates(f"{url}/electricity-tariffs/{s1['code']}/standard-unit-rates/", frm, to)
                if r:
                    cost = sum(k * (_at(r, ts) or 0) for ts, k in slots) / 100 + n * float(s1["standing_charge_inc_vat"]) / 100
                    half = len({x[2] for x in r}) > 3
                    e_out.append({**base, "kind": "half-hourly (smart meter)" if half else "single rate",
                                  "year": round(cost * scale, 2), "standing": float(s1["standing_charge_inc_vat"]),
                                  "rate": None if half else r[-1][2], "exit": float(s1.get("exit_fees_inc_vat") or 0) / 100})
            if s2 and s2.get("code"):
                rd = _rates(f"{url}/electricity-tariffs/{s2['code']}/day-unit-rates/", frm, to)
                rn = _rates(f"{url}/electricity-tariffs/{s2['code']}/night-unit-rates/", frm, to)
                if rd and rn:
                    cost = sum(k * (_at(rn if _night(ts, ns, ne) else rd, ts) or 0) for ts, k in slots) / 100 \
                        + n * float(s2["standing_charge_inc_vat"]) / 100
                    e_out.append({**base, "kind": "Economy 7", "year": round(cost * scale, 2), "standing": float(s2["standing_charge_inc_vat"]),
                                  "rate": f"{rd[-1][2]:.1f}p / {rn[-1][2]:.1f}p", "exit": float(s2.get("exit_fees_inc_vat") or 0) / 100})
            if g1 and g1.get("code") and gas_days:
                r = _rates(f"{url}/gas-tariffs/{g1['code']}/standard-unit-rates/", frm, to)
                if r:
                    cost = sum(gas[dd] * (_at(r, datetime.combine(dd, datetime.min.time()).timestamp() + 43200) or 0) for dd in gas_days) / 100 \
                        + len(gas_days) * float(g1["standing_charge_inc_vat"]) / 100
                    g_out.append({**base, "kind": "gas", "year": round(cost * 365 / len(gas_days), 2), "standing": float(g1["standing_charge_inc_vat"]),
                                  "rate": r[-1][2], "exit": float(g1.get("exit_fees_inc_vat") or 0) / 100})
            return e_out, g_out

        jobs = []
        with ThreadPoolExecutor(8) as ex:
            for src in SOURCES:
                plist, url, pages = [], f"{src['base']}/products/?{src['query']}is_business=false&page_size=100", 0
                while url and pages < 5:
                    j = _get(url)
                    if not j:
                        break
                    plist += j.get("results", [])
                    url, pages = j.get("next"), pages + 1
                for p in _latest([p for p in plist if _open(p, now)]):
                    jobs.append(ex.submit(price_product, src, p))
            for f in jobs:
                try:
                    e, g = f.result()
                    elec_deals += e
                    gas_deals += g
                except Exception:
                    pass
        elec_deals.sort(key=lambda x: x["year"])
        gas_deals.sort(key=lambda x: x["year"])
        out = {"made": int(time.time()), "region": reg, "days": n, "from": days[0].isoformat(), "to": days[-1].isoformat(),
               "use": {"elec_day_kwh": round(day_kwh, 1), "elec_night_kwh": round(night_kwh, 1), "gas_kwh": round(gas_kwh, 1)},
               "current": current, "elec": elec_deals, "gas": gas_deals}
        db.set_state("tariff_scan", out)
        db.set_state("tariff_scan_state", {"state": "done", "ts": int(time.time())})
        return out
    except Exception as e:
        db.set_state("tariff_scan_state", {"state": "error", "ts": int(time.time()), "error": str(e)})
        raise
    finally:
        _lock.release()
