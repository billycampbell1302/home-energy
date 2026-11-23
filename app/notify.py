"""Phone notifications (Web Push) for the home-screen app.

iPhones (iOS 16.4+) allow push notifications for web apps added to the home screen. The server signs each
message with its own key (VAPID), generated on first use - no third-party service or account needed.

Alerts are worked out from the data and each one is only sent once per "episode" (see `once`).
"""
import base64
import json
import logging
import time

from . import db
from .config import load_secrets, save_secrets

log = logging.getLogger("home-energy")

TYPES = {
    "heating_empty": "Heating on with nobody home",
    "cold_home": "Flat getting cold while someone's in",
    "frost": "Frost forecast tonight",
    "lights_left": "Lights left on when everyone's out",
    "washing_done": "Washing finished",
    "high_use": "Unusually high electricity use",
    "tariff_change": "Energy prices changed",
    "contract": "Fixed deal ending / ended",
    "service_down": "Something stopped working",
    "arrivals": "Arrivals and departures",
    "daily": "Daily report (8am)",
    "weekly": "Weekly summary (Sunday evening)",
    "monthly": "Monthly report",
}
DEFAULT_OFF = {"arrivals"}


def _keys() -> dict:
    s = load_secrets()
    if "vapid" not in s:
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid02
        v = Vapid02()
        v.generate_keys()
        pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        s = save_secrets({"vapid": {"private_pem": v.private_pem().decode(),
                                    "public": base64.urlsafe_b64encode(pub).rstrip(b"=").decode()}})
    return s["vapid"]


def public_key() -> str:
    return _keys()["public"]


def subscribe(sub: dict):
    subs = db.get_state("push_subs", [])
    if sub.get("endpoint") and all(x.get("endpoint") != sub["endpoint"] for x in subs):
        subs.append(sub)
    db.set_state("push_subs", subs)


def settings() -> dict:
    saved = db.get_state("notify_settings", {})
    return {k: saved.get(k, k not in DEFAULT_OFF) for k in TYPES}


def send(kind: str, title: str, body: str, url: str = "/", force: bool = False) -> list:
    """Sends to every subscribed phone. Returns [{"ok": bool, "status": int|None, "error": str}] per phone."""
    if not force and not settings().get(kind, True):
        return []
    from py_vapid import Vapid02
    from pywebpush import WebPushException, webpush
    from .config import PUBLIC_URL
    signer = Vapid02.from_pem(_keys()["private_pem"].encode())   # pywebpush wants a key object, not PEM text
    keep, results = [], []
    for sub in db.get_state("push_subs", []):
        try:
            r = webpush(subscription_info=sub, data=json.dumps({"title": title, "body": body, "url": url}),
                        vapid_private_key=signer, vapid_claims={"sub": PUBLIC_URL}, ttl=3600,
                        headers={"urgency": "high"})
            keep.append(sub)
            results.append({"ok": True, "status": getattr(r, "status_code", None), "error": ""})
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            text = getattr(getattr(e, "response", None), "text", "") or str(e)
            if code not in (404, 410):           # 404/410 = phone unsubscribed; drop it
                keep.append(sub)
            results.append({"ok": False, "status": code, "error": text[:200]})
            log.warning("push failed (%s): %s", code, text[:200])
        except Exception as e:
            keep.append(sub)
            results.append({"ok": False, "status": None, "error": str(e)[:200]})
            log.warning("push failed: %s", e)
    db.set_state("push_subs", keep)
    db.set_state("push_last", {"ts": int(time.time()), "title": title, "results": results})
    db.add_event("notify", {"title": title, "sent": sum(r["ok"] for r in results), "failed": sum(not r["ok"] for r in results)})
    return results


def once(key: str, condition: bool, kind: str, title: str, body: str):
    """Send when `condition` becomes true; re-arm only once it's been false again."""
    fired = db.get_state("notify_fired", {})
    if condition and not fired.get(key):
        send(kind, title, body)
        fired[key] = int(time.time())
    elif not condition and fired.get(key):
        fired.pop(key)
    db.set_state("notify_fired", fired)
