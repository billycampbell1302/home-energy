"""Home Energy - phase 1: connect Hive + Smart Life, log everything, phone dashboard with manual controls.

No automatic control happens in phase 1. It watches and records, so the learning in later phases has data.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, weather
from .config import ADMIN_PASSWORD, PEOPLE, POLL_MINUTES, PUBLIC_URL, SESSION_SECRET, webhook_token
from .hive import HiveError, hive
from .tuya import tuya

log = logging.getLogger("home-energy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
STATIC = Path(__file__).parent / "static"
COOKIE = "he_session"
COOKIE_DAYS = 365
_errors: dict[str, str] = {}


# ---------------- background logger ----------------
def presence() -> dict:
    p = db.get_state("presence", {})
    for name in PEOPLE:
        p.setdefault(name, {"home": True, "since": int(time.time())})
    return p


def poll_once():
    row = {}
    if hive.is_configured():
        try:
            s = hive.refresh()
            row.update(indoor_temp=s.get("indoor_temp"), target_temp=s.get("target_temp"),
                       heating_mode=s.get("mode"), boiler_on=int(bool(s.get("boiler_on"))),
                       hot_water_on=int(bool(s.get("hot_water_on"))))
            _errors.pop("hive", None)
        except Exception as e:  # keep logging other things even if Hive is down
            _errors["hive"] = str(e)[:200]
            log.warning("Hive poll failed: %s", e)
    if tuya.is_configured():
        try:
            devs = tuya.refresh()
            row["kids_lights_on"] = int(any(d["on"] for d in devs if d["kids"]))
            _errors.pop("tuya", None)
        except Exception as e:
            _errors["tuya"] = str(e)[:200]
            log.warning("Smart Life poll failed: %s", e)
    try:
        w = weather.current()
        row["outdoor_temp"] = w["temp"]
        _errors.pop("weather", None)
    except Exception as e:
        _errors["weather"] = str(e)[:200]
    row["people_home"] = sum(1 for v in presence().values() if v["home"])
    row["hold"] = int(bool(db.get_state("hold", False)))
    db.add_reading(**row)


async def poll_loop():
    while True:
        try:
            await asyncio.to_thread(poll_once)
        except Exception as e:
            log.exception("poll failed: %s", e)
        await asyncio.sleep(POLL_MINUTES * 60)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()


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


PUBLIC_PATHS = {"/login", "/api/login", "/manifest.json", "/sw.js", "/healthz"}


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
def manifest():
    return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
def sw():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------- dashboard data ----------------
@app.get("/api/status")
def status():
    try:
        w = weather.current()
    except Exception:
        w = None
    return {
        "heating": hive.summary() if hive.last_nodes else {"connected": False, "configured": hive.is_configured()},
        "hive_configured": hive.is_configured(),
        "tuya_configured": tuya.is_configured(),
        "devices": tuya.devices(),
        "weather": w,
        "presence": presence(),
        "hold": bool(db.get_state("hold", False)),
        "errors": _errors,
        "now": int(time.time()),
    }


@app.get("/api/history")
def history(hours: int = 24):
    return db.readings_since(min(hours, 24 * 14) * 3600)


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
    temp: float = 21.0


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
    return _hive_call(hive.boost, max(15, min(b.minutes, 360)), max(5, min(b.temp, 25)))


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
        tuya.command(device_id, a.action)
    except Exception as e:
        raise HTTPException(400, f"Smart Life: {e}")
    return tuya.devices()


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
    return tuya.devices()


class HomeHidden(BaseModel):
    home: str
    hidden: bool


@app.post("/api/homes/hidden")
def hide_home(h: HomeHidden):
    """Hide or show every device in one Smart Life home (e.g. devices set up at someone else's house)."""
    ids = set(db.get_state("hidden_devices", []))
    for d in tuya.devices():
        if d["home"] == h.home:
            (ids.add if h.hidden else ids.discard)(d["id"])
    db.set_state("hidden_devices", sorted(ids))
    return tuya.devices()


class Hold(BaseModel):
    on: bool


@app.post("/api/hold")
def set_hold(h: Hold):
    db.set_state("hold", h.on)
    db.add_event("command", {"hold": h.on})
    return {"hold": h.on}


# ---------------- presence ----------------
def _set_presence(person: str, state: str):
    match = next((p for p in PEOPLE if p.lower() == person.lower()), None)
    if not match:
        raise HTTPException(404, f"Unknown person. Use one of: {', '.join(PEOPLE)}")
    home = state.lower() in ("home", "arrive", "arrived", "in", "1", "true")
    p = presence()
    if p[match]["home"] != home:
        p[match] = {"home": home, "since": int(time.time())}
        db.set_state("presence", p)
        db.add_event("presence", {"person": match, "home": home})
    return p


@app.api_route("/hook/presence", methods=["GET", "POST"])
def presence_hook(token: str, person: str, state: str):
    """Called by the iPhone Shortcuts automations when someone arrives or leaves."""
    if not hmac.compare_digest(token, webhook_token()):
        raise HTTPException(403, "Bad token")
    _set_presence(person, state)
    return {"ok": True}


class PresenceSet(BaseModel):
    person: str
    home: bool


@app.post("/api/presence")
def presence_manual(p: PresenceSet):
    return _set_presence(p.person, "home" if p.home else "away")


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
        "people": PEOPLE,
        "webhooks": {
            p: {
                "arrive": f"{PUBLIC_URL}/hook/presence?token={tok}&person={p}&state=home",
                "leave": f"{PUBLIC_URL}/hook/presence?token={tok}&person={p}&state=away",
            } for p in PEOPLE
        },
    }


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
