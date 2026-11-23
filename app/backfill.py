"""One-off import of past data, so learning doesn't start from zero:

- Hive's stored temperature history for your thermostat (Hive keeps history for the graphs in its app;
  how far back it goes isn't documented, so we keep stepping back until Hive has nothing more).
- Outdoor temperatures for the same period from Open-Meteo's free weather archive.

Runs by itself in the background once Hive is connected, and resumes where it left off after a restart.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from . import db
from .config import LAT, LON

log = logging.getLogger("home-energy")
CHUNK_DAYS = 7
MAX_YEARS = 5

with db._lock:
    db._conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hive_history (ts INTEGER PRIMARY KEY, indoor_temp REAL, target_temp REAL);
        CREATE TABLE IF NOT EXISTS weather_history (ts INTEGER PRIMARY KEY, outdoor_temp REAL);
        """
    )
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(weather_history)")}
    for c in ("wind REAL", "solar REAL", "humidity REAL"):
        if c.split()[0] not in cols:
            db._conn.execute(f"ALTER TABLE weather_history ADD COLUMN {c}")
    db._conn.commit()


def _parse_points(j) -> list:
    """Hive's history format isn't documented: accept a list of points under 'data' (or the top level),
    with a time ('date'/'time'/'timestamp', in ms or s) and a temperature."""
    pts = j.get("data", j) if isinstance(j, dict) else j
    out = []
    if isinstance(pts, dict):          # sometimes keyed by timestamp
        pts = [{"date": k, **(v if isinstance(v, dict) else {"temperature": v})} for k, v in pts.items()]
    for p in pts or []:
        if not isinstance(p, dict):
            continue
        t = p.get("date") or p.get("time") or p.get("timestamp")
        temp = p.get("temperature", p.get("value", p.get("temp")))
        try:
            t = int(float(t))
            t = t // 1000 if t > 10**11 else t
            out.append((t, round(float(temp), 2), p.get("target")))
        except (TypeError, ValueError):
            continue
    return out


def hive_history_chunk(hive, start: datetime, end: datetime) -> list:
    heat = hive._product("heating")
    if not heat:
        raise RuntimeError("No Hive thermostat chosen yet")
    q = f"?start={int(start.timestamp() * 1000)}&end={int(end.timestamp() * 1000)}&timeUnit=MINUTES&rate=30&operation=AVG"
    if heat.get("_home_id") and len(getattr(hive, "homes", [])) > 1:
        q += f"&homeId={heat['_home_id']}"
    return _parse_points(hive._request("GET", f"/history/heating/{heat['id']}{q}"))


def weather_history(start: datetime, end: datetime) -> int:
    """Hourly outdoor temperature from the Open-Meteo archive (free, no key)."""
    r = requests.get("https://archive-api.open-meteo.com/v1/archive", params={
        "latitude": LAT, "longitude": LON, "timezone": "UTC", "wind_speed_unit": "mph",
        "hourly": "temperature_2m,wind_speed_10m,shortwave_radiation,relative_humidity_2m",
        "start_date": start.date().isoformat(), "end_date": end.date().isoformat()}, timeout=60)
    r.raise_for_status()
    h = r.json()["hourly"]
    rows = [(int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()), v, w, sol, hum)
            for t, v, w, sol, hum in zip(h["time"], h["temperature_2m"], h["wind_speed_10m"],
                                          h["shortwave_radiation"], h["relative_humidity_2m"]) if v is not None]
    with db._lock:
        db._conn.executemany("INSERT OR REPLACE INTO weather_history (ts, outdoor_temp, wind, solar, humidity) "
                             "VALUES (?,?,?,?,?)", rows)
        db._conn.commit()
    return len(rows)


def run(hive) -> dict:
    """Step backwards a week at a time from where we got to. Stops after 4 empty weeks in a row (end of Hive's history)."""
    st = db.get_state("backfill", {"cursor": None, "empty_run": 0, "done": False, "count": 0, "oldest": None})
    if st.get("done") and st.get("weather_done") and st.get("weather_version") == 2:
        return st
    now = datetime.now(timezone.utc)
    cursor = datetime.fromtimestamp(st["cursor"], timezone.utc) if st["cursor"] else now
    limit = now - timedelta(days=365 * MAX_YEARS)
    try:
        for _ in range(0 if st.get("done") else 20):                     # up to 20 weeks per run, then let the app breathe
            start = cursor - timedelta(days=CHUNK_DAYS)
            pts = hive_history_chunk(hive, start, cursor)
            if pts:
                with db._lock:
                    db._conn.executemany(
                        "INSERT OR REPLACE INTO hive_history (ts, indoor_temp, target_temp) VALUES (?,?,?)", pts)
                    db._conn.commit()
                st["count"] += len(pts)
                st["oldest"] = min(p[0] for p in pts)
                st["empty_run"] = 0
            else:
                st["empty_run"] += 1
            cursor = start
            st["cursor"] = int(cursor.timestamp())
            if st["empty_run"] >= 4 or cursor < limit:
                st["done"] = True
                break
        st.pop("error", None)
    except Exception as e:
        st["error"] = str(e)[:200]
        log.warning("Hive history import: %s", e)
    db.set_state("backfill", st)

    # Weather for whatever span of Hive history we've got (archive lags ~5 days; recent days come from the live feed).
    if st.get("weather_version") != 2:
        st.pop("weather_done_to", None); st.pop("weather_done", None); st["weather_version"] = 2
    if st.get("oldest") and not st.get("weather_done_to") or (st.get("done") and not st.get("weather_done")):
        try:
            oldest = datetime.fromtimestamp(st["oldest"], timezone.utc)
            n = weather_history(oldest, now - timedelta(days=5))
            st["weather_rows"] = n
            st["weather_done_to"] = int(oldest.timestamp())
            st["weather_done"] = bool(st.get("done"))
        except Exception as e:
            st["weather_error"] = str(e)[:200]
        db.set_state("backfill", st)
    return st


def history_points(since: int, max_points: int = 600) -> list:
    """Imported history for the chart, thinned out evenly to keep it light on a phone."""
    with db._lock:
        rows = db._conn.execute(
            "SELECT h.ts, h.indoor_temp, h.target_temp, "
            "(SELECT outdoor_temp FROM weather_history w WHERE w.ts <= h.ts ORDER BY w.ts DESC LIMIT 1) "
            "FROM hive_history h WHERE h.ts >= ? ORDER BY h.ts", (since,)).fetchall()
    step = max(1, len(rows) // max_points)
    return [{"ts": r[0], "indoor_temp": r[1], "target_temp": r[2], "outdoor_temp": r[3], "boiler_on": None}
            for r in rows[::step]]
