"""Presence from the OpenWrt router.

Every minute the router reports which devices are on the Wi-Fi and how many bytes each has sent/received
(`iw station dump`). From that:
  - a person's phone on the Wi-Fi  -> that person is home (with a 10-minute grace period, because
    iPhones drop Wi-Fi briefly when locked)
  - a Fire Stick moving lots of data -> someone's streaming, so someone's in. A Fire Stick that's simply been
    left on idles at almost no traffic, so it doesn't count.
Phones and Fire Sticks are recognised automatically from their names; anything can be re-assigned on Setup.
"""
import time

from . import db
from .config import PEOPLE

AWAY_GRACE = 10 * 60          # phone must be gone this long before the person counts as out
NIGHT_GRACE = 30 * 60         # longer overnight (23:00-07:00) - sleeping phones and the nightly router reboot
SETTLE = 20 * 60              # after a router reboot / gap in reports, nobody can be marked as "left"
STREAM_BPS = 250_000          # ~2 Mbit/s sustained = video streaming (idle Fire Stick is a few KB/s)
STREAM_HOLD = 3 * 60          # keep "streaming" for a few minutes over short pauses/buffering
KIDS_BPS = 25_000             # ~200 kbit/s: a tablet/console actually being used (videos, online games)
KIDS_HOLD = 5 * 60


def _auto_role(name: str, mac: str) -> str:
    n = (name or "").lower()
    for p in PEOPLE:
        if p.lower() in n:
            return "person:" + p
    if any(k in n for k in ("fire", "amazon", "aft", "firetv", "fire-tv")):
        return "streamer"
    return ""


def last_seen(person: str) -> float:
    devs, roles = db.get_state("router_devices", {}), db.get_state("router_roles", {})
    return max([devs.get(m, {}).get("last_seen", 0) for m, r in roles.items() if r == "person:" + person] or [0])


def settling() -> bool:
    return time.time() < db.get_state("router_settle_until", 0)


def report(clients: list, uptime: int | None = None) -> dict:
    """clients: [{"mac","name","ip","rx","tx"}] - cumulative byte counters per Wi-Fi association."""
    now = time.time()
    last = db.get_state("router_last_report", 0)
    if (uptime is not None and uptime < SETTLE) or (last and now - last > 3 * 60):
        if not settling():
            db.add_event("router", {"note": "router restarted or stopped reporting – departures ignored for 20 min"})
        db.set_state("router_settle_until", now + SETTLE)
    devs = db.get_state("router_devices", {})
    roles = db.get_state("router_roles", {})
    for c in clients:
        mac = (c.get("mac") or "").lower()
        if not mac:
            continue
        d = devs.get(mac, {})
        total = int(c.get("rx") or 0) + int(c.get("tx") or 0)
        if d.get("bytes") is not None and d.get("last_seen") and total >= d["bytes"]:
            dt = max(1, now - d["last_seen"])
            d["bps"] = round((total - d["bytes"]) / dt)
        else:
            d["bps"] = 0      # first sight or counters reset (device reconnected)
        was_streaming = d.get("streaming_until", 0) > now
        role = roles.get(mac, "")
        if d["bps"] >= STREAM_BPS:
            if not was_streaming and role == "streamer":
                db.add_event("device", {"name": c.get("name") or d.get("name") or mac, "kind": "streamer", "state": "streaming"})
            d["streaming_until"] = now + STREAM_HOLD
        if d["bps"] >= KIDS_BPS:
            d["active_until"] = now + KIDS_HOLD
        d.update(bytes=total, last_seen=now, name=c.get("name") or d.get("name") or "", ip=c.get("ip") or d.get("ip"),
                 wired=bool(c.get("wired")))
        if mac not in roles:
            auto = _auto_role(d["name"], mac)
            if auto:
                roles[mac] = auto
        devs[mac] = d
    # forget devices not seen for a week
    devs = {m: d for m, d in devs.items() if now - d.get("last_seen", 0) < 7 * 86400}
    db.set_state("router_devices", devs)
    db.set_state("router_roles", roles)
    db.set_state("router_last_report", now)
    return {"devices": len(devs)}


def people_home() -> dict:
    """person -> True/False/None (None = no phone assigned to them)."""
    now = time.time()
    devs, roles = db.get_state("router_devices", {}), db.get_state("router_roles", {})
    out = {p: None for p in PEOPLE}
    hour = time.localtime().tm_hour
    grace = NIGHT_GRACE if hour >= 23 or hour < 7 else AWAY_GRACE
    for mac, role in roles.items():
        if not role.startswith("person:"):
            continue
        p = role.split(":", 1)[1]
        seen = devs.get(mac, {}).get("last_seen", 0)
        home = now - seen < grace
        out[p] = bool(out[p]) or home
    if settling():   # just rebooted: phones still reconnecting - only arrivals count
        out = {p: (True if v else None) for p, v in out.items()}
    return out


def streaming() -> list:
    now = time.time()
    devs, roles = db.get_state("router_devices", {}), db.get_state("router_roles", {})
    return [devs[m].get("name") or "Fire Stick" for m, r in roles.items()
            if r == "streamer" and m in devs and devs[m].get("streaming_until", 0) > now
            and now - devs[m].get("last_seen", 0) < 5 * 60]


def kids_devices_in_use() -> list:
    """Kids' tablets/consoles being used right now (enough data flowing to mean someone's on it)."""
    now = time.time()
    devs, roles = db.get_state("router_devices", {}), db.get_state("router_roles", {})
    return [devs[m].get("name") or "Kids' device" for m, r in roles.items()
            if r == "kids" and m in devs and devs[m].get("active_until", 0) > now
            and now - devs[m].get("last_seen", 0) < 5 * 60]


def is_active() -> bool:
    last = db.get_state("router_last_report", 0)
    return time.time() - last < 10 * 60


def device_list() -> list:
    devs, roles = db.get_state("router_devices", {}), db.get_state("router_roles", {})
    now = time.time()
    rows = [{"mac": m, "name": d.get("name") or "", "ip": d.get("ip"), "seen_ago": int(now - d.get("last_seen", 0)),
             "kbps": round(d.get("bps", 0) * 8 / 1000), "role": roles.get(m, ""), "wired": d.get("wired", False),
             "online": now - d.get("last_seen", 0) < 3 * 60,
             "streaming": d.get("streaming_until", 0) > now, "in_use": d.get("active_until", 0) > now}
            for m, d in devs.items()]
    return sorted(rows, key=lambda r: (r["role"] == "", r["seen_ago"]))


def set_role(mac: str, role: str):
    roles = db.get_state("router_roles", {})
    roles[mac.lower()] = role
    db.set_state("router_roles", roles)


SCRIPT = r"""#!/bin/sh
# Home Energy: report Wi-Fi and wired devices and their traffic to the dashboard (runs every minute from cron).
URL='__URL__'
LEASES=/tmp/dhcp.leases
OUT=""
for IF in $(iw dev 2>/dev/null | awk '/Interface/ {print $2}'); do
  OUT="$OUT$(iw dev "$IF" station dump 2>/dev/null | awk '/^Station/ {mac=$2} /rx bytes:/ {rx=$3} /tx bytes:/ {print mac, rx, $3}')
"
done
WIFI=$(echo "$OUT" | awk 'NF==3 {print tolower($1)}')
# Wired devices: anything on the LAN that isn't on Wi-Fi. Traffic counted with one nftables counter per address.
nft list table inet homeenergy >/dev/null 2>&1 || nft -f - <<'EOF'
table inet homeenergy {
  chain traffic { type filter hook forward priority -5; policy accept; }
}
EOF
[ "$(nft list chain inet homeenergy traffic 2>/dev/null | grep -c counter)" -gt 200 ] && nft flush chain inet homeenergy traffic
RULES=$(nft list chain inet homeenergy traffic 2>/dev/null)
WIRED=""
for L in $(ip -4 neigh show dev br-lan 2>/dev/null | awk '$2=="lladdr" && $4!="FAILED" && $4!="INCOMPLETE" {print $1 "," tolower($3)}'); do
  IP=${L%,*}; MAC=${L#*,}
  echo "$WIFI" | grep -qx "$MAC" && continue
  if ! echo "$RULES" | grep -q "ip daddr $IP counter"; then
    nft add rule inet homeenergy traffic ip daddr "$IP" counter
    nft add rule inet homeenergy traffic ip saddr "$IP" counter
  fi
  WIRED="$WIRED$MAC $IP
"
done
RULES=$(nft list chain inet homeenergy traffic 2>/dev/null)
OUT="$OUT$(echo "$WIRED" | while read MAC IP; do
  [ -n "$IP" ] || continue
  B=$(echo "$RULES" | awk -v ip="$IP" '$1=="ip" && $3==ip {for (i=1;i<NF;i++) if ($i=="bytes") s+=$(i+1)} END {print s+0}')
  echo "$MAC $B 0 w"
done)
"
JSON=$(echo "$OUT" | awk -v leases="$LEASES" '
  BEGIN { while ((getline l < leases) > 0) { split(l, f, " "); name[tolower(f[2])] = f[4]; ip[tolower(f[2])] = f[3] } }
  NF >= 3 { m = tolower($1); n = (m in name) ? name[m] : ""; if (n == "*") n = "";
            gsub(/"/, "", n); printf "%s{\"mac\":\"%s\",\"name\":\"%s\",\"ip\":\"%s\",\"rx\":%s,\"tx\":%s,\"wired\":%s}", sep, m, n, ip[m], $2, $3, ($4=="w" ? "true" : "false"); sep = "," }')
OFFLOAD=$(uci -q get firewall.@defaults[0].flow_offloading || echo 0)
UP=$(cut -d. -f1 /proc/uptime)
uclient-fetch -q -T 20 -O /dev/null --post-data="{\"uptime\":$UP,\"offload\":\"$OFFLOAD\",\"clients\":[$JSON]}" "$URL" 2>/dev/null
"""


def script(public_url: str, token: str) -> str:
    return SCRIPT.replace("__URL__", f"{public_url}/hook/router?token={token}")
