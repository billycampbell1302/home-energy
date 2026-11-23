"""Smart Life (Tuya) connection for the kids' lights and any Smart Life plugs.

Uses Tuya's official device-sharing SDK with the same QR-code login that Home Assistant uses,
so no Tuya developer account is needed: you enter the User Code from the Smart Life app,
then scan the QR code shown on the Setup page with the Smart Life app.
"""
import io
import json
import threading
import time

import qrcode
import qrcode.image.svg
from tuya_sharing import LoginControl, Manager, SharingTokenListener

from . import db
from .config import load_secrets, save_secrets

# Home Assistant's public Tuya app registration (the documented route for personal use without a developer account).
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"

LIGHT_CATEGORIES = {"dj", "dd", "xdd", "fwd", "dc", "fsd", "tyndj", "gyd"}
PLUG_CATEGORIES = {"cz", "pc", "kg", "tdq"}


class _TokenSaver(SharingTokenListener):
    def update_token(self, token_info):
        s = load_secrets().get("tuya", {})
        s["token_info"] = token_info
        save_secrets({"tuya": s})


class TuyaClient:
    def __init__(self):
        self._lock = threading.RLock()
        self._login = LoginControl()
        self._pending = None   # (qr_token, user_code)
        self._manager = None
        self.home_of: dict[str, str] = {}
        self.last_ok = 0.0

    # ---------- setup ----------
    def start_qr(self, user_code: str) -> str:
        """Returns an SVG QR code for the Smart Life app to scan."""
        user_code = user_code.strip()
        resp = self._login.qr_code(CLIENT_ID, SCHEMA, user_code)
        if not resp.get("success"):
            raise ValueError(resp.get("msg") or "Smart Life rejected that User Code - check it (it's case sensitive).")
        token = resp["result"]["qrcode"]
        self._pending = (token, user_code)
        img = qrcode.make(f"tuyaSmart--qrLogin?token={token}", image_factory=qrcode.image.svg.SvgPathImage, box_size=10)
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode()
        return svg[svg.find("<svg"):]

    def poll_qr(self) -> bool:
        if not self._pending:
            return False
        token, user_code = self._pending
        ok, info = self._login.login_result(token, CLIENT_ID, user_code)
        if not ok:
            return False
        save_secrets({"tuya": {
            "user_code": user_code,
            "terminal_id": info["terminal_id"],
            "endpoint": info["endpoint"],
            "token_info": {
                "t": info["t"],
                "uid": info["uid"],
                "expire_time": info["expire_time"],
                "access_token": info["access_token"],
                "refresh_token": info["refresh_token"],
            },
        }})
        self._pending = None
        self._manager = None
        db.add_event("setup", "Smart Life connected")
        return True

    def is_configured(self) -> bool:
        return bool(load_secrets().get("tuya", {}).get("terminal_id"))

    # ---------- devices ----------
    def _mgr(self) -> Manager:
        if self._manager is None:
            s = load_secrets()["tuya"]
            self._manager = Manager(CLIENT_ID, s["user_code"], s["terminal_id"], s["endpoint"],
                                    s["token_info"], _TokenSaver())
        return self._manager

    def refresh(self) -> list:
        with self._lock:
            m = self._mgr()
            # Same as Manager.update_device_cache, but also remembers each device's Smart Life home.
            homes = m.home_repository.query_homes()
            m.user_homes = homes
            m.device_map.clear()
            for home in homes:
                for device in m.device_repository.query_devices_by_home(home.id):
                    m.device_map[device.id] = device
                    self.home_of[device.id] = home.name
            self.last_ok = time.time()
            return self.devices()

    def devices(self) -> list:
        if self._manager is None:
            return []
        out = []
        kids_ids = set(db.get_state("kids_lights", []))
        hidden_ids = set(db.get_state("hidden_devices", []))
        for d in self._manager.device_map.values():
            if d.category in LIGHT_CATEGORIES:
                kind = "light"
            elif d.category in PLUG_CATEGORIES:
                kind = "plug"
            else:
                kind = "other"
            st = d.status or {}
            on_key = _first(st, ["switch_led", "switch_1", "switch"])
            out.append({
                "id": d.id,
                "name": d.name,
                "kind": kind,
                "online": d.online,
                "on": bool(st.get(on_key)) if on_key else None,
                "mode": st.get("work_mode"),
                "red": _is_red(st),
                "sig": json.dumps([st.get(on_key), st.get("work_mode"), st.get("colour_data_v2") or st.get("colour_data"),
                                   st.get("bright_value_v2") or st.get("bright_value")], sort_keys=True, default=str),
                "kids": d.id in kids_ids,
                "hidden": d.id in hidden_ids,
                "home": self.home_of.get(d.id, ""),
                "power_w": _power(st),
            })
        return sorted(out, key=lambda x: (x["kind"], x["name"]))

    def command(self, device_id: str, action: str):
        with self._lock:
            m = self._mgr()
            d = m.device_map.get(device_id)
            if d is None:
                m.update_device_cache()
                d = m.device_map[device_id]
            st = d.status or {}
            on_key = _first(st, ["switch_led", "switch_1", "switch"])
            cmds = []
            if action == "off":
                cmds = [{"code": on_key, "value": False}]
            elif action == "on":
                cmds = [{"code": on_key, "value": True}]
            elif action in ("red", "white"):
                cmds = [{"code": on_key, "value": True}] + _colour_cmds(d, action)
            else:
                raise ValueError("Unknown action")
            m.send_commands(device_id, cmds)
            db.add_event("command", {"device": d.name, "action": action})
            # Update our cached copy straight away so the dashboard reflects it.
            for c in cmds:
                d.status[c["code"]] = c["value"] if not isinstance(c["value"], str) or not c["value"].startswith("{") else json.loads(c["value"])


def _first(status: dict, keys: list):
    for k in keys:
        if k in status:
            return k
    return None


def _is_red(st: dict) -> bool:
    """Light on, in colour mode, with a red hue (the kids' night setting)."""
    if st.get("work_mode") != "colour":
        return False
    c = st.get("colour_data_v2") or st.get("colour_data")
    try:
        c = json.loads(c) if isinstance(c, str) else c
        h = float(c.get("h", -1))
        return 0 <= h <= 25 or h >= 335
    except Exception:
        return False


def _power(st: dict):
    # Smart Life plugs with energy monitoring report cur_power in tenths of a watt.
    if "cur_power" in st:
        try:
            return round(float(st["cur_power"]) / 10, 1)
        except (TypeError, ValueError):
            return None
    return None


def _colour_cmds(d, action: str) -> list:
    st = d.status or {}
    cmds = []
    if "work_mode" in st:
        cmds.append({"code": "work_mode", "value": "colour" if action == "red" else "white"})
    if action == "red":
        if "colour_data_v2" in st:      # h 0-360, s 0-1000, v 0-1000
            cmds.append({"code": "colour_data_v2", "value": json.dumps({"h": 0, "s": 1000, "v": 100})})
        elif "colour_data" in st:       # older bulbs: s and v 0-255
            cmds.append({"code": "colour_data", "value": json.dumps({"h": 0, "s": 255, "v": 26})})
    else:
        if "bright_value_v2" in st:
            cmds.append({"code": "bright_value_v2", "value": 1000})
        elif "bright_value" in st:
            cmds.append({"code": "bright_value", "value": 255})
        if "temp_value_v2" in st:
            cmds.append({"code": "temp_value_v2", "value": 300})   # warm white
    return cmds


tuya = TuyaClient()
