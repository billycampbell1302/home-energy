"""Home Energy - phase 1: connect Hive + Smart Life, log everything, phone dashboard with manual controls.

No automatic control happens in phase 1. It watches and records, so the learning in later phases has data.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Query, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, weather
from .presence import lights_signal, update_light_activity
from .bright import BrightError, analysis, auto_update_tariff, bright, contract_guess, contract_info, tariff_settings
from .config import ADMIN_PASSWORD, PEOPLE, POLL_MINUTES, PUBLIC_URL, SESSION_SECRET, webhook_token
from .hive import HiveError, hive
from .tuya import tuya
from . import smartthings

log = logging.getLogger("home-energy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
STATIC = Path(__file__).parent / "static"
COOKIE = "he_session"
WHO_COOKIE = "he_who"
COOKIE_DAYS = 365
_errors: dict[str, str] = {}


# ---------------- background logger ----------------
def presence() -> dict:
    p = db.get_state("presence", {})
    for name in PEOPLE:
        p.setdefault(name, {"home": True, "since": int(time.time())})
    return p


def all_devices() -> list:
    return tuya.devices() + (smartthings.devices() if smartthings.is_configured() else [])


def comfort_guard(home_now: list):
    """If a boost (from anywhere) is aiming hotter than the people home find comfortable, trim it to their
    comfortable temperature - same boost, no overheating. Only once their comfort is learned (3+ data points)."""
    from . import comfort
    s = hive.summary()
    if s.get("mode") != "BOOST" or not home_now or not s.get("target_temp"):
        return
    c = comfort.for_people(home_now)
    if c["confident"] and s["target_temp"] > c["temp"] + 0.5 and (s.get("boost_minutes") or 0) > 5:
        hive.boost(int(s["boost_minutes"]), c["temp"])
        db.add_event("comfort", {"trimmed_boost": f"{s['target_temp']}° → {c['temp']}°", "for": ", ".join(c["who"])})


def poll_once():
    row = {}
    if smartthings.is_configured():
        try:
            smartthings.refresh()
            _errors.pop("smartthings", None)
        except Exception as e:
            _errors["smartthings"] = str(e)[:200]
            log.warning("SmartThings poll failed: %s", e)
    if hive.is_configured():
        try:
            s = hive.refresh()
            try:
                home_now = [p for p, v in presence().items() if v["home"]]
                hive.track_changes(home_now)
                comfort_guard(home_now)
                from . import thermal
                thermal.act(hive.summary(), home_now, bool(db.get_state("hold", False)))
                try:
                    from . import plan as _plan
                    _plan.log_forecast()
                except Exception as e:
                    log.warning("forecast log: %s", e)
                try:
                    from . import checkins
                    checkins.check(hive.summary(), presence())
                except Exception as e:
                    log.warning("check-ins: %s", e)
                try:
                    thermal.kids_morning(hive.summary(), home_now, bool(db.get_state("hold", False)))
                except Exception as e:
                    log.warning("kids morning: %s", e)
            except Exception as e:
                log.warning("Hive change tracking: %s", e)
            row.update(indoor_temp=s.get("indoor_temp"), target_temp=s.get("target_temp"),
                       heating_mode=s.get("mode"), boiler_on=int(bool(s.get("boiler_on"))),
                       hot_water_on=int(bool(s.get("hot_water_on"))))
            _errors.pop("hive", None)
        except Exception as e:  # keep logging other things even if Hive is down
            _errors["hive"] = str(e)[:200]
            log.warning("Hive poll failed: %s", e)
    if tuya.is_configured():
        try:
            tuya.refresh()
            _errors.pop("tuya", None)
        except Exception as e:
            _errors["tuya"] = str(e)[:200]
            log.warning("Smart Life poll failed: %s", e)
    devs = all_devices()
    if devs:
        try:
            # Lights/TV only count as someone in if used since the last phone left, or a light's on night red at night.
            update_light_activity(devs)
            row["kids_lights_on"] = int(lights_signal(devs, presence())["someone_in"] and not any(v["home"] for v in presence().values()))
        except Exception as e:
            log.warning("presence from devices failed: %s", e)
    try:
        w = weather.current()
        row["outdoor_temp"] = w["temp"]
        row["extra"] = weather.reading_extras(w)
        _errors.pop("weather", None)
    except Exception as e:
        _errors["weather"] = str(e)[:200]
    row["people_home"] = sum(1 for v in presence().values() if v["home"])
    row["hold"] = int(bool(db.get_state("hold", False)))
    if tuya.is_configured():
        try:
            rooms = {d["name"]: d["temp"] for d in tuya.devices() if d["kind"] == "sensor" and not d["hidden"] and d.get("temp") is not None}
            if rooms:
                row.setdefault("extra", {})["rooms"] = rooms           # per-room temperatures, if sensors are added
        except Exception:
            pass
        try:
            from . import dehumid
            dehumid.check()
            row.setdefault("extra", {}).update(dehumid.reading_extras())
        except Exception as e:
            log.warning("dehumidifier: %s", e)
    from . import thermal
    try:
        from . import sleep as _sleep
        row.setdefault("extra", {}).update(_sleep.activity_now())
    except Exception:
        pass
    try:
        # The kids are never home without one of you: with both phones out, anything on is just left on
        adults_in = (row.get("people_home") or 0) > 0 or bool(db.get_state("hold", False))
        why = thermal._wake_signal_any() if adults_in else None
        row.setdefault("extra", {})["kids_active"] = bool(why)
        if why:
            row["extra"]["kids_why"] = why
    except Exception:
        pass
    if thermal.sun_skewed():
        row.setdefault("extra", {})["sun_skew"] = True
    db.add_reading(**row)
    if hive.is_configured():
        try:
            thermal.detect(hive.summary())
            if not db.get_state(f"savings_{time.strftime('%Y-%m-%d')}"):
                thermal.savings_step()
                db.set_state(f"savings_{time.strftime('%Y-%m-%d')}", True)
        except Exception as e:
            log.warning("smart heating checks: %s", e)


def bright_sync():
    if not bright.is_configured():
        return
    try:
        added = bright.sync()
        _errors.pop("bright", None)
        log.info("Bright sync: %s", added)
    except Exception as e:
        _errors["bright"] = str(e)[:200]
        log.warning("Bright sync failed: %s", e)
    try:
        changed = auto_update_tariff()
        if changed:
            log.info("Tariff updated from meter: %s", changed)
    except Exception as e:
        log.warning("Reading tariff from meter failed: %s", e)


async def bright_loop():
    while True:
        await asyncio.to_thread(bright_sync)
        await asyncio.sleep(6 * 3600)   # smart meter data only arrives about once a day


def backfill_step():
    from . import backfill
    st = db.get_state("backfill", {})
    if hive.is_configured() and hive.last_nodes and not (st.get("done") and st.get("weather_done") and st.get("weather_version") == 2):
        backfill.run(hive)


def daily_backup():
    """Keeps 7 daily copies of the database on the server, so a corrupted file never loses the learning data."""
    import sqlite3
    from .config import DATA_DIR, DB_PATH
    day = time.strftime("%Y-%m-%d")
    bdir = DATA_DIR / "backups"
    bdir.mkdir(exist_ok=True)
    target = bdir / f"home-{day}.db"
    if target.exists():
        return
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    for old in sorted(bdir.glob("home-*.db"))[:-7]:
        old.unlink()


async def poll_loop():
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception as e:
            log.exception("poll failed: %s", e)
        try:
            await asyncio.to_thread(backfill_step)   # imports Hive's past history a few months at a time
        except Exception as e:
            log.warning("history import: %s", e)
        try:
            await asyncio.to_thread(daily_backup)
        except Exception as e:
            log.warning("backup: %s", e)
        try:
            from . import alerts
            await asyncio.to_thread(alerts.check, dict(_errors))
        except Exception as e:
            log.warning("alerts: %s", e)
        await asyncio.sleep(POLL_MINUTES * 60)


async def appliance_loop():
    from . import connectlife
    while True:
        if connectlife.is_configured():
            try:
                await connectlife.refresh()
                connectlife.track_runs()
                _errors.pop("connectlife", None)
            except Exception as e:
                _errors["connectlife"] = str(e)[:200]
                log.warning("ConnectLife poll failed: %s", e)
        await asyncio.sleep(180)   # every 3 minutes - gentle on ConnectLife's servers


def _clean_night_blips():
    """One-off: remove overnight 'left' + 'came back' pairs under 45 minutes (router reboots), so they don't
    teach the routine that someone goes out at 2am."""
    if db.get_state("cleaned_night_blips"):
        return
    with db._lock:
        rows = db._conn.execute("SELECT rowid, ts, detail FROM events WHERE kind='presence' ORDER BY ts").fetchall()
    drop, last_away = [], {}
    for rowid, ts, detail in rows:
        try:
            d = json.loads(detail)
        except ValueError:
            continue
        h = time.localtime(ts).tm_hour
        if not d["home"] and (h >= 23 or h < 7):
            last_away[d["person"]] = (rowid, ts)
        elif d["home"] and d["person"] in last_away:
            rid, t0 = last_away.pop(d["person"])
            if ts - t0 < 45 * 60:
                drop += [rid, rowid]
    with db._lock:
        db._conn.executemany("DELETE FROM events WHERE rowid=?", [(r,) for r in drop])
        db._conn.commit()
    db.set_state("cleaned_night_blips", len(drop))


@asynccontextmanager
async def lifespan(app):
    try:
        _clean_night_blips()
        _clean_short_outs()
    except Exception as e:
        log.warning("cleanup: %s", e)
    tasks = [asyncio.create_task(poll_loop()), asyncio.create_task(bright_loop()), asyncio.create_task(appliance_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# ---------------- login ----------------
def _sign(ts: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()


def _authed(request: Request) -> bool:
    c = request.cookies.get(COOKIE, "")
    if "." not in c:
        return False
    ts, sig = c.split(".", 1)
    return hmac.compare_digest(sig, _sign(ts)) and ts.isdigit() and time.time() - int(ts) < COOKIE_DAYS * 86400


PUBLIC_PATHS = {"/login", "/api/login", "/manifest.json", "/sw.js", "/healthz", "/join"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/") or path.startswith("/hook/") or _authed(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not logged in"}, status_code=401)
    return RedirectResponse("/login")


class Login(BaseModel):
    password: str


@app.post("/api/login")
def login(body: Login):
    time.sleep(0.5)  # slow down password guessing
    if not hmac.compare_digest(body.password, ADMIN_PASSWORD):
        raise HTTPException(401, "Wrong password")
    ts = str(int(time.time()))
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, f"{ts}.{_sign(ts)}", max_age=COOKIE_DAYS * 86400, httponly=True,
                    secure=PUBLIC_URL.startswith("https"), samesite="lax")
    resp.delete_cookie(WHO_COOKIE)        # logging in with the password = the full app
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ---------------- pages ----------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.get("/manifest.json")
def manifest(t: str | None = None):
    m = json.loads((STATIC / "manifest.json").read_text())
    if t and t in db.get_state("invites", {}):
        m["start_url"] = f"/join?t={t}&app=1"          # the home-screen app always opens through the invite
        m["name"], m["short_name"] = "Home", "Home"
    return JSONResponse(m, media_type="application/manifest+json")


class Share(BaseModel):
    person: str


@app.post("/api/share")
def share(sh: Share):
    """A personal link that logs someone in to the simple version of the app (no password needed)."""
    import secrets
    if sh.person not in PEOPLE:
        raise HTTPException(400, "Unknown person")
    inv = db.get_state("invites", {})
    tok = next((k for k, v in inv.items() if v["person"] == sh.person), None) or secrets.token_urlsafe(18)
    inv[tok] = {"person": sh.person, "created": int(time.time())}
    db.set_state("invites", inv)
    return {"url": f"{PUBLIC_URL}/join?t={tok}"}


@app.post("/api/share/revoke")
def share_revoke(sh: Share):
    inv = {k: v for k, v in db.get_state("invites", {}).items() if v["person"] != sh.person}
    db.set_state("invites", inv)
    return {"ok": True}


@app.get("/join")
def join(t: str = "", app_: int = Query(0, alias="app")):
    inv = db.get_state("invites", {}).get(t)
    if not inv:
        return HTMLResponse("<p style='font-family:sans-serif'>This link has expired – ask Billy for a new one.</p>", status_code=403)
    person = inv["person"]
    if app_:
        body = (f"<script>try{{localStorage.setItem('me',{json.dumps(person)});localStorage.setItem('simple','1')}}catch(e){{}}"
                "location.replace('/')</script>")
    else:
        body = f"""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="manifest" href="/manifest.json?t={t}"><meta name="apple-mobile-web-app-capable" content="yes">
<link rel="apple-touch-icon" href="/static/icon-192.png"><title>Home</title>
<style>body{{font-family:-apple-system,sans-serif;background:#0f1115;color:#e8eaf0;padding:24px;line-height:1.5}}
.b{{display:block;background:#ff7a2f;color:#111;text-align:center;padding:14px;border-radius:12px;text-decoration:none;font-weight:600;margin-top:20px}}</style></head>
<body><h2>Hi {person} 👋</h2><p>This is our home heating app. To install it:</p>
<ol><li>Tap the <b>Share</b> button at the bottom of Safari (the square with an arrow)</li>
<li>Choose <b>Add to Home Screen</b>, then <b>Add</b></li>
<li>Open <b>Home</b> from your home screen and tap <b>Turn on notifications</b></li></ol>
<p>It'll occasionally ask how warm it feels – one tap, and it learns what you like.</p>
<a class="b" href="/join?t={t}&app=1">Or just open it here</a>
<script>try{{localStorage.setItem('me',{json.dumps(person)});localStorage.setItem('simple','1')}}catch(e){{}}</script></body></html>"""
    resp = HTMLResponse(body)
    ts = str(int(time.time()))
    resp.set_cookie(COOKIE, f"{ts}.{_sign(ts)}", max_age=COOKIE_DAYS * 86400, httponly=True,
                    secure=PUBLIC_URL.startswith("https"), samesite="lax")
    # Marks this browser as the shared simple version for this person (the server decides, not the phone)
    resp.set_cookie(WHO_COOKIE, f"{person}.{_sign('who:' + person)}", max_age=COOKIE_DAYS * 86400, httponly=True,
                    secure=PUBLIC_URL.startswith("https"), samesite="lax")
    return resp


def shared_person(request: Request) -> str | None:
    """The person whose shared link this browser came in through (None = full app via password)."""
    c = request.cookies.get(WHO_COOKIE, "")
    if "." not in c:
        return None
    person, sig = c.rsplit(".", 1)
    return person if person in PEOPLE and hmac.compare_digest(sig, _sign("who:" + person)) else None


@app.get("/sw.js")
def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------- dashboard data ----------------
@app.get("/api/status")
def status(request: Request = None):
    try:
        w = weather.current()
    except Exception:
        w = None
    return {
        "heating": hive.summary() if hive.last_nodes else {"connected": False, "configured": hive.is_configured()},
        "hive_configured": hive.is_configured(),
        "tuya_configured": tuya.is_configured(),
        "devices": all_devices(),
        "weather": {k: v for k, v in (w or {}).items() if k != "hourly"} if w else None,
        "presence": presence(),
        "locations": __import__("app.location", fromlist=["x"]).all_status(),
        "lights_signal": lights_signal(all_devices(), presence()),
        "hold": bool(db.get_state("hold", False)),
        "errors": _errors,
        "now": int(time.time()),
        "shared_as": shared_person(request) if request is not None else None,
    }


@app.get("/api/insights")
def insights():
    from . import insights as ins
    return ins.build(hive.is_configured(), tuya.is_configured(), dict(_errors))


@app.get("/api/learning")
def learning_health():
    """Is the learning getting good data? Each check is worked out automatically."""
    from . import router
    now = int(time.time())
    with db._lock:
        n24 = db._conn.execute("SELECT COUNT(*) FROM readings WHERE ts>=?", (now - 86400,)).fetchone()[0]
        first = db._conn.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
        temps24 = db._conn.execute("SELECT COUNT(*) FROM readings WHERE ts>=? AND indoor_temp IS NOT NULL", (now - 86400,)).fetchone()[0]
        wind24 = db._conn.execute("SELECT COUNT(*) FROM readings WHERE ts>=? AND extra LIKE '%wind%'", (now - 86400,)).fetchone()[0]
        manual = db._conn.execute("SELECT COUNT(*) FROM events WHERE kind='manual'").fetchone()[0]
        devev = db._conn.execute("SELECT COUNT(*) FROM events WHERE kind='device'").fetchone()[0]
        gas_last = db._conn.execute("SELECT MAX(ts) FROM energy WHERE fuel='gas' AND kwh>0").fetchone()[0]
    expected = min(288, max(1, (now - (first or now)) // 300))
    roles = db.get_state("router_roles", {})
    linked = {r.split(":", 1)[1] for r in roles.values() if r.startswith("person:")}
    bf = db.get_state("backfill", {})
    checks = [
        ("Recording every 5 minutes", n24 >= expected * 0.9, f"{n24} of ~{expected} readings in the last 24h"),
        ("Hive temperatures", temps24 >= expected * 0.9, f"{temps24} readings with a temperature"),
        ("Weather incl. wind & sun", wind24 >= expected * 0.8, "Met Office data with each reading"),
        ("Router reporting", router.is_active(), "phones and Fire Sticks"),
        *[(f"{p}'s phone linked", p in linked, "in ⚙️ → Router" if p not in linked else "tracked by the router") for p in PEOPLE],
        ("Smart meter gas data up to date", bool(gas_last) and now - gas_last < 4 * 86400,
         "needed to measure savings – Bright is behind" if not gas_last or now - gas_last >= 4 * 86400 else "fine"),
        ("Past Hive history imported", bool(bf.get("count")), f"{bf.get('count', 0):,} readings" if bf.get("count") else "in progress / not available"),
        ("Light & TV activity", devev > 0, f"{devev} on/off changes logged" if devev else "none logged yet"),
        ("Your manual heating changes", True, f"{manual} logged – each one teaches it what temperature you wanted"),
    ]
    days = round((now - first) / 86400, 1) if first else 0
    return {"days": days, "target_days": 14, "checks": [{"name": a, "ok": b, "detail": c} for a, b, c in checks]}


@app.get("/api/thermal")
def thermal_info():
    from . import thermal
    out = thermal.info(hive.summary() if hive.is_configured() else None)
    out["suggestions"] = thermal.suggestions() if thermal.learning_done() else []
    return out


@app.get("/api/thermal/report")
def thermal_report(days: int = 30):
    from . import thermal
    return thermal.report(max(1, min(days, 365)))


class Feature(BaseModel):
    key: str
    on: bool


@app.post("/api/thermal/feature")
def thermal_feature(f: Feature):
    from . import thermal
    if f.key not in thermal.FEATURES:
        raise HTTPException(400, "Unknown feature")
    saved = db.get_state("thermal_features", {})
    saved[f.key] = f.on
    db.set_state("thermal_features", saved)
    return thermal_info()


class Suggestion(BaseModel):
    days: str
    start: int
    end: int
    on: bool


@app.post("/api/thermal/suggestion")
def thermal_suggestion(sg: Suggestion):
    from . import thermal
    thermal.accept_suggestion(sg.days, sg.start, sg.end, sg.on)
    return thermal_info()


@app.get("/api/diagnostics")
def diagnostics():
    """Plain-text snapshot to paste to Claude. No passwords, tokens, MAC addresses or coordinates."""
    from . import router, location, thermal
    now = time.time()
    ago = lambda ts: f"{int((now - ts) / 60)} min ago" if ts else "never"
    L = [f"Version {_commit()} · {time.strftime('%Y-%m-%d %H:%M')}", ""]
    L.append(f"ROUTER: last report {ago(db.get_state('router_last_report', 0))} · settling={router.settling()}")
    devs, roles = db.get_state("router_devices", {}), db.get_state("router_roles", {})
    for mac, role in roles.items():
        if role.startswith("person:") or role in ("streamer", "kids"):
            d = devs.get(mac, {})
            L.append(f"  {role:14} {d.get('name') or '?':24} …{mac[-5:]} seen {ago(d.get('last_seen', 0))} · {round(d.get('bps', 0) * 8 / 1000)} kbps")
    L.append(f"  router people_home: {router.people_home()} · flow offloading={db.get_state('router_offload', '?')}")
    L.append("")
    L.append("PRESENCE: " + ", ".join(f"{p}={'home' if v['home'] else 'out'} since {ago(v['since'])}" for p, v in presence().items()))
    for p in PEOPLE:
        st = location.status(p)
        fx = location._fixes(p, now - 86400)
        L.append(f"  OwnTracks {p}: {len(fx)} fixes in 24h" + (f", last {st['age_min']} min ago, {st['km']} km from home, at_home={st['at_home']}" if st else ""))
    L.append(f"  home point learned: {bool(db.get_state('home_point'))}")
    L.append("")
    with db._lock:
        ev = db._conn.execute("SELECT ts, kind, detail FROM events WHERE kind IN ('presence','router','smart','error') "
                              "ORDER BY ts DESC LIMIT 25").fetchall()
    L.append("RECENT EVENTS:")
    L += [f"  {time.strftime('%d %H:%M', time.localtime(ts))} {k}: {d[:120]}" for ts, k, d in ev]
    L.append("")
    h = hive.summary() if hive.last_nodes else {}
    L.append(f"HIVE: {h.get('indoor_temp')}° target {h.get('target_temp')}° mode {h.get('mode')} boiler_on={h.get('boiler_on')}")
    m = thermal.model()
    L.append(f"THERMAL: k={m.get('k')} a={m.get('a')} runs {m.get('cool_runs')}/{m.get('heat_runs')} confident={m.get('confident')} "
             f"learning day {thermal.learning_days():.1f}")
    for d in tuya.devices():
        if d["kind"] == "dehumidifier":
            L.append(f"DEHUMIDIFIER: on={d['on']} {d.get('humidity')}% set {d.get('set_humidity')}% faults={d.get('faults')} "
                     f"state={ {k: v for k, v in (db.get_state('dehum', {}).get(d['id']) or {}).items() if k != 'hist'} }")
    try:
        from . import connectlife
        for a in connectlife.appliances():
            L.append(f"WASHER {a['name']}: offline={a.get('offline')} {connectlife.wash_state(a)}")
            keys = ("machine_status", "Current_program_phase", "Selected_program_remaining_time_in_minutes", "Door_status", "dry_time")
            with db._lock:
                rows = db._conn.execute("SELECT ts, status FROM appliance_log WHERE puid=? ORDER BY ts DESC LIMIT 25",
                                        (a["puid"],)).fetchall()
            for ts, st in reversed(rows):
                st = json.loads(st)
                L.append(f"  {time.strftime('%d %H:%M', time.localtime(ts))} " + " ".join(f"{k.split('_')[0][:6]}={st.get(k)}" for k in keys))
    except Exception as e:
        L.append(f"WASHER: {e}")
    L.append(f"ERRORS: {_errors or 'none'}")
    return PlainTextResponse("\n".join(L))


@app.get("/api/plan")
def plan_week():
    from . import plan
    return plan.week()


class DayOverride(BaseModel):
    date: str
    value: str | None = None      # "exclude", "include" or None (automatic)


@app.post("/api/days")
def day_override(o: DayOverride):
    from . import daytypes, plan
    if o.value not in (None, "exclude", "include"):
        raise HTTPException(400, "Bad value")
    daytypes.set_override(o.date[:10], o.value)
    return plan.week()


@app.get("/api/costs")
def costs_summary():
    from . import costs
    try:
        return costs.summary()
    except Exception as e:
        log.warning("costs: %s", e)
        return {"ready": False}


class PresenceFix(BaseModel):
    person: str
    home: bool            # what was really true from `at`: False = they'd left, True = they'd arrived
    at: str               # "HH:MM" today, or "YYYY-MM-DD HH:MM"


@app.post("/api/presence/correct")
def presence_correct(f: PresenceFix):
    """Correct a late or missed arrival/departure: the history and readings are rewritten from that time."""
    from datetime import datetime as dt
    if f.person not in PEOPLE:
        raise HTTPException(400, "Unknown person")
    try:
        when = dt.strptime(f.at, "%Y-%m-%d %H:%M") if len(f.at) > 5 else dt.combine(dt.now().date(), dt.strptime(f.at, "%H:%M").time())
    except ValueError:
        raise HTTPException(400, "Time should look like 08:20")
    ts = int(when.timestamp())
    now = time.time()
    if ts > now or now - ts > 7 * 86400:
        raise HTTPException(400, "Pick a time in the last 7 days")
    with db._lock:
        rows = db._conn.execute("SELECT rowid, ts, detail FROM events WHERE kind='presence' AND ts>? AND ts<=? ORDER BY ts",
                                (ts, ts + 12 * 3600)).fetchall()
    later = None
    for rowid, t, d in rows:
        d = json.loads(d)
        if d.get("person") != f.person:
            continue
        if d.get("home") == f.home:
            later = (rowid, t)          # the app noticed late - move it back
        break                           # the next event for this person decides it
    end = later[1] if later else int(now)
    with db._lock:
        if later:
            db._conn.execute("UPDATE events SET ts=? WHERE rowid=?", (ts, later[0]))
        else:
            db._conn.execute("INSERT INTO events VALUES (?,?,?)", (ts, "presence", json.dumps({"person": f.person, "home": f.home})))
        # The 5-minute readings: one person more or fewer at home over the corrected stretch
        delta = 1 if f.home else -1
        db._conn.execute("UPDATE readings SET people_home = MAX(0, MIN(?, COALESCE(people_home,0) + ?)) WHERE ts>=? AND ts<?",
                         (len(PEOPLE), delta, ts, end))
        db._conn.commit()
    if not later:
        p = presence()
        p[f.person] = {"home": f.home, "since": ts}
        db.set_state("presence", p)
    db.add_event("smart", {"correction": f"{f.person} {'arrived' if f.home else 'left'} at {when:%H:%M} on {when:%a %d %b} (corrected by you)"})
    return {"ok": True, "moved": bool(later), "minutes": round((end - ts) / 60)}


@app.get("/api/simple")
async def simple_dashboard():
    """Everything the simple (shared) dashboard shows, in plain words."""
    from datetime import datetime as dt, timedelta
    from . import thermal, comfort, connectlife, dehumid
    out = {}
    now = dt.now()
    if hive.is_configured():
        h = hive.summary()
        out["heating"] = {"indoor": h.get("indoor_temp"), "target": h.get("target_temp"), "mode": h.get("mode"),
                          "on": h.get("boiler_on"), "boost_minutes": h.get("boost_minutes")}
        today = [(st, t) for st, t in thermal._schedule_slots(now) if st.date() == now.date()]
        sched, low = [], 16.0
        for i, (st, t) in enumerate(today):
            end = today[i + 1][0] if i + 1 < len(today) else None
            if t > low:
                sched.append({"from": f"{st:%H:%M}", "to": f"{end:%H:%M}" if end else "midnight", "temp": t,
                              "now": st <= now and (end is None or now < end)})
        out["schedule"] = sched
        slots = thermal._schedule_slots(now)
        cur_t = next((t for st, t in reversed(slots) if st <= now), None)
        nxt = next(((st, t) for st, t in slots if st > now and t != cur_t), None)
        if nxt:
            out["next"] = f"{'Heating on' if nxt[1] > low else 'Heating off'} at {nxt[0]:%H:%M}" + (f" ({nxt[1]}°)" if nxt[1] > low else "") \
                + (" tomorrow" if nxt[0].date() != now.date() else "")
    try:
        tomorrow = now + timedelta(days=1)
        wt = thermal.wake_time(now if now.hour < 9 else tomorrow)
        out["kids_up"] = (f"{int(wt):02d}:{int(round(wt % 1 * 60)) % 60:02d}") if wt is not None else None
        out["learning"] = {"day": int(thermal.learning_days()) + 1, "of": thermal.LEARNING_DAYS, "done": thermal.learning_done()}
        out["lights_on"] = [d["name"] + (" (night red)" if d.get("red") else "") for d in tuya.devices()
                            if d["kind"] == "light" and d["on"] and not d["hidden"]]
    except Exception:
        pass
    home = [p for p, v in presence().items() if v["home"]]
    out["who"] = {p: v["home"] for p, v in presence().items()}
    c = comfort.for_people(home or list(PEOPLE))
    out["comfort"] = {"temp": c["temp"], "who": c["who"]}
    try:
        w = weather.current()
        out["weather"] = {"temp": w.get("temp"), "feels": w.get("feels_like"), "low": w.get("overnight_low"),
                          "rain": w.get("rain"), "wind": w.get("wind")}
    except Exception:
        pass
    # Washing machine (shown only - it's controlled on the machine)
    try:
        if connectlife.is_configured():
            for a in connectlife.appliances():
                ws = connectlife.wash_state(a)
                txt = "Off" if a.get("offline") else "Not running"
                if ws["running"]:
                    txt = ("Drying" if ws["drying"] else "Washing") + (f" · {ws['remaining_min']} min left" if ws["remaining_min"] else "")
                    if ws["finish_ts"]:
                        txt += f" · done about {dt.fromtimestamp(ws['finish_ts']):%H:%M}"
                out.setdefault("appliances", []).append({"name": a["name"], "text": txt, "running": ws["running"]})
    except Exception:
        pass
    # Dehumidifier
    try:
        st = db.get_state("dehum", {})
        for d in tuya.devices():
            if d["kind"] != "dehumidifier" or d["hidden"]:
                continue
            s_ = st.get(d["id"], {})
            if d.get("tank_full"):
                txt = "Tank full – please empty it"
            elif s_.get("deferred") and s_.get("start_at"):
                L = dehumid.learned()
                txt = f"Waiting for cheap electricity – starts at {dt.fromtimestamp(s_['start_at']):%H:%M}, clothes dry by about {L['ready_by_text']}"
            elif s_.get("load") and d["on"]:
                txt = "Drying the washing"
            elif d["on"]:
                txt = f"On – keeping the flat at {int(d.get('set_humidity') or 50)}% humidity"
            else:
                txt = "Off"
            out.setdefault("dehumidifiers", []).append({"name": d["name"], "text": txt, "humidity": d.get("humidity"),
                                                        "alert": bool(d.get("tank_full"))})
    except Exception:
        pass
    return out


@app.get("/api/week/check")
def week_check():
    """The last 7 days of comings and goings, per person, to confirm (or fix) once a week."""
    from datetime import datetime as dt, timedelta
    now = dt.now()
    start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    with db._lock:
        rows = db._conn.execute("SELECT ts, detail FROM events WHERE kind='presence' AND ts>=? ORDER BY ts",
                                (start.timestamp(),)).fetchall()
    days = []
    for i in range(7):
        d0 = start + timedelta(days=i)
        day = {"date": d0.strftime("%Y-%m-%d"), "label": d0.strftime("%a %d"), "people": {p: [] for p in PEOPLE}}
        for ts, d in rows:
            d = json.loads(d)
            t = dt.fromtimestamp(ts)
            if t.date() == d0.date() and d.get("person") in PEOPLE:
                day["people"][d["person"]].append(f"{'in' if d['home'] else 'out'} {t:%H:%M}")
        days.append(day)
    week = now.strftime("%G-W%V")
    return {"days": days, "week": week, "confirmed": week in db.get_state("weeks_confirmed", [])}


@app.post("/api/week/confirm")
def week_confirm():
    from datetime import datetime as dt
    wk = dt.now().strftime("%G-W%V")
    db.set_state("weeks_confirmed", (db.get_state("weeks_confirmed", []) + [wk])[-60:])
    db.add_event("smart", {"week_check": f"week {wk} comings and goings confirmed"})
    return week_check()


@app.get("/api/tariffs/compare")
def tariffs_compare():
    return {"scan": db.get_state("tariff_scan"), "state": db.get_state("tariff_scan_state", {"state": "none"}),
            "peaksave": bool(db.get_state("peaksave"))}


@app.post("/api/tariffs/compare")
def tariffs_compare_start():
    from . import tariffs
    st = db.get_state("tariff_scan_state", {})
    if st.get("state") == "running" and time.time() - st.get("ts", 0) < 600:
        return tariffs_compare()
    def go():
        try:
            tariffs.scan()
        except Exception as e:
            log.warning("tariff scan: %s", e)
    threading.Thread(target=go, daemon=True).start()
    db.set_state("tariff_scan_state", {"state": "running", "ts": int(time.time())})
    return tariffs_compare()


class Flag(BaseModel):
    on: bool


@app.post("/api/peaksave")
def peaksave(f: Flag):
    db.set_state("peaksave", f.on)
    return {"peaksave": f.on}


@app.get("/api/routine")
def routine():
    from . import routine as r
    return r.build()


@app.get("/api/forecast")
def forecast():
    try:
        return weather.current()
    except Exception as e:
        raise HTTPException(502, f"Weather unavailable: {e}")


@app.get("/api/history")
def history(hours: int = 24):
    """Our own 5-minute readings, plus Hive's imported history for anything older than when we started."""
    from . import backfill
    hours = max(1, min(hours, 24 * 365 * 6))
    rows = db.readings_since(hours * 3600)
    since = int(time.time()) - hours * 3600
    first_own = rows[0]["ts"] if rows else int(time.time())
    older = backfill.history_points(since, 600) if first_own - since > 3600 else []
    older = [r for r in older if r["ts"] < first_own]
    step = max(1, len(rows) // 600)
    return older + rows[::step]


@app.get("/api/history/import")
def history_import_status():
    st = db.get_state("backfill", {})
    return {k: st.get(k) for k in ("done", "count", "oldest", "error", "weather_rows", "weather_error")}


@app.get("/api/events")
def events():
    return db.recent_events(40)


@app.post("/api/refresh")
async def refresh():
    await asyncio.to_thread(poll_once)
    return status()


# ---------------- controls ----------------
class Boost(BaseModel):
    minutes: int = 30
    temp: float | None = None
    person: str | None = None


class Target(BaseModel):
    temp: float


class Mode(BaseModel):
    mode: str


def _hive_call(fn, *args):
    try:
        if not hive.last_nodes:
            hive.refresh()
        fn(*args)
        hive.refresh()
        return hive.summary()
    except HiveError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Hive didn't respond: {e}")


@app.post("/api/heating/boost")
def heating_boost(b: Boost):
    from . import comfort
    home_now = [p for p, v in presence().items() if v["home"]]
    if b.person in PEOPLE and b.person not in home_now:
        home_now.append(b.person)
    temp = b.temp
    if temp is None:
        c = comfort.for_people(home_now)
        temp = max(c["temp"], (hive.summary().get("indoor_temp") or 0) + 0.5)   # always at least a little warmer
        temp = min(temp, c["temp"] + 1)
    if b.person in PEOPLE:
        comfort.record([b.person], "cold", hive.summary().get("indoor_temp"), "dashboard")
    return _hive_call(hive.boost, max(15, min(b.minutes, 360)), max(5, min(temp, 25)))


class Feeling(BaseModel):
    person: str
    feeling: str


@app.post("/api/comfort")
def comfort_feedback(f: Feeling):
    from . import comfort
    if f.person not in PEOPLE:
        raise HTTPException(400, "Unknown person")
    indoor = hive.summary().get("indoor_temp")
    try:
        outdoor = weather.current()["temp"]
    except Exception:
        outdoor = None
    comfort.record([f.person], f.feeling, indoor, "dashboard", outdoor)
    return comfort_info()


@app.get("/api/comfort")
def comfort_info():
    from . import comfort
    home_now = [p for p, v in presence().items() if v["home"]]
    return {"now": comfort.for_people(home_now), "people": PEOPLE}


@app.post("/api/heating/boost_off")
def heating_boost_off():
    return _hive_call(hive.boost_off)


@app.post("/api/heating/target")
def heating_target(t: Target):
    return _hive_call(hive.set_target, max(5, min(t.temp, 25)))


@app.post("/api/heating/mode")
def heating_mode(m: Mode):
    return _hive_call(hive.set_mode, m.mode.upper())


@app.post("/api/hotwater/boost")
def hotwater_boost(b: Boost):
    return _hive_call(hive.hot_water_boost, max(15, min(b.minutes, 120)))


class DeviceAction(BaseModel):
    action: str


@app.post("/api/devices/{device_id}")
def device_action(device_id: str, a: DeviceAction):
    try:
        if device_id.startswith("st:"):
            smartthings.command(device_id, a.action)
            smartthings.refresh()
        else:
            tuya.command(device_id, a.action)
    except Exception as e:
        raise HTTPException(400, f"{'SmartThings' if device_id.startswith('st:') else 'Smart Life'}: {e}")
    return all_devices()


class KidsFlag(BaseModel):
    kids: bool


@app.post("/api/devices/{device_id}/kids")
def mark_kids(device_id: str, k: KidsFlag):
    ids = set(db.get_state("kids_lights", []))
    (ids.add if k.kids else ids.discard)(device_id)
    db.set_state("kids_lights", sorted(ids))
    return tuya.devices()


class HiddenFlag(BaseModel):
    hidden: bool


@app.post("/api/devices/{device_id}/hidden")
def hide_device(device_id: str, h: HiddenFlag):
    ids = set(db.get_state("hidden_devices", []))
    (ids.add if h.hidden else ids.discard)(device_id)
    db.set_state("hidden_devices", sorted(ids))
    return all_devices()


class HomeHidden(BaseModel):
    home: str
    hidden: bool


@app.post("/api/homes/hidden")
def hide_home(h: HomeHidden):
    """Hide or show every device in one Smart Life home (e.g. devices set up at someone else's house)."""
    ids = set(db.get_state("hidden_devices", []))
    for d in all_devices():
        if d["home"] == h.home:
            (ids.add if h.hidden else ids.discard)(d["id"])
    db.set_state("hidden_devices", sorted(ids))
    return all_devices()


class Hold(BaseModel):
    on: bool


@app.post("/api/hold")
def set_hold(h: Hold):
    db.set_state("hold", h.on)
    db.add_event("command", {"hold": h.on})
    return {"hold": h.on}


# ---------------- presence ----------------
SHORT_OUT = 5 * 60      # out for less than this doesn't count (bins, car, a phone blip)


def _undo_short_out(person: str) -> bool:
    """If this person's last 'left' was under 5 minutes ago, remove it: they never really went out.
    The 5-minute readings for that stretch are put back to them being home."""
    with db._lock:
        rows = db._conn.execute("SELECT rowid, ts, detail FROM events WHERE kind='presence' AND ts>=? ORDER BY ts DESC",
                                (time.time() - SHORT_OUT,)).fetchall()
    left = next(((rid, ts) for rid, ts, d in rows if json.loads(d).get("person") == person), None)
    if not left or json.loads(next(d for rid, ts, d in rows if rid == left[0])).get("home") is not False:
        return False
    with db._lock:
        db._conn.execute("DELETE FROM events WHERE rowid=?", (left[0],))
        db._conn.execute("UPDATE readings SET people_home = MIN(?, COALESCE(people_home,0) + 1) WHERE ts>=?", (len(PEOPLE), left[1]))
        prev = db._conn.execute("SELECT ts, detail FROM events WHERE kind='presence' AND ts<? ORDER BY ts DESC LIMIT 20", (left[1],)).fetchall()
        db._conn.commit()
    since = next((ts for ts, d in prev if json.loads(d).get("person") == person and json.loads(d).get("home")), left[1])
    p = presence()
    p[person] = {"home": True, "since": since}
    db.set_state("presence", p)
    return True


def _clean_short_outs():
    """One-off: remove every 'left' + 'back' pair under 5 minutes from the history, and fix the readings."""
    if db.get_state("cleaned_short_outs"):
        return
    with db._lock:
        rows = db._conn.execute("SELECT rowid, ts, detail FROM events WHERE kind='presence' ORDER BY ts").fetchall()
    drop, fix, last_out = [], [], {}
    for rowid, ts, detail in rows:
        try:
            d = json.loads(detail)
        except ValueError:
            continue
        if not d.get("home"):
            last_out[d["person"]] = (rowid, ts)
        elif d["person"] in last_out:
            rid, t0 = last_out.pop(d["person"])
            if ts - t0 < SHORT_OUT:
                drop += [rid, rowid]
                fix.append((t0, ts))
        else:
            last_out.pop(d.get("person"), None)
    with db._lock:
        db._conn.executemany("DELETE FROM events WHERE rowid=?", [(r,) for r in drop])
        for a, b in fix:
            db._conn.execute("UPDATE readings SET people_home = MIN(?, COALESCE(people_home,0) + 1) WHERE ts>=? AND ts<?",
                             (len(PEOPLE), a, b))
        db._conn.commit()
    db.set_state("cleaned_short_outs", len(fix))
    if fix:
        db.add_event("smart", {"presence": f"removed {len(fix)} trips out of under 5 minutes from the history"})


def _set_presence(person: str, state: str, force: bool = False):
    match = next((p for p in PEOPLE if p.lower() == person.lower()), None)
    if not match:
        raise HTTPException(404, f"Unknown person. Use one of: {', '.join(PEOPLE)}")
    home = state.lower() in ("home", "arrive", "arrived", "in", "1", "true")
    p = presence()
    if home and not p[match]["home"] and not force and _undo_short_out(match):
        return presence()                     # back within 5 minutes: it never counted as going out
    if p[match]["home"] != home or force:
        changed = p[match]["home"] != home
        if changed or match not in db.get_state("presence", {}):
            p[match] = {"home": home, "since": int(time.time())}
        db.set_state("presence", p)
        db.add_event("presence", {"person": match, "home": home})
        if home and changed:
            try:
                from . import location
                location.learn_arrival(match)
            except Exception:
                pass
        if not changed:
            return p
        try:
            from . import notify
            notify.send("arrivals", f"{match} {'arrived home' if home else 'left'}", time.strftime("%H:%M"))
        except Exception:
            pass
        if home and db.get_state("hold", False):
            db.set_state("hold", False)   # guests switch is only for when nobody's phone is home
    return p


@app.api_route("/hook/presence", methods=["GET", "POST"])
def presence_hook(token: str, person: str, state: str):
    """Called by the iPhone Shortcuts automations when someone arrives or leaves."""
    if not hmac.compare_digest(token, webhook_token()):
        raise HTTPException(403, "Bad token")
    _set_presence(person, state)
    return {"ok": True}


@app.api_route("/hook/sleep", methods=["GET", "POST"])
def sleep_hook(token: str, person: str, state: str):
    """iPhone Shortcuts automation: Sleep Focus on (state=on) / off (state=off)."""
    from . import sleep
    if not hmac.compare_digest(token, webhook_token()):
        raise HTTPException(403, "Bad token")
    match = next((p for p in PEOPLE if p.lower() == person.lower()), None)
    if not match:
        raise HTTPException(404, "Unknown person")
    sleep.record(match, state.lower() in ("on", "asleep", "1", "true"))
    return {"ok": True}


@app.post("/hook/owntracks")
async def owntracks_hook(request: Request, token: str, person: str):
    """The OwnTracks app on each iPhone posts its location here (HTTP mode)."""
    from . import location
    if not hmac.compare_digest(token, webhook_token()):
        raise HTTPException(403, "Bad token")
    match = next((p for p in PEOPLE if p.lower() == person.lower()), None)
    if not match:
        raise HTTPException(404, "Unknown person")
    try:
        msg = json.loads((await request.body()) or b"{}")
    except ValueError:
        raise HTTPException(400, "Bad JSON")
    location.record(match, msg)
    # A fix well away from home ends "home" straight away (the Wi-Fi can take a while to notice)
    if presence().get(match, {}).get("home") and location.clearly_away(match):
        _set_presence(match, "away")
        db.add_event("smart", {"presence": f"{match} marked out – phone location is away from home"})
    return JSONResponse([])


@app.get("/hook/router/script")
def router_script(token: str):
    """The router's cron job downloads and runs this every minute, so updates need no router changes."""
    from . import router
    if not hmac.compare_digest(token, webhook_token()):
        raise HTTPException(403, "Bad token")
    return PlainTextResponse(router.script(PUBLIC_URL, token))


@app.post("/hook/router")
async def router_hook(request: Request, token: str):
    """The router reports Wi-Fi devices and their traffic every minute."""
    from . import router
    if not hmac.compare_digest(token, webhook_token()):
        raise HTTPException(403, "Bad token")
    try:
        body = json.loads((await request.body()) or b"{}")
    except ValueError:
        raise HTTPException(400, "Bad JSON")
    db.set_state("router_offload", str(body.get("offload", "")))
    up = body.get("uptime")
    router.report(body.get("clients", []), int(up) if isinstance(up, (int, float)) else None)
    # Phones the router knows about decide that person's presence (the Wi-Fi shortcuts become a backup).
    from . import location
    current = presence()
    logged = db.get_state("presence_logged", [])
    for person, home in router.people_home().items():
        if home is None:
            continue
        if not home and location.fresh_at_home(person, router.last_seen(person)):
            continue                       # OwnTracks says they're at home - a Wi-Fi blip, not a departure
        if home and not current.get(person, {}).get("home") and location.clearly_away(person):
            continue                       # location says they've gone - the router is still listing a phone that just left
        if person not in logged:
            # First time this phone is tracked: start their routine from now (otherwise nothing shows until they move)
            _set_presence(person, "home" if home else "away", force=True)
            logged.append(person)
            db.set_state("presence_logged", logged)
        elif current.get(person, {}).get("home") != home:
            _set_presence(person, "home" if home else "away")
    return {"ok": True}


@app.get("/api/devices/overview")
def devices_overview():
    """Everything the app knows about, grouped for the Devices tab."""
    from . import connectlife, router
    rdev = router.device_list() if db.get_state("router_last_report") else []
    pres = presence()
    phones = []
    for d in rdev:
        if d["role"].startswith("person:"):
            person = d["role"].split(":", 1)[1]
            phones.append({**d, "person": person, "home": pres.get(person, {}).get("home"),
                           "since": pres.get(person, {}).get("since")})
    return {
        "router_active": router.is_active(),
        "phones": phones,
        "people_without_phone": [p for p in PEOPLE if not any(ph["person"] == p for ph in phones)],
        "streamers": [d for d in rdev if d["role"] == "streamer"],
        "kids": [d for d in rdev if d["role"] == "kids"],
        "other_wifi": [d for d in rdev if d["role"] in ("", "ignore")],
        "heating": hive.summary() if hive.last_nodes else None,
        "appliances": connectlife.appliances() if connectlife.is_configured() else [],
    }


@app.get("/api/router")
def router_status():
    from . import router
    tok = webhook_token()
    return {
        "active": router.is_active(),
        "last_report": db.get_state("router_last_report"),
        "cron": f"* * * * * uclient-fetch -q -O- '{PUBLIC_URL}/hook/router/script?token={tok}' | sh",
        "devices": router.device_list(),
        "people": PEOPLE,
        "streaming": router.streaming(),
    }


class RouterRole(BaseModel):
    mac: str
    role: str


@app.post("/api/router/role")
def router_role(r: RouterRole):
    from . import router
    if r.role not in ("", "streamer", "kids", "ignore") and not (r.role.startswith("person:") and r.role[7:] in PEOPLE):
        raise HTTPException(400, "Unknown role")
    router.set_role(r.mac, r.role)
    return router_status()


class PresenceSet(BaseModel):
    person: str
    home: bool


@app.post("/api/presence")
def presence_manual(p: PresenceSet):
    return _set_presence(p.person, "home" if p.home else "away")


# ---------------- smart meter / tariff ----------------
class BrightLogin(BaseModel):
    username: str
    password: str


@app.post("/api/setup/bright")
async def setup_bright(b: BrightLogin):
    try:
        await asyncio.to_thread(bright.login, b.username.strip(), b.password)
    except BrightError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Couldn't reach Bright: {e}")
    asyncio.create_task(asyncio.to_thread(bright_sync))   # first download can take a minute - runs in the background
    return {"ok": True}


@app.post("/api/energy/sync")
async def energy_sync():
    await asyncio.to_thread(bright_sync)
    return {"ok": True, "error": _errors.get("bright")}


@app.get("/api/energy")
def energy(days: int = 30):
    a = analysis(max(1, min(days, 400)))
    a["bright_configured"] = bright.is_configured()
    a["contract"] = contract_info()
    a["tariff_history"] = db.get_state("tariff_history", [])[-6:]
    a["tariff_checked"] = db.get_state("tariff_checked")
    a["last_sync"] = bright.last_sync
    a["error"] = _errors.get("bright")
    return a


@app.get("/api/tariff/from-meter")
async def tariff_from_meter():
    if not bright.is_configured():
        raise HTTPException(400, "Connect Bright first (Settings → Smart meter).")
    try:
        return await asyncio.to_thread(bright.meter_tariff)
    except Exception as e:
        raise HTTPException(502, f"Couldn't read the tariff from Bright: {e}")


class ContractConfirm(BaseModel):
    what: str                 # prices | type | end
    value: str = ""           # for type: fixed/variable; for end: YYYY-MM-DD or "" to confirm the estimate


@app.post("/api/contract/confirm")
def contract_confirm(c: ContractConfirm):
    conf = db.get_state("contract_confirm", {})
    today = time.strftime("%Y-%m-%d")
    if c.what == "prices":
        conf["prices"] = today
    elif c.what == "type" and c.value in ("fixed", "variable"):
        conf["type"] = c.value
    elif c.what == "end":
        if c.value:
            try:
                time.strptime(c.value, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "Date must be YYYY-MM-DD")
            conf["end_date"] = c.value
        conf["end"] = today
    elif c.what == "reset":
        conf = {}
    else:
        raise HTTPException(400, "Unknown confirmation")
    db.set_state("contract_confirm", conf)
    return contract_info()


class Tariff(BaseModel):
    day_rate: float
    night_rate: float
    e7_standing: float
    single_rate: float
    single_standing: float
    night_start: str
    night_end: str
    gas_rate: float
    gas_standing: float


@app.post("/api/tariff")
def save_tariff(t: Tariff):
    for v in (t.night_start, t.night_end):
        h, _, m = v.partition(":")
        if not (h.isdigit() and m.isdigit() and int(h) < 24 and int(m) < 60):
            raise HTTPException(400, "Times must look like 00:30")
    db.set_state("tariff", t.model_dump())
    return tariff_settings()


# ---------------- ConnectLife appliances ----------------
class ConnectLifeLogin(BaseModel):
    username: str
    password: str


@app.post("/api/setup/connectlife")
async def setup_connectlife(b: ConnectLifeLogin):
    from . import connectlife
    try:
        return {"appliances": await connectlife.login(b.username.strip(), b.password)}
    except Exception as e:
        raise HTTPException(400, f"ConnectLife login failed: {str(e)[:200]}")


@app.get("/api/appliances")
async def appliances(refresh: bool = False):
    from . import connectlife
    if not connectlife.is_configured():
        return {"configured": False, "appliances": []}
    if refresh:
        try:
            await connectlife.refresh()
        except Exception as e:
            raise HTTPException(502, f"ConnectLife: {str(e)[:200]}")
    items = connectlife.appliances()
    for a in items:
        a["changing"] = connectlife.changed_fields(a["puid"])
        a["wash"] = connectlife.wash_state(a)
    return {"configured": True, "appliances": items, "error": _errors.get("connectlife"), "summary": connectlife.wash_summary()}


# ---------------- SmartThings (Samsung TV) ----------------
class PAT(BaseModel):
    token: str


@app.post("/api/setup/smartthings")
def setup_smartthings(t: PAT):
    try:
        return {"url": smartthings.register_app(t.token)}
    except smartthings.STError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Couldn't reach SmartThings: {e}")


@app.get("/api/smartthings/callback")
def smartthings_callback(code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse("/?smartthings=cancelled")
    try:
        smartthings.finish_oauth(code, state)
        smartthings.refresh()
    except Exception as e:
        log.warning("SmartThings callback: %s", e)
        return RedirectResponse("/?smartthings=failed")
    return RedirectResponse("/?smartthings=ok")


# ---------------- notifications & report ----------------
@app.get("/api/push")
def push_info():
    from . import notify
    return {"public_key": notify.public_key(), "settings": notify.settings(), "types": notify.TYPES,
            "devices": len(db.get_state("push_subs", [])), "last": db.get_state("push_last")}


@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    from . import notify
    body = json.loads(await request.body())
    person = body.pop("person", None)
    simple = body.pop("simple", None)
    notify.subscribe(body, person if person in PEOPLE else None)
    if simple is not None:
        subs = db.get_state("push_subs", [])
        for x in subs:
            if x.get("endpoint") == body.get("endpoint"):
                x["simple"] = bool(simple)
        db.set_state("push_subs", subs)
    return {"ok": True}


class NotifySetting(BaseModel):
    kind: str
    on: bool


@app.post("/api/push/settings")
def push_settings(n: NotifySetting):
    from . import notify
    st = db.get_state("notify_settings", {})
    if n.kind in notify.TYPES:
        st[n.kind] = n.on
        db.set_state("notify_settings", st)
    return notify.settings()


@app.post("/api/push/test")
def push_test():
    from . import notify
    results = notify.send("test", "Home Energy", "Notifications are working 👍", force=True)
    return {"phones": len(results), "results": results}


@app.post("/api/push/reset")
def push_reset():
    """Forget all phone subscriptions (then turn notifications on again on each phone)."""
    db.set_state("push_subs", [])
    return {"ok": True}


@app.get("/api/report")
def monthly_report():
    from . import report
    return report.current_and_previous()


# ---------------- app updates ----------------
APP_DIR = Path(__file__).resolve().parent.parent


def _commit() -> str:
    """Current code version, read straight from git's files (no git command needed)."""
    try:
        git = APP_DIR / ".git"
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head[:7]
        ref = head.split(" ", 1)[1]
        if (git / ref).exists():
            return (git / ref).read_text().strip()[:7]
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(ref):
                return line[:7]
    except Exception:
        pass
    return "unknown"


@app.get("/api/version")
def version():
    from .config import DATA_DIR
    status = {}
    try:
        status = json.loads((DATA_DIR / "update-status.json").read_text())
    except Exception:
        pass
    return {"commit": _commit(), "status": status}


@app.post("/api/update")
def request_update():
    """Asks the server to pull the latest code from GitHub now (a root service watches this file)."""
    from .config import DATA_DIR
    (DATA_DIR / "update-request").write_text(str(time.time()))
    db.add_event("setup", "Update check requested")
    return {"ok": True, "commit": _commit()}


# ---------------- setup ----------------
@app.get("/api/setup")
def setup_info():
    tok = webhook_token()
    return {
        "hive_configured": hive.is_configured(),
        "tuya_configured": tuya.is_configured(),
        "bright_configured": bright.is_configured(),
        "smartthings_configured": smartthings.is_configured(),
        "connectlife_configured": __import__("app.connectlife", fromlist=["x"]).is_configured(),
        "people": PEOPLE,
        "owntracks": {p: f"{PUBLIC_URL}/hook/owntracks?token={tok}&person={p}" for p in PEOPLE},
        "webhooks": {
            p: {
                "arrive": f"{PUBLIC_URL}/hook/presence?token={tok}&person={p}&state=home",
                "leave": f"{PUBLIC_URL}/hook/presence?token={tok}&person={p}&state=away",
            } for p in PEOPLE
        },
    }


class OctopusSetup(BaseModel):
    api_key: str
    account: str


@app.post("/api/setup/octopus")
async def octopus_setup(o: OctopusSetup):
    from . import octopus
    try:
        await asyncio.to_thread(octopus.meters, o.api_key.strip(), o.account)
    except Exception as e:
        raise HTTPException(400, str(e))
    async def go():
        try:
            await asyncio.to_thread(octopus.run, o.api_key.strip(), o.account)
        except Exception as e:
            log.warning("Octopus import: %s", e)
    asyncio.create_task(go())
    return {"ok": True}


@app.get("/api/setup/octopus")
def octopus_status():
    return db.get_state("octopus_status", {"state": "none"})


@app.get("/api/setup/hive/zones")
def hive_zones():
    if hive.is_configured():
        try:
            hive.refresh()
        except Exception as e:
            raise HTTPException(502, f"Hive didn't respond: {e}")
    return hive.zones()


class ZonePick(BaseModel):
    id: str


@app.post("/api/setup/hive/zone")
def hive_pick_zone(z: ZonePick):
    db.set_state("hive_heating_id", z.id)
    db.add_event("setup", f"Hive zone set to {z.id[:8]}")
    return hive.zones()


class HiveLogin(BaseModel):
    username: str
    password: str


class Code(BaseModel):
    code: str


@app.post("/api/setup/hive/login")
def setup_hive_login(b: HiveLogin):
    try:
        return {"status": hive.start_login(b.username.strip(), b.password)}
    except HiveError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("Hive login failed")
        name = type(e).__name__
        if name in ("HiveInvalidPassword", "HiveInvalidUsername"):
            raise HTTPException(400, "Hive says the email or password is wrong.")
        raise HTTPException(400, f"Hive login failed: {name}: {str(e)[:150]}")


@app.post("/api/setup/hive/sms")
def setup_hive_sms(b: Code):
    try:
        hive.finish_login(b.code)
        hive.refresh()
        return {"status": "ok", "heating": hive.summary()}
    except HiveError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Hive verification failed: {type(e).__name__}: {e}")


@app.post("/api/setup/tuya/start")
def setup_tuya_start(b: Code):
    try:
        return {"svg": tuya.start_qr(b.code)}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/setup/tuya/poll")
def setup_tuya_poll():
    try:
        done = tuya.poll_qr()
    except Exception as e:
        raise HTTPException(400, str(e))
    if done:
        tuya.refresh()
    return {"done": done, "devices": tuya.devices() if done else []}
