"""Is anyone in, when neither adult's phone is home?

Lights alone aren't enough - the kids sometimes leave them on. So a light only counts as someone being in if:
  1. it was switched on/off or changed (colour, brightness) AFTER the last phone left - i.e. someone is using it, or
  2. it's in red night mode during night hours - the kids asleep (a babysitter evening).
A light that's simply been left on, untouched since everyone went out, is ignored.
"""
import time
from datetime import datetime

from . import db

NIGHT_START, NIGHT_END = 19, 8          # red night light counts between 19:00 and 08:00


def phones_home(presence: dict) -> bool:
    return any(v["home"] for v in presence.values())


def empty_since(presence: dict) -> int | None:
    """When the last phone left (None if someone's phone is home)."""
    if phones_home(presence) or not presence:
        return None
    return max(v["since"] for v in presence.values())


def update_light_activity(devices: list) -> None:
    """Called every poll: remember when each visible light last changed."""
    sigs = db.get_state("light_sigs", {})
    activity = db.get_state("light_activity", {})
    now = int(time.time())
    for d in devices:
        if d["kind"] not in ("light", "tv") or d["hidden"]:
            continue
        if d["id"] in sigs and sigs[d["id"]] != d["sig"]:
            activity[d["id"]] = now
        sigs[d["id"]] = d["sig"]
    db.set_state("light_sigs", sigs)
    db.set_state("light_activity", activity)


def lights_signal(devices: list, presence: dict) -> dict:
    """Returns {"someone_in": bool, "why": str, "ignored": [names of lights left on]}."""
    lights = [d for d in devices if d["kind"] in ("light", "tv") and not d["hidden"] and d.get("online", True)]
    on = [d for d in lights if d["on"]]
    if phones_home(presence):
        return {"someone_in": True, "why": "", "ignored": []}
    since = empty_since(presence) or 0
    activity = db.get_state("light_activity", {})
    used = [d for d in lights if activity.get(d["id"], 0) > since]
    hour = datetime.now().hour
    night = hour >= NIGHT_START or hour < NIGHT_END
    red = [d for d in on if d.get("red") and d["kind"] == "light"] if night else []
    from . import router
    streams = router.streaming()
    if streams:
        return {"someone_in": True, "why": f"{streams[0]} is streaming", "ignored": []}
    kids = router.kids_devices_in_use()
    if kids:
        return {"someone_in": True, "why": f"{kids[0]} is being used", "ignored": []}
    if used:
        return {"someone_in": True, "why": f"{used[0]['name']} was used since you left", "ignored": []}
    if red:
        return {"someone_in": True, "why": f"{red[0]['name']} is on night red", "ignored": []}
    return {"someone_in": False, "why": "", "ignored": [d["name"] for d in on]}
