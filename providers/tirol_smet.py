"""LWD Tirol and HD Tirol station observations (ground truth).

Both networks publish their stations through the Euregio avalanche.report
station list (linea.geojson, `operator` distinguishes the networks) and their
measurements as gzipped SMET week files on wiski.tirol.gv.at: 10/15-minute
samples in SI units (temperatures in Kelvin, RH as 0–1 fraction, wind in m/s,
precipitation in mm), timestamps in UTC. The samples are aggregated to hourly
and daily entries like in the lwd_bayern provider.

Observations are immutable, so the poller only stores forecast_times not yet
in the archive (see `is_observation` and poller._poll_source).

provider_location_id is the SMET shortName, e.g. 'NSGS1' (LWD) or a UUID (HD).
"""
import gzip
import logging
import math
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .base import ForecastEntry, LocationResult, WeatherProvider

logger = logging.getLogger(__name__)

_STATIONS_URL = 'https://static.avalanche.report/eaws_weather_stations/linea.geojson'
_SMET_URL = 'https://wiski.tirol.gv.at/lawine/grafiken/smet/woche/{short_name}.smet.gz'
_HEADERS = {'User-Agent': 'bether-weather-archive (personal, single daily poll)'}
_TZ = ZoneInfo('Europe/Vienna')

_COMPASS = ['N', 'NNO', 'NO', 'ONO', 'O', 'OSO', 'SO', 'SSO',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']

# Store an (immutable) daily aggregate only when most of the day's samples are
# present, so truncated days at the feed edge don't yield wrong extremes.
_MIN_DAILY_COVERAGE = 0.8

_STATION_CACHE_TTL = 6 * 3600
_station_cache: dict = {'ts': 0.0, 'stations': []}   # all ALBINA stations, all operators


def _region(props: dict) -> str:
    """'AT-07-22 Stubaier Alpen Mitte' → 'Stubaier Alpen Mitte'."""
    region = re.sub(r'^[A-Z]{2}-\d{2}(-[A-Z\d]{2})*\s*', '',
                    props.get('microRegionID') or '').strip()
    return region or 'Tirol'


def _load_all_stations() -> list[dict]:
    """All avalanche.report SMET stations (cached in-process, all operators)."""
    if _station_cache['stations'] and time.time() - _station_cache['ts'] < _STATION_CACHE_TTL:
        return _station_cache['stations']

    resp = requests.get(_STATIONS_URL, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    stations = []
    for f in resp.json().get('features', []):
        props = f.get('properties', {})
        coords = f.get('geometry', {}).get('coordinates') or []
        data_urls = props.get('dataURLs') or []
        if (props.get('dataProviderID') != 'ALBINA' or not data_urls
                or len(coords) < 2):
            continue
        # Most HD Tirol stations carry no shortName — the SMET file name in
        # dataURLs is the station id then ('…/woche/<id>.smet.gz').
        m = re.search(r'/([^/]+)\.smet(?:\.gz)?$', data_urls[0])
        short_name = props.get('shortName') or (m.group(1) if m else None)
        if not short_name:
            continue
        stations.append({
            'id': short_name,
            'name': props.get('name', ''),
            'state': _region(props),
            'operator': props.get('operator') or '',
            'latitude': float(coords[1]),
            'longitude': float(coords[0]),
            'height': round(coords[2]) if len(coords) > 2 and coords[2] is not None else None,
        })

    stations.sort(key=lambda s: s['name'])
    _station_cache['stations'] = stations
    _station_cache['ts'] = time.time()
    logger.info('Loaded %d avalanche.report SMET stations', len(stations))
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


def _mean(vals: list[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _parse_smet(text: str) -> list[dict]:
    """SMET → samples with parsed local timestamp under '_dt'.

    Values are converted to the archive's units: temperatures K → °C,
    RH fraction → %, wind stays m/s, PSUM stays mm."""
    fields: list[str] = []
    nodata = -777.0
    samples = []
    in_data = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith('[DATA]'):
            in_data = True
            continue
        if not in_data:
            if line.startswith('fields'):
                fields = line.split('=', 1)[1].split()
            elif line.startswith('nodata'):
                try:
                    nodata = float(line.split('=', 1)[1])
                except ValueError:
                    pass
            continue
        parts = line.split()
        if len(parts) != len(fields) or not fields:
            continue
        row: dict = {}
        for key, raw in zip(fields, parts):
            if key == 'timestamp':
                try:
                    dt = datetime.strptime(raw, '%Y-%m-%dT%H:%M:%SZ')
                except ValueError:
                    row = {}
                    break
                row['_dt'] = dt.replace(tzinfo=timezone.utc).astimezone(_TZ).replace(tzinfo=None)
            else:
                try:
                    v = float(raw)
                except ValueError:
                    continue
                if v == nodata:
                    continue
                if key in ('TA', 'TD', 'TSS', 'TSG'):
                    v -= 273.15
                elif key == 'RH':
                    v *= 100
                row[key] = v
        if row.get('_dt'):
            samples.append(row)
    samples.sort(key=lambda r: r['_dt'])
    return samples


def _samples_per_day(samples: list[dict]) -> float:
    """Expected sample count per day from the median sampling interval."""
    diffs = [(b['_dt'] - a['_dt']).total_seconds()
             for a, b in zip(samples, samples[1:])]
    diffs = [d for d in diffs if d > 0]
    if not diffs:
        return 144.0
    return 86400.0 / statistics.median(diffs)


class TirolSmetProvider(WeatherProvider):
    """Common base for the Tirol SMET station networks; subclasses pick the
    stations via `operator_prefix`."""
    supports_daily = True
    supports_hourly = True
    is_observation = True   # measured values: immutable, poller dedups by forecast_time

    operator_prefix: str

    def _stations(self) -> list[dict]:
        return [s for s in _load_all_stations()
                if s['operator'].startswith(self.operator_prefix)]

    def search(self, query: str) -> list[LocationResult]:
        q = query.strip().lower()
        results = []
        for s in self._stations():
            if q in s['name'].lower():
                results.append(self._to_result(s))
            if len(results) >= 10:
                break
        return results

    def nearest_stations(self, lat: float, lon: float, limit: int = 5) -> list[dict]:
        stations = sorted(
            self._stations(),
            key=lambda s: _distance_km(lat, lon, s['latitude'], s['longitude']),
        )[:limit]
        out = []
        for s in stations:
            d = dict(s)
            d['distance_km'] = round(_distance_km(lat, lon, s['latitude'], s['longitude']), 2)
            out.append(d)
        return out

    def all_stations(self) -> list[dict]:
        """Return the full station list of this network."""
        return self._stations()

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
        resp = requests.get(_SMET_URL.format(short_name=provider_location_id),
                            headers=_HEADERS, timeout=60)
        if resp.status_code == 404 or not resp.content:
            return []
        resp.raise_for_status()
        data = resp.content
        if data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        return _parse_smet(data.decode('utf-8', errors='replace'))

    def fetch_all(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        # Hourly and daily aggregates come from the same week file —
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

    @staticmethod
    def _aggregate(rows: list[dict]) -> dict:
        temps  = [v for r in rows if (v := r.get('TA')) is not None]
        hums   = [v for r in rows if (v := r.get('RH')) is not None]
        winds  = [v for r in rows if (v := r.get('VW')) is not None]
        dirs   = [v for r in rows if (v := r.get('DW')) is not None]
        precip = [v for r in rows if (v := r.get('PSUM')) is not None and v >= 0]
        temp = _mean(temps)
        hum  = _mean(hums)
        wind = _mean(winds)
        return {
            'temps': temps,
            'temperature': round(temp, 1) if temp is not None else None,
            'humidity': round(hum) if hum is not None else None,
            'wind_speed': round(wind * 3.6) if wind is not None else None,
            'wind_direction': _compass_mean(dirs),
            'precip_amount': round(sum(precip), 1) if precip else None,
        }

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
            agg = self._aggregate(by_hour[hour])
            e = ForecastEntry(
                forecast_time=hour.isoformat(),
                granularity='hourly',
                temperature=agg['temperature'],
                humidity=agg['humidity'],
                wind_speed=agg['wind_speed'],
                wind_direction=agg['wind_direction'],
                precip_amount=agg['precip_amount'],
            )
            if any(v is not None for v in (e.temperature, e.wind_speed,
                                           e.humidity, e.precip_amount)):
                entries.append(e)
        return entries

    def _daily_entries(self, samples: list[dict],
                       relevant: Optional[set]) -> list[ForecastEntry]:
        today = datetime.now(_TZ).date()
        min_samples = _MIN_DAILY_COVERAGE * _samples_per_day(samples)
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
            if len(rows) < min_samples:   # truncated day at the feed edge
                continue
            agg = self._aggregate(rows)
            temps = agg['temps']
            e = ForecastEntry(
                forecast_time=f'{d.isoformat()}T00:00:00',
                granularity='daily',
                temperature=agg['temperature'],
                temp_max=round(max(temps), 1) if temps else None,
                temp_min=round(min(temps), 1) if temps else None,
                humidity=agg['humidity'],
                wind_speed=agg['wind_speed'],
                precip_amount=agg['precip_amount'],
            )
            if any(v is not None for v in (e.temperature, e.wind_speed,
                                           e.humidity, e.precip_amount)):
                entries.append(e)
        return entries


class LwdTirolProvider(TirolSmetProvider):
    name = 'lwd_tirol'
    display_name = 'LWD Tirol'
    operator_prefix = 'LWD Tirol'   # includes joint operators like 'LWD Tirol/WLV'


class HdTirolProvider(TirolSmetProvider):
    name = 'hd_tirol'
    display_name = 'HD Tirol'
    operator_prefix = 'HD Tirol'
