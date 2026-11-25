"""Comfort check-ins: "How does it feel?" notifications to the phone of whoever is home - only when the answer
would teach the app something.

Sent to a person when ALL of these are true:
  • they've been home 30+ minutes, it's 07:00–21:30
  • no check-in to them in the last 3 hours, max 2 a day, and they haven't tapped 🥶/👍/🥵 in the last 90 minutes
  • the answer is useful: fewer than 5 answers from them for this part of the day, OR the flat is now 0.7°+ away
    from what they've said is comfortable (an unusual temperature worth checking)
  • a good moment: the heating has just reached temperature, the temperature is unusual, or it's a
    routine moment (around 08:00, 13:00, 19:00)
Once a part of the day has 5+ answers it only asks about unusual temperatures, at most every other day.
Tapping the notification opens the app straight to three big buttons.
"""
import time
from datetime import datetime

from . import db, notify
from .config import PEOPLE


def _subscribed(person: str) -> bool:
    return any(s.get("person") == person for s in db.get_state("push_subs", []))


def check(s: dict, presence: dict):
    from . import comfort
    if not notify.settings().get("comfort_check", True):
        return
    indoor = s.get("indoor_temp")
    if indoor is None:
        return
    now = time.time()
    dt = datetime.now()
    h = dt.hour + dt.minute / 60
    if not (7 <= h < 21.5):
        return
    sent = db.get_state("checkins", {})
    log = db.get_state("comfort_log", [])
    period = comfort.period_of(dt.hour)
    est = comfort.estimates(period)
    # Did the heating just reach temperature? (boiler went off in the last 15 minutes)
    rows = db.readings_since(20 * 60)
    just_reached = len(rows) >= 2 and any(r.get("boiler_on") for r in rows[:-1]) and not rows[-1].get("boiler_on")
    routine = any(abs(h - x) <= 0.5 for x in (8, 13, 19))
    for person in PEOPLE:
        p = presence.get(person, {})
        if not p.get("home") or now - p.get("since", now) < 30 * 60 or not _subscribed(person):
            continue
        mine = [t for t in sent.get(person, []) if now - t < 86400]
        if (mine and now - max(mine) < 3 * 3600) or len(mine) >= 2:
            continue
        if any(e["ts"] > now - 90 * 60 and person in e["people"] for e in log):
            continue
        e = est.get(person, {})
        samples = e.get("by_period", {}).get(period, {}).get("samples", 0)
        unusual = abs(indoor - e.get("temp", 20)) >= 0.7
        if samples >= 5 and not unusual:
            continue
        if samples >= 5 and mine and now - max(sent.get(person, [0])) < 2 * 86400:
            continue
        if not (just_reached or unusual or routine):
            continue
        notify.send("comfort_check", "How does it feel?",
                    f"It's {indoor:.1f}° at home. Tap to say too cold, just right or too warm.",
                    url="/?feel=1", person=person)
        sent.setdefault(person, []).append(int(now))
        sent[person] = sent[person][-20:]
        db.add_event("notify", {"title": f"comfort check-in to {person} at {indoor:.1f}°"})
    db.set_state("checkins", sent)
