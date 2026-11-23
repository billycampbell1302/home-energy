"""Smart meter data from Hildebrand's Bright app (Glowmarkt API).

Bright pulls half-hourly readings from the national smart meter network (DCC), whatever the supplier.
Readings usually lag by a day or so. We copy them into our own database so the dashboard can
analyse them (Economy 7 night share, tariff comparison, standby usage) and the heating can learn from gas use.
"""
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from . import db
from .config import load_secrets, save_secrets

API = "https://api.glowmarkt.com/api/v0-1"
APP_ID = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"   # the Bright app's public application id
TIMEOUT = 30
BACKFILL_DAYS = 400                               # Bright can supply up to ~13 months of history


class BrightError(Exception):
    pass


class BrightClient:
    def __init__(self):
        self._lock = threading.RLock()
        self._token = None
        self._token_exp = 0
        self.last_sync = 0.0

    # ---------- auth ----------
    def login(self, username: str, password: str):
        r = requests.post(f"{API}/auth", json={"username": username, "password": password, "applicationId": APP_ID},
                          headers={"applicationId": APP_ID}, timeout=TIMEOUT)
        j = r.json() if r.text else {}
        if r.status_code != 200 or not j.get("valid"):
            raise BrightError("Bright didn't accept that email and password.")
        self._token, self._token_exp = j["token"], j.get("exp", time.time() + 6 * 86400)
        save_secrets({"bright": {"username": username, "password": password}})
        db.add_event("setup", "Bright smart meter data connected")

    def is_configured(self) -> bool:
        return bool(load_secrets().get("bright", {}).get("username"))

    def _get(self, path: str, params: dict | None = None):
        with self._lock:
            if not self._token or time.time() > self._token_exp - 3600:
                s = load_secrets()["bright"]
                self.login(s["username"], s["password"])
            r = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT,
                             headers={"applicationId": APP_ID, "token": self._token, "Content-Type": "application/json"})
            if r.status_code >= 300:
                raise BrightError(f"Bright API {r.status_code}: {r.text[:200]}")
            return r.json()

    # ---------- data ----------
    def resources(self) -> dict:
        """Map of fuel -> resource id for the half-hourly consumption feeds."""
        out = {}
        for res in self._get("/resource"):
            c = res.get("classifier", "")
            if c == "electricity.consumption":
                out["electricity"] = res["resourceId"]
            elif c == "gas.consumption":
                out["gas"] = res["resourceId"]
        return out

    def sync(self) -> dict:
        """Fetch any half-hours we don't have yet (and the back history on first run)."""
        added = {}
        for fuel, rid in self.resources().items():
            try:
                self._get(f"/resource/{rid}/catchup")   # ask Bright to pull the latest from the meter network
            except Exception:
                pass
            last = db.last_energy_ts(fuel)
            now = _floor30(datetime.now(timezone.utc))
            if last:
                # Re-read the last couple of days too: Bright fills in late half-hours after the fact.
                start = max(datetime.fromtimestamp(last, timezone.utc) - timedelta(days=2), now - timedelta(days=12))
            else:
                start = now - timedelta(days=BACKFILL_DAYS)
            start, end = _floor30(start), now
            n = 0
            while end - start >= timedelta(minutes=30):
                chunk_end = min(start + timedelta(days=10), end)   # API limit: 10 days of half-hours per call
                j = self._get(f"/resource/{rid}/readings", {
                    "from": start.strftime("%Y-%m-%dT%H:%M:%S"), "to": chunk_end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "period": "PT30M", "function": "sum", "offset": 0,
                })
                rows = [(int(ts), fuel, float(v)) for ts, v in j.get("data", []) if v is not None]
                n += db.add_energy(rows)
                start = chunk_end
            added[fuel] = n
        self.last_sync = time.time()
        return added


    def meter_tariff(self) -> dict:
        """Reads the prices the supplier has programmed into the smart meter (via Bright's cost feeds).

        The response format isn't well documented, so this collects every unit rate and standing charge it can
        find and works out pence. Returns what it found plus the raw data (shown on the Setup page if unsure).
        """
        found, raw = {}, {}
        for res in self._get("/resource"):
            c = res.get("classifier", "")
            if c not in ("electricity.consumption.cost", "gas.consumption.cost"):
                continue
            fuel = c.split(".")[0]
            try:
                j = self._get(f"/resource/{res['resourceId']}/tariff")
            except Exception as e:
                raw[fuel] = f"error: {e}"
                continue
            raw[fuel] = j
            rates, standing = set(), []
            _collect(j, rates, standing)
            found[fuel] = {"rates": sorted(_pence(r) for r in rates), "standing": [_pence(x) for x in standing[:1]]}
        out = {}
        e = found.get("electricity")
        if e:
            if len(e["rates"]) >= 2:          # Economy 7: two prices -> higher is day, lower is night
                out["day_rate"], out["night_rate"] = e["rates"][-1], e["rates"][0]
            elif e["rates"]:
                out["single_rate"] = e["rates"][0]
            if e["standing"]:
                out["e7_standing"] = out["single_standing"] = e["standing"][0]
        g = found.get("gas")
        if g:
            if g["rates"]:
                out["gas_rate"] = g["rates"][0]
            if g["standing"]:
                out["gas_standing"] = g["standing"][0]
        return {"values": out, "found": found, "raw": raw}


def _floor30(t: datetime) -> datetime:
    """Round down to the start of a half-hour (Bright rejects ranges that don't line up)."""
    return t.replace(minute=t.minute - t.minute % 30, second=0, microsecond=0)


def _collect(node, rates: set, standing: list, key: str = ""):
    """Walks the JSON picking out numbers stored under rate-like and standing-charge-like keys."""
    if isinstance(node, dict):
        for k, v in node.items():
            _collect(v, rates, standing, k.lower())
    elif isinstance(node, list):
        for v in node:
            _collect(v, rates, standing, key)
    elif isinstance(node, (int, float)) and not isinstance(node, bool) and node > 0:
        if any(x in key for x in ("vat", "discount", "threshold", "percent")):
            return
        if "standing" in key:
            standing.append(float(node))
        elif key in ("rate", "unitrate", "price", "tierrate") or key.endswith("rate"):
            rates.add(float(node))


def _pence(v: float) -> float:
    """Bright reports pounds (e.g. 0.2632); the dashboard uses pence (26.32)."""
    return round(v * 100, 2) if v < 2 else round(v, 2)


bright = BrightClient()


# ---------------- analysis ----------------
def _in_window(ts: int, start: str, end: str) -> bool:
    """Is this half-hour inside the Economy 7 night window? Times are GMT (meters don't change for summer time)."""
    t = datetime.fromtimestamp(ts, timezone.utc)
    m = t.hour * 60 + t.minute
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    s, e = sh * 60 + sm, eh * 60 + em
    return s <= m < e if s < e else (m >= s or m < e)


CAP_CHANGE_DATES = ["2027-01-01", "2027-04-01", "2027-07-01", "2027-10-01", "2028-01-01", "2028-04-01"]
AUTO_KEYS = ("day_rate", "night_rate", "e7_standing", "gas_rate", "gas_standing")


def auto_update_tariff(client=None) -> dict | None:
    """Reads the prices programmed into the meter and applies any change automatically, logging it.

    Runs after every Bright sync, so a price change by the supplier (or a new price cap) shows up without anyone
    typing anything. Keeps a dated history, which is also how we tell a fixed tariff from a variable one.
    """
    client = client or bright
    mt = client.meter_tariff()
    found = {k: v for k, v in mt["values"].items() if k in AUTO_KEYS}
    if not found:
        return None
    meter_start = _tariff_start_from_meter(mt.get("raw", {}))
    if meter_start and not db.get_state("tariff_start"):
        db.set_state("tariff_start", {"date": meter_start, "source": "meter"})
    current = tariff_settings()
    changed = {k: (current.get(k), v) for k, v in found.items() if abs((current.get(k) or 0) - v) > 0.005}
    was = contract_info()                   # what we believed before any change (fixed/variable)
    history = db.get_state("tariff_history", [])
    today = datetime.now().date().isoformat()
    if changed or not history:
        db.set_state("tariff", {**current, **found})
        history.append({"date": today, **found})
        db.set_state("tariff_history", history[-50:])
        if not history[:-1] and not db.get_state("tariff_start"):
            db.set_state("tariff_start", {"date": today, "source": "first seen"})
        if changed and len(history) > 1:
            db.add_event("tariff", {k: f"{a}p → {b}p" for k, (a, b) in changed.items()})
            db.set_state("tariff_start", {"date": today, "source": "price change"})
            db.set_state("contract_confirm", {})
            try:
                from . import notify
                names = {"day_rate": "day", "night_rate": "night", "e7_standing": "standing", "gas_rate": "gas", "gas_standing": "gas standing"}
                detail = ", ".join(f"{names.get(k, k)} {a}p → {b}p" for k, (a, b) in changed.items())
                if was.get("type") == "fixed":
                    notify.send("contract", "Your fixed deal has ended",
                                f"British Gas changed your prices: {detail}. Worth comparing deals now – you may be on a variable tariff.")
                else:
                    notify.send("tariff_change", "Your energy prices changed", detail)
            except Exception:
                pass
    db.set_state("tariff_checked", today)
    return changed


def _tariff_start_from_meter(raw) -> str | None:
    """Bright's tariff data carries the date the current prices were put on the meter. Field names aren't
    documented, so look for any date under a from/start/effective key and take the latest one not in the future."""
    found = []

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k.lower())
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif any(w in key for w in ("from", "start", "effective", "activat")):
            d = None
            if isinstance(node, (int, float)) and node > 10**9:
                d = datetime.fromtimestamp(node / 1000 if node > 10**11 else node).date()
            elif isinstance(node, str) and len(node) >= 10 and node[4] == "-":
                try:
                    d = datetime.fromisoformat(node[:10]).date()
                except ValueError:
                    pass
            if d:
                found.append(d)

    walk(raw)
    today = datetime.now().date()
    past = [d for d in found if d <= today and d.year >= 2015]
    return max(past).isoformat() if past else None


FIX_MONTHS = 12   # British Gas's standard fixed deals run 12 months


def contract_info() -> dict:
    """Everything known about the current deal: real start date, fixed/variable, likely end (until confirmed)."""
    g = contract_guess()
    conf = db.get_state("contract_confirm", {})
    if conf.get("type") in ("fixed", "variable"):
        g = {"type": conf["type"], "why": "Confirmed by you."}
    st = db.get_state("tariff_start")
    out = {"type": g["type"], "why": g["why"], "start": st["date"] if st else None,
           "start_source": st["source"] if st else None, "likely_end": None}
    out["confirmed"] = {k: conf.get(k) for k in ("prices", "type", "end")}
    if conf.get("end_date"):
        out["likely_end"] = conf["end_date"]
        out["end_confirmed"] = True
        return out
    if st and g["type"] != "variable":
        d = datetime.fromisoformat(st["date"]).date()
        y, m = d.year + (d.month + FIX_MONTHS - 1) // 12, (d.month + FIX_MONTHS - 1) % 12 + 1
        try:
            out["likely_end"] = d.replace(year=y, month=m).isoformat()
        except ValueError:              # 29-31st in a shorter month
            out["likely_end"] = d.replace(year=y, month=m, day=28).isoformat()
    return out


# Ofgem price cap, Direct Debit, Great Britain average (gas incl. 5% VAT). Updated each quarter.
PRICE_CAPS = [
    {"from": "2026-10-01", "to": "2026-12-31", "period": "Oct–Dec 2026", "gas_rate": 7.97, "gas_standing": 29.68},
]


def current_cap():
    today = datetime.now().date().isoformat()
    for c in PRICE_CAPS:
        if c["from"] <= today <= c["to"]:
            return {**c, "regional_margin": 0.6}   # regions differ from the GB average by a fraction of a penny
    return None


def contract_guess() -> dict:
    """Fixed or variable? Variable tariffs move with the price cap on 1 Jan/Apr/Jul/Oct; fixed ones don't.

    Watches the meter's prices around each cap change date. Needs one cap change to have happened since we
    started watching, so it says 'not known yet' until then.
    """
    history = db.get_state("tariff_history", [])
    if len(history) == 0:
        return {"type": "unknown", "why": "Waiting for the first price reading from your meter."}
    # Quick answer: a variable (price-capped) tariff can never charge MORE than the cap. If your gas unit rate is
    # above the cap, you must be on a fixed deal. (Electricity isn't used here - the cap figures we have are for
    # single-rate meters, and Economy 7 prices are split differently.)
    cap = current_cap()
    gas = tariff_settings().get("gas_rate")
    if cap and gas and gas > cap["gas_rate"] + cap["regional_margin"]:
        return {"type": "fixed", "why": f"Your gas price ({gas}p) is above the price cap (about {cap['gas_rate']}p for "
                                        f"{cap['period']}) – a variable tariff can't charge more than the cap, so this is a fixed deal."}
    first = history[0]["date"]
    today = datetime.now().date().isoformat()
    for cap_day in CAP_CHANGE_DATES:
        if cap_day <= first:
            continue
        settle = (datetime.fromisoformat(cap_day) + timedelta(days=10)).date().isoformat()
        if today < settle:
            return {"type": "unknown", "next_check": cap_day,
                    "why": f"I'll know after the price cap changes on {cap_day}: if your prices change then, you're on a variable tariff; if not, it's fixed."}
        moved = any(h["date"] >= (datetime.fromisoformat(cap_day) - timedelta(days=3)).date().isoformat() and h["date"] <= settle
                    for h in history[1:])
        if moved:
            return {"type": "variable", "why": f"Your prices changed when the price cap changed on {cap_day}."}
        return {"type": "fixed", "why": f"Your prices stayed the same when the price cap changed on {cap_day}."}
    return {"type": "unknown", "why": "Not enough history yet."}


def tariff_settings() -> dict:
    return db.get_state("tariff", {
        "day_rate": 30.0, "night_rate": 15.0, "e7_standing": 55.0,
        "single_rate": 26.3, "single_standing": 55.0,
        "night_start": "00:30", "night_end": "07:30",
        "gas_rate": 7.97, "gas_standing": 29.7,
    })


def analysis(days: int = 30) -> dict:
    """Analyses the most recent `days` days *of real meter data*.

    Bright fills not-yet-received half-hours with zeros. A home's electricity never reads exactly zero
    (the fridge alone sees to that), so zero electricity = missing data. The window ends at the last real reading,
    so a gap in Bright's data (e.g. around a supplier switch) doesn't produce a nonsense answer.
    """
    t = tariff_settings()
    all_elec = [r for r in db.energy_since("electricity", 0) if r[1] > 0]
    latest = all_elec[-1][0] if all_elec else None
    end = (latest + 1800) if latest else int(time.time())
    since = end - days * 86400
    elec = [r for r in all_elec if r[0] >= since]
    have = {ts for ts, _ in elec}
    # Gas: only count half-hours where the electricity shows the meter data was actually received.
    gas = [r for r in db.energy_since("gas", since) if r[0] < end and r[0] in have]
    out = {"days": days, "tariff": t, "has_elec": bool(elec), "has_gas": bool(gas),
           "window_end": end, "first": all_elec[0][0] if all_elec else None}
    if elec:
        covered = (elec[-1][0] - elec[0][0]) / 86400 + 1 / 48
        night = sum(v for ts, v in elec if _in_window(ts, t["night_start"], t["night_end"]))
        total = sum(v for _, v in elec)
        day = total - night
        e7_cost = (day * t["day_rate"] + night * t["night_rate"]) / 100 + covered * t["e7_standing"] / 100
        single_cost = total * t["single_rate"] / 100 + covered * t["single_standing"] / 100
        # Break-even: the night share at which E7 and single rate cost the same (ignoring standing charge differences)
        spread = t["day_rate"] - t["night_rate"]
        breakeven = (t["day_rate"] - t["single_rate"]) / spread if spread > 0 else None
        # Profile: average kWh in each half-hour slot (local time), and baseload = typical lowest half-hour
        slots = [[] for _ in range(48)]
        for ts, v in elec:
            lt = datetime.fromtimestamp(ts).astimezone()
            slots[lt.hour * 2 + lt.minute // 30].append(v)
        profile = [round(sum(s) / len(s), 3) if s else 0 for s in slots]
        daily_mins = {}
        for ts, v in elec:
            d = datetime.fromtimestamp(ts).date()
            daily_mins[d] = min(daily_mins.get(d, 99), v)
        base = sorted(daily_mins.values())[len(daily_mins) // 2] if daily_mins else 0   # median of daily minimum
        out["electricity"] = {
            "days_covered": round(covered, 1),
            "total_kwh": round(total, 1), "night_kwh": round(night, 1), "day_kwh": round(day, 1),
            "night_share": round(night / total, 3) if total else 0,
            "breakeven_share": round(breakeven, 3) if breakeven is not None else None,
            "cost_e7": round(e7_cost, 2), "cost_single": round(single_cost, 2),
            "per_year_e7": round(e7_cost / covered * 365), "per_year_single": round(single_cost / covered * 365),
            "profile": profile,
            "baseload_w": round(base * 2 * 1000),    # kWh per half-hour -> average watts
            "baseload_cost_year": round(base * 2 * 24 * 365 * ((t["day_rate"] * 17 + t["night_rate"] * 7) / 24) / 100),
        }
    if gas:
        covered = (gas[-1][0] - gas[0][0]) / 86400 + 1 / 48
        total = sum(v for _, v in gas)
        out["gas"] = {
            "days_covered": round(covered, 1), "total_kwh": round(total, 1),
            "cost": round(total * t["gas_rate"] / 100 + covered * t["gas_standing"] / 100, 2),
        }
    # Daily totals for a bar chart
    daily = {}
    for fuel, rows in (("electricity", elec), ("gas", gas)):
        for ts, v in rows:
            d = datetime.fromtimestamp(ts).date().isoformat()
            daily.setdefault(d, {"date": d, "electricity": 0.0, "gas": 0.0})[fuel] += v
    out["daily"] = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()} for d in sorted(daily.values(), key=lambda x: x["date"])]
    out["latest"] = latest
    # Where are the holes? List missing days in the window so the dashboard can say so.
    real_days = {datetime.fromtimestamp(ts).date() for ts in have}
    first_day = datetime.fromtimestamp(max(since, all_elec[0][0] if all_elec else since)).date()
    last_day = datetime.fromtimestamp(time.time()).date()
    missing = [(first_day + timedelta(days=i)).isoformat() for i in range((last_day - first_day).days + 1)
               if first_day + timedelta(days=i) not in real_days]
    out["missing_days"] = len(missing)
    return out
