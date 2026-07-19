"""Weather Underground personal weather station observations (ground truth).

Wunderground (wunderground.com, owned by The Weather Company / IBM) hosts a
huge worldwide network of personal weather stations (PWS).  Data is served by
api.weather.com using the public API key embedded in the wunderground.com
website — no account required.

Endpoints used:
  - v3/location/search   — geocode a free-text query
  - v3/location/near     — nearest PWS around a coordinate (product=pws)
  - v2/pws/observations/current — latest observation (station lookup / metadata)
  - v2/pws/history/hourly — hourly summaries for one local date
  - v2/pws/history/daily  — one summary row for one local date

History is available for years back, so any missing past date can be
backfilled.  Observations are immutable: the poller passes the needed dates
via `_relevant_dates` and dedups by `(granularity, forecast_time)`.

provider_location_id is the PWS station id, e.g. 'IMUNIC508'.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from .base import ForecastEntry, LocationResult, WeatherProvider

logger = logging.getLogger(__name__)

_API_KEY = 'e1f10a1e78da46f5b10a1e78da96f525'  # public key from wunderground.com
_API_BASE = 'https://api.weather.com'
_HEADERS = {'User-Agent': 'bether-weather-archive (personal, single daily poll)'}

# Station IDs look like 'IMUNIC508', 'KMAHANOV10' — uppercase letters + digits
_STATION_ID_RE = re.compile(r'^[A-Z][A-Z0-9]{4,}$')

_COMPASS = ['N', 'NNO', 'NO', 'ONO', 'O', 'OSO', 'SO', 'SSO',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']

# Cap history backfill so one poll never fires hundreds of requests
# (2 requests per date: hourly + daily).
_MAX_HISTORY_DATES = 30

# Cache the station list for the map import UI for 6 hours in-process,
# persisted to disk so restarts don't refetch (~1 request per location cell).
_STATION_CACHE_TTL = 6 * 3600
_STATIONS_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'wunderground_stations.json')
_station_cache: dict = {'ts': 0.0, 'stations': [], 'loaded': False}
_station_cache_lock = threading.Lock()
_station_fetch_running = False


def _load_station_cache():
    """Populate the in-process cache from disk once per process."""
    if _station_cache['loaded']:
        return
    _station_cache['loaded'] = True
    try:
        with open(_STATIONS_CACHE_PATH) as f:
            data = json.load(f)
        _station_cache['stations'] = data.get('stations') or []
        _station_cache['ts'] = float(data.get('ts') or 0.0)
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        pass


def _save_station_cache():
    try:
        with open(_STATIONS_CACHE_PATH, 'w') as f:
            json.dump({'ts': _station_cache['ts'],
                       'stations': _station_cache['stations']}, f)
    except OSError as e:
        logger.warning('Failed to save Wunderground station cache: %s', e)


def _api_get(path: str, params: dict) -> Optional[dict]:
    """GET against api.weather.com.  Returns None on 204 (no data)."""
    resp = requests.get(f'{_API_BASE}/{path}',
                        params={**params, 'format': 'json', 'apiKey': _API_KEY},
                        headers=_HEADERS, timeout=30)
    if resp.status_code == 204 or not resp.content:
        return None
    resp.raise_for_status()
    return resp.json()


def _compass(deg) -> Optional[str]:
    if deg is None:
        return None
    try:
        return _COMPASS[round(float(deg) / 22.5) % 16]
    except (TypeError, ValueError):
        return None


def _num(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _mid_pressure(metric: dict) -> Optional[float]:
    hi, lo = _num(metric.get('pressureMax')), _num(metric.get('pressureMin'))
    if hi is None or lo is None:
        return hi if lo is None else lo
    return round((hi + lo) / 2, 1)


def _near_stations(lat: float, lon: float) -> list[dict]:
    """Nearest PWS around a coordinate via v3/location/near (up to ~10)."""
    data = _api_get('v3/location/near',
                    {'geocode': f'{lat:.3f},{lon:.3f}', 'product': 'pws'})
    loc = (data or {}).get('location') or {}
    ids = loc.get('stationId') or []
    stations = []
    for i, sid in enumerate(ids):
        if not sid:
            continue
        try:
            qc = (loc.get('qcStatus') or [])[i]
        except IndexError:
            qc = None
        if qc == -1:  # offline / rejected station
            continue

        def _col(key):
            vals = loc.get(key) or []
            return vals[i] if i < len(vals) else None

        stations.append({
            'id': str(sid),
            'name': f"{_col('stationName') or 'PWS'} ({sid})",
            'latitude': _num(_col('latitude')),
            'longitude': _num(_col('longitude')),
            'distance_km': _num(_col('distanceKm')),
        })
    return stations


class WundergroundProvider(WeatherProvider):
    name = 'wunderground'
    display_name = 'Wunderground'
    supports_daily = True
    supports_hourly = True
    is_observation = True   # measured values: immutable, poller dedups by forecast_time

    # ── search / station discovery ────────────────────────────────────────────

    def search(self, query: str) -> list[LocationResult]:
        q = query.strip()
        if not q:
            return []

        # Direct station-ID lookup, e.g. 'IMUNIC508'
        if _STATION_ID_RE.match(q.upper()):
            result = self._lookup_station(q.upper())
            if result:
                return [result]

        # Geocode the query, then list nearby stations
        try:
            data = _api_get('v3/location/search',
                            {'query': q, 'language': 'de-DE'})
        except Exception as e:
            logger.warning('Wunderground location search %r failed: %s', q, e)
            return []
        loc = (data or {}).get('location') or {}
        lats = loc.get('latitude') or []
        lons = loc.get('longitude') or []
        if not lats or not lons:
            return []

        try:
            nearby = self.nearest_stations(float(lats[0]), float(lons[0]), limit=8)
        except Exception as e:
            logger.warning('Wunderground near lookup failed: %s', e)
            return []
        return [
            LocationResult(
                name=s['name'],
                provider_location_id=s['id'],
                latitude=s['latitude'],
                longitude=s['longitude'],
                extra={'title': f"{s['name']} · {s['distance_km']:.1f} km",
                       'station_name': s['name'],
                       'height': s.get('height'),
                       'distance_km': s['distance_km']},
            )
            for s in nearby
        ]

    def _lookup_station(self, station_id: str) -> Optional[LocationResult]:
        """Resolve one station via its latest observation."""
        try:
            data = _api_get('v2/pws/observations/current',
                            {'stationId': station_id, 'units': 'm'})
        except Exception:
            return None
        obs = ((data or {}).get('observations') or [None])[0]
        if not obs:
            return None
        neighborhood = obs.get('neighborhood') or station_id
        name = f'{neighborhood} ({station_id})'
        elev = _num((obs.get('metric') or {}).get('elev'))
        return LocationResult(
            name=name,
            provider_location_id=station_id,
            latitude=_num(obs.get('lat')),
            longitude=_num(obs.get('lon')),
            extra={'title': name, 'station_name': name,
                   'country': obs.get('country', ''), 'height': elev},
        )

    def nearest_stations(self, lat: float, lon: float, limit: int = 5) -> list[dict]:
        stations = _near_stations(lat, lon)[:limit]
        for s in stations:
            s.setdefault('state', '')
            s.setdefault('height', None)
        return stations

    @staticmethod
    def all_stations() -> list[dict]:
        """Stations for the map-based import UI.

        Wunderground has no bulk station list, so aggregate the nearest
        stations around every tracked location.  Locations are deduplicated
        onto a ~0.1° grid to keep the request count low, and the fetch runs
        in a background thread — the call returns the (possibly still empty)
        cached list immediately, like the AWEKAS name resolution.
        """
        global _station_fetch_running
        _load_station_cache()
        fresh = time.time() - _station_cache['ts'] < _STATION_CACHE_TTL
        if not (_station_cache['stations'] and fresh):
            with _station_cache_lock:
                if not _station_fetch_running:
                    _station_fetch_running = True
                    threading.Thread(
                        target=WundergroundProvider._refresh_station_cache,
                        daemon=True).start()
        return _station_cache['stations']

    @staticmethod
    def _refresh_station_cache():
        global _station_fetch_running
        try:
            import db  # local import: db must not be a hard dependency of providers
            cells = {}
            for loc in db.get_locations(show_hidden=True):
                try:
                    key = (round(loc['latitude'], 1), round(loc['longitude'], 1))
                except (TypeError, ValueError):
                    continue
                cells.setdefault(key, (loc['latitude'], loc['longitude']))

            seen: dict[str, dict] = {}
            for lat, lon in cells.values():
                try:
                    nearby = _near_stations(lat, lon)
                except Exception as e:
                    logger.warning('Wunderground near lookup %.2f,%.2f failed: %s',
                                   lat, lon, e)
                    continue
                for s in nearby:
                    seen.setdefault(s['id'], {
                        'id': s['id'], 'name': s['name'], 'state': '',
                        'latitude': s['latitude'], 'longitude': s['longitude'],
                        'height': None,
                    })
                time.sleep(0.2)

            _station_cache['stations'] = list(seen.values())
            _station_cache['ts'] = time.time()
            _save_station_cache()
            logger.info('Loaded %d Wunderground stations near tracked locations',
                        len(seen))
        finally:
            _station_fetch_running = False

    # ── data ──────────────────────────────────────────────────────────────────

    def fetch_all(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        relevant = extra.get('_relevant_dates')
        dates = self._dates_to_fetch(relevant)
        entries: list[ForecastEntry] = []
        for d in dates:
            try:
                entries.extend(self._fetch_hourly_date(provider_location_id, d))
                daily = self._fetch_daily_date(provider_location_id, d)
                if daily:
                    entries.append(daily)
            except Exception as e:
                logger.warning('Wunderground %s history %s failed: %s',
                               provider_location_id, d, e)
            time.sleep(0.3)
        return entries

    def fetch_daily(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        return [e for e in self.fetch_all(provider_location_id, extra)
                if e.granularity == 'daily']

    def fetch_hourly(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        return [e for e in self.fetch_all(provider_location_id, extra)
                if e.granularity == 'hourly']

    @staticmethod
    def _dates_to_fetch(relevant: Optional[set]) -> list[date]:
        """Past local dates to request, most recent first, capped.

        `_relevant_dates` may contain future forecast dates — history only
        exists for the past, so those are dropped. Today is included for
        hourly data (completed hours); the daily summary skips it.
        """
        today = date.today()
        if relevant is None:
            wanted = {today - timedelta(days=i) for i in range(7)}
        else:
            wanted = set()
            for iso in relevant:
                try:
                    d = date.fromisoformat(iso)
                except (TypeError, ValueError):
                    continue
                if d <= today:
                    wanted.add(d)
        return sorted(wanted, reverse=True)[:_MAX_HISTORY_DATES]

    def _fetch_hourly_date(self, station_id: str, d: date) -> list[ForecastEntry]:
        data = _api_get('v2/pws/history/hourly',
                        {'stationId': station_id, 'units': 'm',
                         'numericPrecision': 'decimal',
                         'date': d.strftime('%Y%m%d')})
        rows = (data or {}).get('observations') or []
        rows.sort(key=lambda r: r.get('epoch') or 0)

        now = datetime.now(timezone.utc)
        entries = []
        prev_total: Optional[float] = None
        for row in rows:
            metric = row.get('metric') or {}
            ts = row.get('obsTimeUtc')
            if not ts:
                continue
            try:
                obs_dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            except ValueError:
                continue
            hour = obs_dt.replace(minute=0, second=0, microsecond=0)

            # precipTotal accumulates since local midnight → diff per hour
            total = _num(metric.get('precipTotal'))
            precip = None
            if total is not None:
                precip = max(0.0, total - prev_total) if prev_total is not None else total
                prev_total = total

            # Skip the still-running current hour — its summary is incomplete
            # but would be deduped as immutable on the next poll.
            if hour + timedelta(hours=1) > now:
                continue

            temp = _num(metric.get('tempAvg'))
            hum = _num(row.get('humidityAvg'))
            wind = _num(metric.get('windspeedAvg'))
            vals = dict(
                temperature=round(temp, 1) if temp is not None else None,
                humidity=round(hum) if hum is not None else None,
                wind_speed=round(wind) if wind is not None else None,
                wind_direction=_compass(row.get('winddirAvg')),
                precip_amount=round(precip, 1) if precip is not None else None,
                pressure=_mid_pressure(metric),
            )
            if any(v is not None for v in vals.values()):
                entries.append(ForecastEntry(
                    forecast_time=hour.replace(tzinfo=None).isoformat(),
                    granularity='hourly',
                    **vals,
                ))
        return entries

    def _fetch_daily_date(self, station_id: str, d: date) -> Optional[ForecastEntry]:
        if d >= date.today():
            return None  # day still in progress
        data = _api_get('v2/pws/history/daily',
                        {'stationId': station_id, 'units': 'm',
                         'numericPrecision': 'decimal',
                         'date': d.strftime('%Y%m%d')})
        row = ((data or {}).get('observations') or [None])[0]
        if not row:
            return None
        metric = row.get('metric') or {}
        temp = _num(metric.get('tempAvg'))
        hum = _num(row.get('humidityAvg'))
        wind = _num(metric.get('windspeedAvg'))
        t_max = _num(metric.get('tempHigh'))
        t_min = _num(metric.get('tempLow'))
        precip = _num(metric.get('precipTotal'))
        entry = ForecastEntry(
            forecast_time=f'{d.isoformat()}T00:00:00',
            granularity='daily',
            temperature=round(temp, 1) if temp is not None else None,
            temp_max=round(t_max, 1) if t_max is not None else None,
            temp_min=round(t_min, 1) if t_min is not None else None,
            humidity=round(hum) if hum is not None else None,
            wind_speed=round(wind) if wind is not None else None,
            wind_direction=_compass(row.get('winddirAvg')),
            precip_amount=round(precip, 1) if precip is not None else None,
            pressure=_mid_pressure(metric),
        )
        if any(v is not None for v in (entry.temperature, entry.temp_max,
                                       entry.temp_min, entry.humidity,
                                       entry.wind_speed, entry.precip_amount,
                                       entry.pressure)):
            return entry
        return None
