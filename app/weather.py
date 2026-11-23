"""Weather for Tiptree from Open-Meteo (free, no key), using the Met Office's own UK model (UKV, 2 km grid)
for the best local forecast, with Open-Meteo's blend as a fallback.

Beyond temperature it collects the things that change how fast the flat loses or gains heat:
  - wind speed/direction/gusts  - wind strips heat from walls and windows (the biggest factor after temperature)
  - solar radiation and cloud   - sunshine through windows heats the flat for free
  - humidity, rain              - damp air feels colder; wet walls lose heat faster
  - "feels like" temperature
These are recorded with every reading so the smart heating can learn their effect on your flat.
"""
import time

import requests

from .config import LAT, LON

_cache = {"ts": 0.0, "data": None}
FIELDS = ("temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,wind_direction_10m,"
          "wind_gusts_10m,shortwave_radiation,cloud_cover,precipitation,weather_code")
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _fetch(model: str | None):
    params = {"latitude": LAT, "longitude": LON, "current": FIELDS, "hourly": FIELDS,
              "forecast_hours": 48, "timezone": "Europe/London", "wind_speed_unit": "mph"}
    if model:
        params["models"] = model
    r = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("current", {}).get("temperature_2m") is None:
        raise ValueError("no data from this model")
    return j


def current() -> dict | None:
    if _cache["data"] and time.time() - _cache["ts"] < 600:
        return _cache["data"]
    try:
        j, source = _fetch("ukmo_seamless"), "Met Office UK model"
    except Exception:
        j, source = _fetch(None), "Open-Meteo blend"
    c, h = j["current"], j.get("hourly", {})
    temps = [t for t in h.get("temperature_2m", [])[:24] if t is not None]
    wd = c.get("wind_direction_10m")
    data = {
        "temp": c["temperature_2m"],
        "feels_like": c.get("apparent_temperature"),
        "humidity": c.get("relative_humidity_2m"),
        "wind": c.get("wind_speed_10m"),                     # mph
        "gusts": c.get("wind_gusts_10m"),
        "wind_dir": COMPASS[round(wd / 45) % 8] if wd is not None else None,
        "wind_deg": wd,
        "solar": c.get("shortwave_radiation"),               # W/m² of sunshine
        "cloud": c.get("cloud_cover"),                       # %
        "rain": c.get("precipitation"),                      # mm in the last hour
        "code": c.get("weather_code"),
        "overnight_low": min(temps) if temps else None,
        "source": source,
        "hourly": [
            {"time": t, "temp": h["temperature_2m"][i], "wind": h["wind_speed_10m"][i],
             "solar": h["shortwave_radiation"][i], "cloud": h["cloud_cover"][i], "rain": h["precipitation"][i]}
            for i, t in enumerate(h.get("time", [])[:48])
        ],
    }
    _cache.update(ts=time.time(), data=data)
    return data


def reading_extras(w: dict) -> dict:
    """The external factors saved with each 5-minute reading."""
    return {k: w.get(k) for k in ("feels_like", "humidity", "wind", "gusts", "wind_deg", "solar", "cloud", "rain")}
