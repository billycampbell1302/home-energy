"""Hive heating connection.

pyhiveapi is used only for the login (Hive uses AWS Cognito with SMS two-factor).
After that we call Hive's REST API directly, which keeps this simple and easy to fix.
The server registers itself as a "remembered device", so the SMS code is only needed once.
"""
import json
import threading
import time

import re

import requests
from pyhiveapi import Auth
from pyhiveapi.api import hive_api as _hive_api

# Hive's login needs two IDs that pyhiveapi scrapes from Hive's sign-in page. That scrape breaks
# whenever Hive changes the page, so: search the page more loosely, and fall back to known values.
_FALLBACK_POOL_ID = "eu-west-1_SamNfoWtf"
_FALLBACK_CLIENT_ID = "3rl4i0ajrmtdm8sbre54p9dvd9"


def _login_info(self=None):
    pool, client = None, None
    try:
        html = requests.get("https://sso.hivehome.com/", timeout=15,
                            headers={"User-Agent": "Mozilla/5.0"}).text
        m1 = re.search(r"HiveSSOPoolId[\"']?\s*[=:]\s*[\"']([\w-]+_[\w]+)", html)
        m2 = re.search(r"HiveSSOPublicCognitoClientId[\"']?\s*[=:]\s*[\"']([\w]+)", html)
        pool, client = (m1.group(1) if m1 else None), (m2.group(1) if m2 else None)
    except Exception:
        pass
    pool, client = pool or _FALLBACK_POOL_ID, client or _FALLBACK_CLIENT_ID
    return {"UPID": pool, "CLIID": client, "REGION": pool}


_hive_api.HiveApi.getLoginInfo = _login_info

from . import db
from .config import load_secrets, save_secrets

BASE = "https://beekeeper-uk.hivehome.com/1.0"
TIMEOUT = 15


class HiveError(Exception):
    pass


class HiveClient:
    def __init__(self):
        self._lock = threading.RLock()
        self._pending_auth = None        # Auth object kept between password step and SMS step
        self._pending_result = None
        self._id_token = None
        self._refresh_token = None
        self._token_time = 0.0
        self._auth = None
        self.last_nodes = []
        self.homes = []
        self.last_ok = 0.0

    # ---------- setup (called from the Setup page) ----------
    def start_login(self, username: str, password: str) -> str:
        """Step 1. Returns 'sms_required' or 'ok'."""
        with self._lock:
            auth = Auth(username=username, password=password)
            result = auth.login()
            if result and result.get("ChallengeName") == "SMS_MFA":
                self._pending_auth = auth
                self._pending_result = result
                save_secrets({"hive_username": username, "hive_password": password})
                return "sms_required"
            if result and "AuthenticationResult" in result:
                # Account has no 2FA. Hive's remembered-device login needs 2FA turned on.
                raise HiveError("Turn on two-factor authentication in the Hive app first, then try again.")
            raise HiveError("Hive login failed - check the email and password.")

    def finish_login(self, code: str):
        """Step 2: the SMS code. Registers this server as a remembered device."""
        with self._lock:
            auth = self._pending_auth
            if auth is None:
                raise HiveError("Start again: enter your Hive email and password first.")
            result = auth.sms_2fa(code.strip(), self._pending_result)
            if not result or "AuthenticationResult" not in result:
                raise HiveError("That code didn't work - request a new one and try again.")
            auth.device_registration("home-energy-server")
            group_key, device_key, device_password = auth.get_device_data()
            save_secrets({
                "hive_device_group_key": group_key,
                "hive_device_key": device_key,
                "hive_device_password": device_password,
            })
            self._store_tokens(result["AuthenticationResult"])
            self._auth = auth
            self._pending_auth = None
            db.add_event("setup", "Hive connected")

    def is_configured(self) -> bool:
        s = load_secrets()
        return bool(s.get("hive_device_key") and s.get("hive_password"))

    # ---------- tokens ----------
    def _store_tokens(self, auth_result: dict):
        self._id_token = auth_result["IdToken"]
        self._refresh_token = auth_result.get("RefreshToken", self._refresh_token)
        self._token_time = time.time()

    def _device_login(self):
        s = load_secrets()
        self._auth = Auth(
            username=s["hive_username"],
            password=s["hive_password"],
            device_group_key=s["hive_device_group_key"],
            device_key=s["hive_device_key"],
            device_password=s["hive_device_password"],
        )
        result = self._auth.device_login()
        if not result or "AuthenticationResult" not in result:
            raise HiveError("Hive device login failed - reconnect Hive on the Setup page.")
        self._store_tokens(result["AuthenticationResult"])

    def _ensure_token(self):
        if not self.is_configured():
            raise HiveError("Hive not connected yet")
        # Hive ID tokens last an hour; refresh after 50 minutes.
        if self._id_token and time.time() - self._token_time < 50 * 60:
            return
        if self._auth and self._refresh_token:
            try:
                result = self._auth.refresh_token(self._refresh_token)
                if result and "AuthenticationResult" in result:
                    self._store_tokens(result["AuthenticationResult"])
                    return
            except Exception:
                pass
        self._device_login()

    def _request(self, method: str, path: str, body: dict | None = None):
        with self._lock:
            self._ensure_token()
            headers = {"content-type": "application/json", "Accept": "*/*", "authorization": self._id_token}
            r = requests.request(method, BASE + path, headers=headers,
                                 data=json.dumps(body) if body is not None else None, timeout=TIMEOUT)
            if r.status_code == 401:
                self._id_token = None
                self._ensure_token()
                headers["authorization"] = self._id_token
                r = requests.request(method, BASE + path, headers=headers,
                                     data=json.dumps(body) if body is not None else None, timeout=TIMEOUT)
            if r.status_code >= 300:
                raise HiveError(f"Hive API {r.status_code}: {r.text[:200]}")
            return r.json() if r.text else {}

    # ---------- reading ----------
    def track_changes(self, people_home: list | None = None):
        """Logs changes made outside this app (on the thermostat or in the Hive app) - each one tells the learning
        what temperature you actually wanted at that time. Also snapshots the Hive schedule once a day."""
        s = self.summary()
        if not s.get("connected"):
            return
        prev = db.get_state("hive_prev", {})
        cur = {"target": s.get("target_temp"), "mode": s.get("mode"), "boost_minutes": s.get("boost_minutes")}
        own_recent = time.time() - db.get_state("hive_last_own_change", 0) < 15 * 60
        changed = {k: cur[k] for k in ("target", "mode")} != {k: prev.get(k) for k in ("target", "mode")}
        if prev and changed and not own_recent:
            # A target change while on schedule is just the schedule moving on - only log real overrides.
            if cur["mode"] != prev.get("mode") or cur["mode"] in ("MANUAL", "BOOST"):
                db.add_event("manual", {"from": prev, "to": cur, "indoor": s.get("indoor_temp")})
                # Comfort evidence: up/boost = too cold, down/cancelled boost = too warm (for whoever's home)
                from . import comfort
                feeling = None
                if cur["mode"] == "BOOST" and prev.get("mode") != "BOOST":
                    feeling = "cold"
                elif prev.get("mode") == "BOOST" and cur["mode"] != "BOOST" and (prev.get("boost_minutes") or 0) > 5:
                    feeling = "warm"                      # boost cancelled early
                elif cur["target"] is not None and prev.get("target") is not None:
                    if cur["target"] > prev["target"]:
                        feeling = "cold"
                    elif cur["target"] < prev["target"]:
                        feeling = "warm"
                if feeling and people_home:
                    comfort.record(people_home, feeling, s.get("indoor_temp"), "hive")
            elif cur["target"] != prev.get("target"):
                db.add_event("schedule", {"target": cur["target"]})
        db.set_state("hive_prev", cur)
        today = time.strftime("%Y-%m-%d")
        if db.get_state("hive_schedule_day") != today:
            heat = self._product("heating") or {}
            sched = heat.get("state", {}).get("schedule")
            if sched:
                db.add_event("hive_schedule", sched)
            db.set_state("hive_schedule_day", today)

    def refresh(self) -> dict:
        """Reads every Hive home on the account (e.g. yours and a parent's) and tags each product with its home."""
        base = "/nodes/all?products=true&devices=true&actions=true"
        data = self._request("GET", base)
        homes = (data.get("homes") or {}).get("homes") or []
        products, seen = [], set()
        sources = [(data, None)] if len(homes) <= 1 else []
        for h in homes if len(homes) > 1 else []:
            try:
                sources.append((self._request("GET", f"{base}&homeId={h['id']}"), h))
            except HiveError:
                continue
        for d, h in sources:
            for p in d.get("products", []):
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                home = h or (homes[0] if homes else {})
                p["_home_id"], p["_home_name"] = home.get("id"), home.get("name", "")
                products.append(p)
        self.homes = homes
        self.last_nodes = products
        self.last_ok = time.time()
        return self.summary()

    def _product(self, ptype: str):
        matches = [p for p in self.last_nodes if p.get("type") == ptype]
        if ptype == "heating":
            chosen = db.get_state("hive_heating_id")
            for p in matches:
                if p.get("id") == chosen:
                    return p
        else:
            # Hot water etc: use the one in the same Hive home as the chosen heating.
            heat = self._product("heating")
            same = [p for p in matches if heat and p.get("_home_id") == heat.get("_home_id")]
            matches = same or matches
        # Prefer a single (not grouped) product that's online - old/removed thermostats can linger offline.
        ranked = sorted(matches, key=lambda p: (p.get("isGroup", False), not p.get("props", {}).get("online", True)))
        return ranked[0] if ranked else None

    def zones(self) -> list:
        """Every heating zone/thermostat on the account, so the right one can be picked on the Setup page."""
        chosen = (self._product("heating") or {}).get("id")
        out = []
        for p in self.last_nodes:
            if p.get("type") not in ("heating", "trvcontrol"):
                continue
            st, pr = p.get("state", {}), p.get("props", {})
            out.append({
                "id": p["id"], "type": p.get("type"),
                "name": st.get("name") or pr.get("zoneName") or p["id"][:8],
                "home": p.get("_home_name", ""),
                "indoor_temp": _num(pr.get("temperature")), "target_temp": _num(st.get("target")),
                "mode": st.get("mode"), "online": pr.get("online", True),
                "group": p.get("isGroup", False), "chosen": p["id"] == chosen,
            })
        return out

    def summary(self) -> dict:
        heat = self._product("heating")
        water = self._product("hotwater")
        out = {"connected": bool(heat), "updated": self.last_ok}
        if heat:
            st, pr = heat.get("state", {}), heat.get("props", {})
            mode = st.get("mode")
            if st.get("boost"):
                mode = "BOOST"
            out.update({
                "heating_id": heat["id"],
                "zone_name": st.get("name") or "",
                "home_name": heat.get("_home_name", ""),
                "indoor_temp": _num(pr.get("temperature")),
                "target_temp": _num(st.get("target")),
                "mode": mode,
                "boost_minutes": st.get("boost") or 0,
                "boiler_on": bool(pr.get("working")),
                "online": pr.get("online", True),
            })
        if water:
            st = water.get("state", {})
            out.update({
                "hot_water_id": water["id"],
                "hot_water_mode": "BOOST" if st.get("boost") else st.get("mode"),
                "hot_water_on": st.get("status") == "ON" or bool(water.get("props", {}).get("working")),
            })
        return out

    # ---------- controls ----------
    def _set(self, ptype: str, pid: str, **values):
        # Hive's API expects the values as strings, matching the official app.
        body = {k: str(v) for k, v in values.items()}
        prod = next((p for p in self.last_nodes if p.get("id") == pid), {})
        q = f"?homeId={prod['_home_id']}" if prod.get("_home_id") and len(getattr(self, "homes", [])) > 1 else ""
        self._request("POST", f"/nodes/{ptype}/{pid}{q}", body)
        db.add_event("command", {"device": ptype, **body})
        db.set_state("hive_last_own_change", time.time())

    def set_target(self, temp: float):
        s = self.summary()
        # In schedule mode a new target only lasts until the next schedule change, same as the thermostat.
        self._set("heating", s["heating_id"], target=round(float(temp) * 2) / 2)

    def set_mode(self, mode: str):
        if mode not in ("SCHEDULE", "MANUAL", "OFF"):
            raise HiveError("Unknown mode")
        s = self.summary()
        self._set("heating", s["heating_id"], mode=mode)

    def boost(self, minutes: int, temp: float):
        s = self.summary()
        self._set("heating", s["heating_id"], mode="BOOST", boost=int(minutes), target=round(float(temp) * 2) / 2)

    def boost_off(self):
        heat = self._product("heating")
        prev = (heat or {}).get("props", {}).get("previous", {})
        values = {"mode": prev.get("mode", "SCHEDULE")}
        if values["mode"] == "MANUAL" and prev.get("target"):
            values["target"] = prev["target"]
        self._set("heating", heat["id"], **values)

    def hot_water_boost(self, minutes: int):
        s = self.summary()
        self._set("hotwater", s["hot_water_id"], mode="BOOST", boost=int(minutes))


def _num(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


hive = HiveClient()
