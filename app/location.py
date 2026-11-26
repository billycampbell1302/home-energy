"""Where everyone is and how soon they'll be home - from the free OwnTracks app on each iPhone.

OwnTracks (open source, no account) sends the phone's location straight to this server - nothing goes to
any other company. Set to "Significant changes" mode it uses very little battery.

  Home point     learned from where each phone is when the router says it's home (falls back to Tiptree centre)
  Heading home   getting closer over the last 20 minutes
  ETA            learned from your real journeys: every time someone arrives, the fixes from the trip
                 teach it "X km away = Y minutes from home". Until there are enough, ~30 km/h + 5 min.
  Backup         a fresh fix at home stops a router glitch (e.g. the nightly reboot) marking someone as out.
Only the last 60 days of locations are kept.
"""
import math
import statistics
import time

from . import db
from .config import LAT, LON, PEOPLE

with db._lock:
    db._conn.executescript("""
        CREATE TABLE IF NOT EXISTS locations (ts INTEGER, person TEXT, lat REAL, lon REAL, acc REAL, vel REAL);
        CREATE INDEX IF NOT EXISTS locations_ts ON locations(person, ts);
    """)
    db._conn.commit()

HOME_RADIUS_KM = 0.15


def km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def home_point() -> tuple:
    h = db.get_state("home_point")
    return (h["lat"], h["lon"]) if h else (LAT, LON)


def record(person: str, msg: dict):
    if msg.get("_type") != "location" or msg.get("lat") is None:
        return
    ts = int(msg.get("tst") or time.time())
    acc = msg.get("acc")
    with db._lock:
        db._conn.execute("INSERT INTO locations VALUES (?,?,?,?,?,?)",
                         (ts, person, float(msg["lat"]), float(msg["lon"]), acc, msg.get("vel")))
        db._conn.execute("DELETE FROM locations WHERE ts < ?", (time.time() - 60 * 86400,))
        db._conn.commit()
    # Learn the home point from fixes taken while the router says this person is home
    try:
        from .router import people_home
        if people_home().get(person) and (acc is None or acc <= 100):
            pts = db.get_state("home_fixes", [])[-49:] + [[float(msg["lat"]), float(msg["lon"])]]
            db.set_state("home_fixes", pts)
            if len(pts) >= 5:
                db.set_state("home_point", {"lat": statistics.median(p[0] for p in pts), "lon": statistics.median(p[1] for p in pts)})
    except Exception:
        pass


def _fixes(person: str, since: float):
    with db._lock:
        return db._conn.execute("SELECT ts, lat, lon, acc FROM locations WHERE person=? AND ts>=? ORDER BY ts",
                                (person, since)).fetchall()


def eta_minutes(dist_km: float) -> int:
    s = db.get_state("eta_samples", [])
    if len(s) >= 10:
        xs, ys = [a for a, _ in s], [b for _, b in s]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        den = sum((x - mx) ** 2 for x in xs)
        b1 = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 2.0
        b0 = my - b1 * mx
        if b1 > 0:
            return max(1, round(b0 + b1 * dist_km))
    return max(1, round(5 + dist_km * 2))                  # ~30 km/h plus parking/walking


def learn_arrival(person: str):
    """Called when someone arrives home: their trip's fixes become ETA examples."""
    now = time.time()
    hlat, hlon = home_point()
    samples = db.get_state("eta_samples", [])
    for ts, lat, lon, acc in _fixes(person, now - 3 * 3600):
        d = km(lat, lon, hlat, hlon)
        if d > 0.3 and (acc is None or acc < 500):
            samples.append([round(d, 2), round((now - ts) / 60, 1)])
    # keep only the run-in to home: drop points from before the person was last home (long stays out)
    samples = [s for s in samples if s[1] <= 180][-400:]
    db.set_state("eta_samples", samples)


def status(person: str) -> dict | None:
    now = time.time()
    fx = _fixes(person, now - 3 * 3600)
    if not fx:
        return None
    hlat, hlon = home_point()
    ts, lat, lon, acc = fx[-1]
    d = km(lat, lon, hlat, hlon)
    older = [f for f in fx if now - f[0] >= 15 * 60]
    heading = bool(older) and d < km(older[-1][1], older[-1][2], hlat, hlon) - 0.3
    return {"km": round(d, 1), "miles": round(d * 0.621, 1), "age_min": round((now - ts) / 60), "at_home": d <= HOME_RADIUS_KM,
            "heading_home": heading, "eta_min": eta_minutes(d) if heading and d > HOME_RADIUS_KM else None}


def fresh_at_home(person: str, after_ts: float = 0, within_min: int = 15) -> bool:
    """True only for a real Wi-Fi blip: a recent, accurate fix AT home taken at least 8 minutes AFTER the phone
    dropped off the Wi-Fi. (Someone leaving is well away by then - a fix taken while walking out doesn't count.)"""
    fx = _fixes(person, max(after_ts + 8 * 60, time.time() - within_min * 60))
    if not fx:
        return False
    hlat, hlon = home_point()
    ts, lat, lon, acc = fx[-1]
    return (acc is None or acc <= 65) and km(lat, lon, hlat, hlon) <= 0.1


def clearly_away(person: str) -> bool:
    """A fresh fix well away from home (400 m+, last 15 min) - they've left, whatever the Wi-Fi says."""
    fx = _fixes(person, time.time() - 15 * 60)
    if not fx:
        return False
    hlat, hlon = home_point()
    ts, lat, lon, acc = fx[-1]
    return (acc is None or acc <= 300) and km(lat, lon, hlat, hlon) >= 0.4


def all_status() -> dict:
    return {p: status(p) for p in PEOPLE}
