"""Outdoor temperature from Open-Meteo (free, no key). Used to learn how fast the flat warms up."""
import time

import requests

from .config import LAT, LON

_cache = {"ts": 0.0, "data": None}


def current() -> dict | None:
    if _cache["data"] and time.time() - _cache["ts"] < 600:
        return _cache["data"]
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": LAT, "longitude": LON,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "hourly": "temperature_2m", "forecast_hours": 24,
            "timezone": "Europe/London",
        },
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    data = {
        "temp": j["current"]["temperature_2m"],
        "wind": j["current"].get("wind_speed_10m"),
        "code": j["current"].get("weather_code"),
        "overnight_low": min(j["hourly"]["temperature_2m"]) if j.get("hourly") else None,
    }
    _cache.update(ts=time.time(), data=data)
    return data
