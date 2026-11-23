"""SQLite storage: a reading every few minutes, plus an event log. This is the data the learning will train on."""
import json
import sqlite3
import threading
import time

from .config import DB_PATH

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS readings (
        ts INTEGER NOT NULL,          -- unix seconds
        indoor_temp REAL,
        target_temp REAL,
        heating_mode TEXT,            -- SCHEDULE / MANUAL / OFF / BOOST
        boiler_on INTEGER,            -- 1 if Hive says heat is being called for
        hot_water_on INTEGER,
        outdoor_temp REAL,
        people_home INTEGER,          -- how many adults' phones are home
        kids_lights_on INTEGER,       -- any kids' light on
        hold INTEGER,                 -- guests/kids-home hold switched on
        extra TEXT                    -- JSON for anything else
    );
    CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts);
    CREATE TABLE IF NOT EXISTS events (
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL,           -- presence / command / error / setup
        detail TEXT
    );
    CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
    CREATE TABLE IF NOT EXISTS state (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """
)
_conn.commit()


def add_reading(**fields):
    fields.setdefault("ts", int(time.time()))
    if "extra" in fields and not isinstance(fields["extra"], str):
        fields["extra"] = json.dumps(fields["extra"])
    cols = ",".join(fields)
    q = ",".join("?" for _ in fields)
    with _lock:
        _conn.execute(f"INSERT INTO readings ({cols}) VALUES ({q})", list(fields.values()))
        _conn.commit()


def add_event(kind: str, detail):
    if not isinstance(detail, str):
        detail = json.dumps(detail)
    with _lock:
        _conn.execute("INSERT INTO events (ts, kind, detail) VALUES (?,?,?)", (int(time.time()), kind, detail))
        _conn.commit()


def readings_since(seconds: int):
    since = int(time.time()) - seconds
    with _lock:
        cur = _conn.execute(
            "SELECT ts, indoor_temp, target_temp, outdoor_temp, boiler_on, people_home "
            "FROM readings WHERE ts >= ? ORDER BY ts",
            (since,),
        )
        return [dict(zip([c[0] for c in cur.description], row)) for row in cur.fetchall()]


def recent_events(limit: int = 30):
    with _lock:
        cur = _conn.execute("SELECT ts, kind, detail FROM events ORDER BY ts DESC LIMIT ?", (limit,))
        return [{"ts": r[0], "kind": r[1], "detail": r[2]} for r in cur.fetchall()]


def get_state(key: str, default=None):
    with _lock:
        row = _conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(key: str, value):
    with _lock:
        _conn.execute(
            "INSERT INTO state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        _conn.commit()
