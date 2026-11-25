"""Untypical days (bank holidays, annual leave, sick days...) are left out of the schedule learning.

  Bank holidays  from GOV.UK's official list (England & Wales), refreshed weekly. A coming bank holiday is
                 planned like a Sunday.
  Days off       found automatically: a weekday where the flat was occupied in the daytime much more (or less)
                 than on the other same weekdays - e.g. annual leave, a sick day, a day away. Needs 2+ other
                 same weekdays to compare against.
  Your call      any day can be left out or counted back in from the Plan tab (overrides the above).
"""
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

from . import db

DAY_START, DAY_END = 8, 18          # "daytime" used to compare days
DIFF = 0.4                          # 40 percentage points different from the usual = not a typical day


def bank_holidays() -> dict:
    st = db.get_state("bank_holidays", {})
    if time.time() - st.get("ts", 0) > 7 * 86400:
        try:
            j = requests.get("https://www.gov.uk/bank-holidays.json", timeout=10).json()
            st = {"ts": time.time(), "days": {e["date"]: e["title"] for e in j["england-and-wales"]["events"]}}
            db.set_state("bank_holidays", st)
        except Exception:
            st.setdefault("days", {})
    return st.get("days", {})


def _daytime_occupied(days: int = 42) -> dict:
    """date -> fraction of daytime readings with someone in (weekdays and weekends)."""
    with db._lock:
        rows = db._conn.execute("SELECT ts, people_home, kids_lights_on, hold FROM readings WHERE ts>=?",
                                (time.time() - days * 86400,)).fetchall()
    acc = defaultdict(lambda: [0, 0])
    for ts, p, k, h in rows:
        d = datetime.fromtimestamp(ts)
        if DAY_START <= d.hour < DAY_END:
            a = acc[d.date().isoformat()]
            a[0] += int(bool(p or k or h))
            a[1] += 1
    return {k: v[0] / v[1] for k, v in acc.items() if v[1] >= 60}      # at least 5 hours of readings that day


def unusual_days() -> dict:
    """date -> reason, worked out from the data."""
    occ = _daytime_occupied()
    today = date.today().isoformat()
    out = {}
    for ds, f in occ.items():
        if ds == today:
            continue
        wd = date.fromisoformat(ds).weekday()
        others = [v for k, v in occ.items() if k != ds and k != today and date.fromisoformat(k).weekday() == wd]
        if len(others) < 2:
            continue
        usual = sorted(others)[len(others) // 2]
        if f - usual >= DIFF:
            out[ds] = f"home much more than a usual {date.fromisoformat(ds):%A} ({f * 100:.0f}% vs {usual * 100:.0f}% of the daytime) – day off?"
        elif usual - f >= DIFF:
            out[ds] = f"out much more than a usual {date.fromisoformat(ds):%A} ({f * 100:.0f}% vs {usual * 100:.0f}% in) – away?"
    return out


def excluded() -> dict:
    """date (YYYY-MM-DD) -> reason, for every day left out of the learning."""
    st = db.get_state("day_overrides", {})          # {"date": "exclude" | "include"}
    out = {}
    for ds, title in bank_holidays().items():
        if ds <= date.today().isoformat():
            out[ds] = f"bank holiday – {title}"
    for ds, why in unusual_days().items():
        out.setdefault(ds, why)
    for ds, v in st.items():
        if v == "exclude":
            out[ds] = "you left it out"
        elif v == "include":
            out.pop(ds, None)
    return out


_cache = {"ts": 0, "set": set()}


def excluded_set() -> set:
    """Fast lookup for the learning code (refreshed every 10 minutes)."""
    if time.time() - _cache["ts"] > 600:
        try:
            _cache.update(ts=time.time(), set=set(excluded()))
        except Exception:
            _cache["ts"] = time.time()
    return _cache["set"]


def is_excluded(d) -> bool:
    ds = d.isoformat() if isinstance(d, date) else str(d)[:10]
    return ds in excluded_set()


def set_override(ds: str, value: str | None):
    st = db.get_state("day_overrides", {})
    if value:
        st[ds] = value
    else:
        st.pop(ds, None)
    db.set_state("day_overrides", st)
    _cache["ts"] = 0
    db.add_event("smart", {"days": f"{ds} {'left out of' if value == 'exclude' else 'counted in'} the learning"})


def recent(days: int = 21) -> list:
    """The last few weeks, for the Plan tab: each day, whether it's used for learning and why not."""
    ex = excluded()
    st = db.get_state("day_overrides", {})
    out = []
    for i in range(1, days + 1):
        d = date.today() - timedelta(days=i)
        ds = d.isoformat()
        out.append({"date": ds, "label": f"{d:%a %d %b}", "excluded": ds in ex, "why": ex.get(ds, ""), "override": st.get(ds)})
    return out


def planned_as(d: date) -> tuple:
    """For the coming week: a bank holiday is planned like a Sunday."""
    title = bank_holidays().get(d.isoformat())
    return (6, f"Bank holiday ({title}) – planned like a Sunday") if title else (d.weekday(), None)
