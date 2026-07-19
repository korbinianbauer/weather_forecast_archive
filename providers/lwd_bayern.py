"""LWD Bayern (Lawinenwarndienst Bayern) station observations (ground truth).

Reads the public la-dok API behind lawinenwarndienst.bayern.de: the station
list plus per-station 10-minute measurements of the last ~2.5 days
(air temperature TL, snow surface temperature TO, snow height HS, wind
dd/ff/ffBoe). The 10-minute samples are aggregated to hourly and daily
entries; timestamps in the feed are local Europe/Berlin time.

Observations are immutable, so the poller only stores forecast_times not yet
in the archive (see `is_observation` and poller._poll_source).

provider_location_id is the numeric la-dok stationId, e.g. '15'.
"""
import logging
import math
import re
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .base import ForecastEntry, LocationResult, WeatherProvider

logger = logging.getLogger(__name__)

_API = 'https://api-la-dok.bayern.de'
_HEADERS = {'User-Agent': 'bether-weather-archive (personal, single daily poll)'}
_TZ = ZoneInfo('Europe/Berlin')

_COMPASS = ['N', 'NNO', 'NO', 'ONO', 'O', 'OSO', 'SO', 'SSO',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']

# A full day has 144 ten-minute samples; require most of them before storing
# an (immutable) daily aggregate so truncated days don't yield wrong extremes.
_MIN_DAILY_SAMPLES = 120

_STATION_CACHE_TTL = 6 * 3600
_station_cache: dict = {'ts': 0.0, 'stations': []}


def _parse_height(altitude: str) -> Optional[int]:
    """First number from strings like '1625/1575 m ü. NN'."""
    m = re.search(r'\d+', altitude or '')
    return int(m.group()) if m else None


def _load_stations() -> list[dict]:
    """Active LWD Bayern stations from the la-dok API (cached in-process)."""
    if _station_cache['stations'] and time.time() - _station_cache['ts'] < _STATION_CACHE_TTL:
        return _station_cache['stations']

    resp = requests.get(f'{_API}/public/weatherstations/all',
                        headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    stations = []
    for s in resp.json():
        pos = s.get('position') or {}
        if not s.get('stationId') or pos.get('lat') is None or pos.get('lng') is None:
            continue
        stations.append({
            'id': str(s['stationId']),
            'name': s.get('name', ''),
            'state': s.get('regionName') or s.get('oldRegionName') or 'Bayern',
            'latitude': float(pos['lat']),
            'longitude': float(pos['lng']),
            'height': _parse_height(s.get('altitude', '')),
        })

    stations.sort(key=lambda s: s['name'])
    _station_cache['stations'] = stations
    _station_cache['ts'] = time.time()
    logger.info('Loaded %d LWD Bayern stations', len(stations))
    return stations


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


def _fval(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
        return float(v)
    return None


def _mean(vals: list[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


class LwdBayernProvider(WeatherProvider):
    name = 'lwd_bayern'
    display_name = 'LWD Bayern'
    supports_daily = True
    supports_hourly = True
    is_observation = True   # measured values: immutable, poller dedups by forecast_time

    def search(self, query: str) -> list[LocationResult]:
        q = query.strip().lower()
        results = []
        for s in _load_stations():
            if q in s['name'].lower():
                results.append(self._to_result(s))
            if len(results) >= 10:
                break
        return results

    def nearest_stations(self, lat: float, lon: float, limit: int = 5) -> list[dict]:
        stations = sorted(
            _load_stations(),
            key=lambda s: _distance_km(lat, lon, s['latitude'], s['longitude']),
        )[:limit]
        out = []
        for s in stations:
            d = dict(s)
            d['distance_km'] = round(_distance_km(lat, lon, s['latitude'], s['longitude']), 2)
            out.append(d)
        return out

    @staticmethod
    def all_stations() -> list[dict]:
        """Return the full list of LWD Bayern stations."""
        return _load_stations()

    def _to_result(self, s: dict) -> LocationResult:
        return LocationResult(
            name=s['name'],
            provider_location_id=s['id'],
            latitude=s['latitude'],
            longitude=s['longitude'],
            extra={'title': f"{s['name']} ({s['state']})",
                   'station_name': s['name'], 'state': s['state'],
                   'height': s['height']},
        )

    # ── data ──────────────────────────────────────────────────────────────────

    def _fetch_samples(self, provider_location_id: str) -> list[dict]:
        """10-minute samples with parsed local timestamp under '_dt'."""
        resp = requests.get(f'{_API}/public/weatherWeb/{provider_location_id}',
                            headers=_HEADERS, timeout=60)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        samples = []
        for row in resp.json():
            ts = row.get('TS', '')
            try:
                row['_dt'] = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                continue
            samples.append(row)
        samples.sort(key=lambda r: r['_dt'])
        return samples

    def fetch_all(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        # Hourly and daily aggregates come from the same 10-minute feed —
        # fetch it once and derive both.
        samples = self._fetch_samples(provider_location_id)
        if not samples:
            return []
        relevant = extra.get('_relevant_dates')
        return (self._hourly_entries(samples, relevant)
                + self._daily_entries(samples, relevant))

    def fetch_daily(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        samples = self._fetch_samples(provider_location_id)
        return self._daily_entries(samples, extra.get('_relevant_dates'))

    def fetch_hourly(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        samples = self._fetch_samples(provider_location_id)
        return self._hourly_entries(samples, extra.get('_relevant_dates'))

    def _hourly_entries(self, samples: list[dict],
                        relevant: Optional[set]) -> list[ForecastEntry]:
        latest = max(s['_dt'] for s in samples)
        by_hour: dict[datetime, list[dict]] = {}
        for s in samples:
            hour = s['_dt'].replace(minute=0, second=0)
            # Observations are archived once — only emit fully elapsed hours.
            if hour + timedelta(hours=1) > latest:
                continue
            if relevant is not None and hour.date().isoformat() not in relevant:
                continue
            by_hour.setdefault(hour, []).append(s)

        entries = []
        for hour in sorted(by_hour):
            rows = by_hour[hour]
            temps = [v for r in rows if (v := _fval(r, 'TL')) is not None]
            winds = [v for r in rows if (v := _fval(r, 'ff')) is not None]
            dirs  = [v for r in rows if (v := _fval(r, 'dd')) is not None]
            temp = _mean(temps)
            wind = _mean(winds)
            e = ForecastEntry(
                forecast_time=hour.isoformat(),
                granularity='hourly',
                temperature=round(temp, 1) if temp is not None else None,
                wind_speed=round(wind * 3.6) if wind is not None else None,
                wind_direction=_compass_mean(dirs),
            )
            if e.temperature is not None or e.wind_speed is not None:
                entries.append(e)
        return entries

    def _daily_entries(self, samples: list[dict],
                       relevant: Optional[set]) -> list[ForecastEntry]:
        today = datetime.now(_TZ).date()
        by_day: dict[date, list[dict]] = {}
        for s in samples:
            d = s['_dt'].date()
            if d >= today:            # only fully elapsed days (entries are immutable)
                continue
            if relevant is not None and d.isoformat() not in relevant:
                continue
            by_day.setdefault(d, []).append(s)

        entries = []
        for d in sorted(by_day):
            rows = by_day[d]
            if len(rows) < _MIN_DAILY_SAMPLES:  # truncated day at the feed edge
                continue
            temps = [v for r in rows if (v := _fval(r, 'TL')) is not None]
            winds = [v for r in rows if (v := _fval(r, 'ff')) is not None]
            temp = _mean(temps)
            wind = _mean(winds)
            e = ForecastEntry(
                forecast_time=f'{d.isoformat()}T00:00:00',
                granularity='daily',
                temperature=round(temp, 1) if temp is not None else None,
                temp_max=round(max(temps), 1) if temps else None,
                temp_min=round(min(temps), 1) if temps else None,
                wind_speed=round(wind * 3.6) if wind is not None else None,
            )
            if e.temperature is not None or e.wind_speed is not None:
                entries.append(e)
        return entries
