"""AWEKAS community weather station observations (ground truth).

AWEKAS (Automatisches Wetter- und Erstellungssystem) is an Austrian weather
station network with ~2400+ community stations.  Data is accessed via the
map API at app.awekas.at — no API key required, but a session UUID must be
obtained first.

The plot endpoint provides hourly time-series for the last ~48 hours.
Hourly entries are returned directly; daily aggregates are derived from
the hourly data.

Observations are immutable, so the poller only stores forecast_times not yet
in the archive (see `is_observation` and poller._poll_source).

provider_location_id is the numeric AWEKAS station id, e.g. '52847'.
"""
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .base import ForecastEntry, LocationResult, WeatherProvider

logger = logging.getLogger(__name__)

_API_BASE = 'https://app.awekas.at/map/v2/api'
_GEOCODE_URL = 'https://mapproxy.awekas.at/search'
_HEADERS = {'User-Agent': 'bether-weather-archive (personal, single daily poll)'}

_COMPASS = ['N', 'NNO', 'NO', 'ONO', 'O', 'OSO', 'SO', 'SSO',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']

# Sensors to fetch from the plot endpoint
_SENSORS = ('temp', 'hum', 'baro', 'wind', 'gust', 'rain', 'rate',
            'solar', 'dew', 'snow')

# Cache the station list for 6 hours
_STATION_CACHE_TTL = 6 * 3600
_station_cache: dict = {'ts': 0.0, 'stations': []}

# Session UUID cache — the JWT has an extremely long expiry
_session_cache: dict = {'ts': 0.0, 'uuid': ''}

# Persistent cache for station names (loaded from disk, lazily populated)
_NAMES_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'awekas_names.json')
_names_cache: Optional[dict] = None


def _load_names_cache() -> dict:
    global _names_cache
    if _names_cache is not None:
        return _names_cache
    try:
        with open(_NAMES_CACHE_PATH) as f:
            _names_cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _names_cache = {}
    return _names_cache


def _save_names_cache():
    if _names_cache is None:
        return
    try:
        with open(_NAMES_CACHE_PATH, 'w') as f:
            json.dump(_names_cache, f)
    except OSError as e:
        logger.warning('Failed to save AWEKAS names cache: %s', e)


def _resolve_name(station_id: str) -> str:
    """Get station name from cache or API. Returns 'Station {id}' on failure."""
    cache = _load_names_cache()
    if station_id in cache:
        return cache[station_id]
    detail = _fetch_station_detail(station_id)
    name = (detail or {}).get('name') or f"Station {station_id}"
    cache[station_id] = name
    _save_names_cache()
    return name


def _get_session() -> str:
    """Get or refresh the AWEKAS session UUID (JWT)."""
    if _session_cache['uuid'] and time.time() - _session_cache['ts'] < 86400:
        return _session_cache['uuid']
    resp = requests.get(f'{_API_BASE}/session.php', headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json().get('data', {})
    uuid = data.get('uuid', '')
    if not uuid:
        raise RuntimeError('AWEKAS session failed: no UUID returned')
    _session_cache['uuid'] = uuid
    _session_cache['ts'] = time.time()
    return uuid


def _api_get(endpoint: str, params: dict) -> dict:
    """Authenticated GET against the AWEKAS map API."""
    uuid = _get_session()
    headers = {**_HEADERS, 'Authentication': uuid}
    resp = requests.get(f'{_API_BASE}/{endpoint}', params=params,
                        headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.sin((rlat2 - rlat1) / 2) ** 2 +
         math.cos(rlat1) * math.cos(rlat2) * math.sin((rlon2 - rlon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _compass_mean(degs: list[float]) -> Optional[str]:
    """Compass point of the vector-averaged wind direction."""
    if not degs:
        return None
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    if x == 0 and y == 0:
        return None
    deg = math.degrees(math.atan2(y, x)) % 360
    return _COMPASS[round(deg / 22.5) % 16]


def _load_stations() -> list[dict]:
    """All AWEKAS stations from a large bounding box (cached in-process).

    The mappoints endpoint returns {id, lat, lon} for each station.
    Station names are NOT included and must be fetched individually via
    station.php.
    """
    if _station_cache['stations'] and time.time() - _station_cache['ts'] < _STATION_CACHE_TTL:
        return _station_cache['stations']

    # Cover all of Austria + South Tyrol / Bavaria border region
    params = {'p': 'temp', 't': 'temp',
              'n': 49.0, 'e': 17.5, 's': 45.5, 'w': 9.0, 'plt': 'web'}
    data = _api_get('mappoints.php', params)
    stations = []
    for pt in (data.get('data') or []):
        sid = pt.get('id')
        if not sid:
            continue
        stations.append({
            'id': str(sid),
            'latitude': float(pt['lat']),
            'longitude': float(pt['lon']),
        })

    _station_cache['stations'] = stations
    _station_cache['ts'] = time.time()
    logger.info('Loaded %d AWEKAS stations', len(stations))
    return stations


def _fetch_station_detail(station_id: str) -> Optional[dict]:
    """Fetch station metadata (name, altitude, country, sensors) from the API."""
    try:
        data = _api_get('station.php',
                        {'id': station_id, 'plt': 'web', 't': 'temp'})
    except Exception as e:
        logger.warning('AWEKAS station %s detail fetch failed: %s', station_id, e)
        return None
    d = data.get('data')
    if not d or not isinstance(d, dict):
        return None
    return d


class AwekasProvider(WeatherProvider):
    name = 'awekas'
    display_name = 'AWEKAS'
    supports_daily = True
    supports_hourly = True
    is_observation = True   # measured values: immutable, poller dedups by forecast_time

    def search(self, query: str) -> list[LocationResult]:
        q = query.strip().lower()
        if not q:
            return []

        # If query looks like a station ID, try direct lookup first
        if q.isdigit():
            stations = _load_stations()
            for s in stations:
                if q == s['id']:
                    detail = _fetch_station_detail(s['id'])
                    return [self._to_result(s, detail)]

        # Try geocoding the query to get coordinates
        try:
            resp = requests.get(_GEOCODE_URL, params={'q': query.strip()},
                                headers={**_HEADERS, 'Accept': 'application/json'},
                                timeout=10)
            resp.raise_for_status()
            features = resp.json()
        except Exception:
            features = []

        if features:
            # Use first geocoding result — find nearby stations
            lat = float(features[0]['lat'])
            lon = float(features[0]['lon'])
            nearby = self.nearest_stations(lat, lon, limit=10)
            return [self._to_result(s, _fetch_station_detail(s['id']))
                    for s in nearby]

        return []

    def nearest_stations(self, lat: float, lon: float, limit: int = 5) -> list[dict]:
        stations = sorted(
            _load_stations(),
            key=lambda s: _distance_km(lat, lon, s['latitude'], s['longitude']),
        )[:limit]
        out = []
        for s in stations:
            detail = _fetch_station_detail(s['id'])
            d = dict(s)
            d['distance_km'] = round(_distance_km(lat, lon, s['latitude'], s['longitude']), 2)
            d['name'] = (detail or {}).get('name', f"Station {s['id']}")
            out.append(d)
        return out

    @staticmethod
    def all_stations() -> list[dict]:
        """Return the full list of AWEKAS stations.

        Names are resolved from a persistent on-disk cache.  Missing names
        are fetched from the AWEKAS station API in a background thread so
        the first call returns quickly while subsequent calls (and the
        import flow) get proper names.
        """
        cache = _load_names_cache()
        stations = _load_stations()

        # Identify stations whose names still need resolving
        missing = [s for s in stations if s['id'] not in cache]

        def _fetch_missing():
            for s in missing:
                _resolve_name(s['id'])
                time.sleep(0.15)  # gentle rate-limit

        if missing:
            import threading
            threading.Thread(target=_fetch_missing, daemon=True).start()

        return [{'id': s['id'], 'name': cache.get(s['id'], f"Station {s['id']}"),
                 'state': '', 'latitude': s['latitude'],
                 'longitude': s['longitude'], 'height': None}
                for s in stations]

    def _to_result(self, s: dict, detail: Optional[dict] = None) -> LocationResult:
        name = (detail or {}).get('name', f"Station {s['id']}")
        country = (detail or {}).get('country', '')
        altitude = (detail or {}).get('altitude')
        return LocationResult(
            name=name,
            provider_location_id=s['id'],
            latitude=s['latitude'],
            longitude=s['longitude'],
            extra={'title': f"{name} ({country})" if country else name,
                   'station_name': name, 'country': country,
                   'height': altitude},
        )

    def resolve_station_name(self, station_id: str) -> str:
        """Get the real station name (from cache or API)."""
        return _resolve_name(station_id)

    # ── data ──────────────────────────────────────────────────────────────────

    def fetch_all(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        # Fetch all sensor plot data and derive both hourly and daily.
        hourly = self._fetch_all_hourly(provider_location_id, extra.get('_relevant_dates'))
        daily = self._derive_daily(hourly, extra.get('_relevant_dates'))
        return hourly + daily

    def fetch_daily(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        hourly = self._fetch_all_hourly(provider_location_id, extra.get('_relevant_dates'))
        return self._derive_daily(hourly, extra.get('_relevant_dates'))

    def fetch_hourly(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        return self._fetch_all_hourly(provider_location_id, extra.get('_relevant_dates'))

    def _fetch_all_hourly(self, station_id: str,
                          relevant: Optional[set]) -> list[ForecastEntry]:
        """Fetch all sensor plot data and combine into hourly entries."""
        tz_name = 'Europe/Berlin'  # default; overridden by station timezone
        all_series: dict[str, dict[datetime, float]] = {}

        for sensor in _SENSORS:
            series, station_tz = self._fetch_plot(station_id, sensor)
            if station_tz:
                tz_name = station_tz
            all_series[sensor] = series

        tz = ZoneInfo(tz_name)
        # Collect all timestamps across all sensors
        all_hours: set[datetime] = set()
        for series in all_series.values():
            all_hours.update(series.keys())

        if not all_hours:
            return []

        # Filter to relevant dates
        if relevant is not None:
            all_hours = {h for h in all_hours
                         if h.astimezone(tz).date().isoformat() in relevant}

        entries = []
        for hour in sorted(all_hours):
            vals: dict = {}
            ts = all_series.get('temp', {})
            if hour in ts:
                vals['temperature'] = round(ts[hour], 1)
            ts = all_series.get('hum', {})
            if hour in ts:
                vals['humidity'] = round(ts[hour])
            ts = all_series.get('baro', {})
            if hour in ts:
                vals['pressure'] = round(ts[hour], 1)
            ts = all_series.get('wind', {})
            if hour in ts:
                vals['wind_speed'] = round(ts[hour] * 3.6)
            ts = all_series.get('gust', {})
            if hour in ts:
                pass  # gust not in ForecastEntry
            ts = all_series.get('rain', {})
            if hour in ts:
                vals['precip_amount'] = round(ts[hour], 1)
            ts = all_series.get('solar', {})
            if hour in ts:
                pass  # solar not in ForecastEntry
            ts = all_series.get('dew', {})
            if hour in ts:
                pass  # dew not in ForecastEntry

            if any(v is not None for v in vals.values()):
                entries.append(ForecastEntry(
                    forecast_time=hour.astimezone(timezone.utc)
                    .replace(tzinfo=None).isoformat(),
                    granularity='hourly',
                    **vals,
                ))
        return entries

    def _fetch_plot(self, station_id: str, sensor: str
                    ) -> tuple[dict[datetime, float], Optional[str]]:
        """Fetch one sensor's hourly plot data. Returns {utc_hour: value} and
        the station's timezone name."""
        try:
            data = _api_get('plot.php',
                            {'id': station_id, 'sensor': sensor, 'plt': 'web'})
        except Exception as e:
            logger.warning('AWEKAS plot %s/%s failed: %s', station_id, sensor, e)
            return {}, None

        d = data.get('data')
        if not d or not isinstance(d, dict):
            return {}, None

        station_tz = d.get('timezone')
        x_vals = d.get('x', [])
        y_vals = d.get('y', [])

        series: dict[datetime, float] = {}
        for ts_ms, val in zip(x_vals, y_vals):
            if val is None:
                continue
            try:
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                hour = dt.replace(minute=0, second=0, microsecond=0)
                series[hour] = float(val)
            except (TypeError, ValueError, OSError):
                continue
        return series, station_tz

    def _derive_daily(self, hourly: list[ForecastEntry],
                      relevant: Optional[set]) -> list[ForecastEntry]:
        """Derive daily aggregates from hourly entries."""
        today = datetime.now(timezone.utc).date()
        by_date: dict[str, dict] = {}
        for e in hourly:
            try:
                d = datetime.fromisoformat(e.forecast_time).date()
            except (ValueError, TypeError):
                continue
            if d >= today:
                continue
            key = d.isoformat()
            if relevant is not None and key not in relevant:
                continue
            if key not in by_date:
                by_date[key] = {'temps': [], 'humids': [], 'winds': [],
                                'precips': [], 'pressures': []}
            bucket = by_date[key]
            if e.temperature is not None:
                bucket['temps'].append(e.temperature)
            if e.humidity is not None:
                bucket['humids'].append(e.humidity)
            if e.wind_speed is not None:
                bucket['winds'].append(e.wind_speed)
            if e.precip_amount is not None:
                bucket['precips'].append(e.precip_amount)
            if e.pressure is not None:
                bucket['pressures'].append(e.pressure)

        entries = []
        for d_key in sorted(by_date):
            b = by_date[d_key]
            temps = b['temps']
            hums = b['humids']
            winds = b['winds']
            precips = b['precips']
            press = b['pressures']
            e = ForecastEntry(
                forecast_time=f'{d_key}T00:00:00',
                granularity='daily',
                temperature=round(sum(temps) / len(temps), 1) if temps else None,
                temp_max=round(max(temps), 1) if temps else None,
                temp_min=round(min(temps), 1) if temps else None,
                humidity=round(sum(hums) / len(hums)) if hums else None,
                wind_speed=round(sum(winds) / len(winds)) if winds else None,
                precip_amount=round(sum(precips), 1) if precips else None,
                pressure=round(sum(press) / len(press), 1) if press else None,
            )
            if any(v is not None for v in (e.temperature, e.temp_max, e.temp_min,
                                           e.humidity, e.wind_speed,
                                           e.precip_amount, e.pressure)):
                entries.append(e)
        return entries
