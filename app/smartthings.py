"""Samsung SmartThings (the TV, and anything else on the SmartThings account).

Samsung's personal access tokens only last 24 hours now, so we use one just once: to register this server as
its own small SmartThings "API only" app with OAuth. You then approve it once, and the server keeps itself
logged in by refreshing its token every day - no repeat setup.
"""
import base64
import secrets
import time

import requests

from . import db
from .config import PUBLIC_URL, load_secrets, save_secrets

API = "https://api.smartthings.com/v1"
TOKEN_URL = "https://api.smartthings.com/oauth/token"
AUTH_URL = "https://api.smartthings.com/oauth/authorize"
SCOPES = ["r:devices:*", "x:devices:*", "r:locations:*"]
REDIRECT = f"{PUBLIC_URL}/api/smartthings/callback"
TIMEOUT = 20


class STError(Exception):
    pass


def _cfg() -> dict:
    return load_secrets().get("smartthings", {})


def _save(**kw):
    c = _cfg()
    c.update(kw)
    save_secrets({"smartthings": c})


def is_configured() -> bool:
    return bool(_cfg().get("refresh_token"))


# ---------- one-off setup ----------
def register_app(pat: str) -> str:
    """Creates our OAuth app on the user's SmartThings account and returns the 'Allow' link to open."""
    pat = pat.strip()
    name = "homeenergy" + secrets.token_hex(3)
    body = {
        "appName": name,
        "displayName": "Home Energy",
        "description": "Billy's home energy dashboard",
        "appType": "API_ONLY",
        "classifications": ["CONNECTED_SERVICE"],
        "singleInstance": True,
        "apiOnly": {},
        "oauth": {"clientName": "Home Energy", "scope": SCOPES, "redirectUris": [REDIRECT]},
    }
    r = requests.post(f"{API}/apps", json=body, timeout=TIMEOUT,
                      headers={"Authorization": f"Bearer {pat}", "Content-Type": "application/json",
                               "Accept": "application/json"})
    if r.status_code >= 300:
        hint = " Make sure you ticked all the permission boxes when creating the token." if r.status_code in (401, 403) else ""
        raise STError(f"SmartThings said {r.status_code}: {r.text[:300] or r.reason}.{hint}")
    j = r.json()
    client_id, client_secret = j.get("oauthClientId"), j.get("oauthClientSecret")
    if not client_id:
        raise STError(f"SmartThings didn't return app details: {r.text[:200]}")
    state = secrets.token_urlsafe(16)
    _save(client_id=client_id, client_secret=client_secret, app_name=name, state=state)
    q = requests.Request("GET", AUTH_URL, params={
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": " ".join(SCOPES), "state": state}).prepare().url
    return q


def _token_request(data: dict) -> dict:
    c = _cfg()
    basic = base64.b64encode(f"{c['client_id']}:{c['client_secret']}".encode()).decode()
    r = requests.post(TOKEN_URL, data={**data, "client_id": c["client_id"]}, timeout=TIMEOUT,
                      headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
    if r.status_code >= 300:
        raise STError(f"SmartThings token error {r.status_code}: {r.text[:200]}")
    j = r.json()
    _save(access_token=j["access_token"], refresh_token=j.get("refresh_token", c.get("refresh_token")),
          expires=time.time() + int(j.get("expires_in", 86399)))
    return j


def finish_oauth(code: str, state: str):
    if state != _cfg().get("state"):
        raise STError("That approval link has expired - start the SmartThings setup again.")
    _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT})
    db.add_event("setup", "SmartThings connected")


# ---------- API ----------
def _token() -> str:
    c = _cfg()
    # Refresh when 75% of the token's ~24h life has gone (SmartThings' own advice).
    if not c.get("access_token") or time.time() > c.get("expires", 0) - 6 * 3600:
        _token_request({"grant_type": "refresh_token", "refresh_token": c["refresh_token"]})
        c = _cfg()
    return c["access_token"]


def _req(method: str, path: str, body=None):
    r = requests.request(method, f"{API}{path}", json=body, timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {_token()}"})
    if r.status_code >= 300:
        raise STError(f"SmartThings {r.status_code}: {r.text[:200]}")
    return r.json() if r.text else {}


def _kind(d: dict) -> str:
    cats = [c.get("name", "") for comp in d.get("components", []) for c in comp.get("categories", [])]
    caps = [c.get("id", "") for comp in d.get("components", []) for c in comp.get("capabilities", [])]
    name = (d.get("deviceTypeName") or d.get("ocf", {}).get("deviceTypeName") or "").lower()
    if "Television" in cats or "tv" in name or "tvChannel" in caps:
        return "tv"
    if "Light" in cats:
        return "light"
    return "other"


def refresh() -> list:
    devices = _req("GET", "/devices").get("items", [])
    out = []
    for d in devices:
        try:
            st = _req("GET", f"/devices/{d['deviceId']}/status").get("components", {}).get("main", {})
        except STError:
            st = {}
        sw = st.get("switch", {}).get("switch", {}).get("value")
        power = st.get("powerMeter", {}).get("power", {}).get("value")
        health = d.get("healthState", {}).get("state")
        out.append({
            "id": "st:" + d["deviceId"], "st_id": d["deviceId"],
            "name": d.get("label") or d.get("name") or "Device",
            "kind": _kind(d), "on": sw == "on", "online": health != "OFFLINE",
            "power_w": power, "source": "smartthings",
            "sig": str(sw),
        })
    db.set_state("smartthings_devices", out)
    return out


def devices() -> list:
    hidden = set(db.get_state("hidden_devices", []))
    return [{**d, "hidden": d["id"] in hidden, "home": "SmartThings", "red": False, "mode": None}
            for d in db.get_state("smartthings_devices", [])]


def command(device_id: str, action: str):
    st_id = device_id.removeprefix("st:")
    if action not in ("on", "off"):
        raise STError("Unknown action")
    _req("POST", f"/devices/{st_id}/commands",
         {"commands": [{"component": "main", "capability": "switch", "command": action}]})
    db.add_event("command", {"device": st_id[:8], "action": action})
