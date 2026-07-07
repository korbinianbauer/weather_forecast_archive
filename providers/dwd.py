"""DWD open-data observations (ground truth, not a forecast).

Reads the 'recent' station observation files from opendata.dwd.de:
daily KL climate data plus the per-parameter hourly files. Observations are
immutable, so the poller only stores forecast_times not yet in the archive
(see `is_observation` and poller._poll_source).

provider_location_id is the 5-digit DWD Stations_id, e.g. '00044'.
"""
import io
import logging
import math
import re
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .base import ForecastEntry, LocationResult, WeatherProvider

logger = logging.getLogger(__name__)

_BASE = 'https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate'
_STATION_LIST_URL = f'{_BASE}/daily/kl/recent/KL_Tageswerte_Beschreibung_Stationen.txt'
_HEADERS = {'User-Agent': 'bether-weather-archive (personal, single daily poll)'}
_TZ = ZoneInfo('Europe/Berlin')

# (directory, file prefix) of each hourly parameter file
_HOURLY_SOURCES = [
    ('air_temperature', 'TU'),
    ('precipitation',   'RR'),
    ('wind',            'FF'),
    ('pressure',        'P0'),
    ('sun',             'SD'),
    ('cloudiness',      'N'),
]

_COMPASS = ['N', 'NNO', 'NO', 'ONO', 'O', 'OSO', 'SO', 'SSO',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']

_STATION_CACHE_TTL = 6 * 3600
_station_cache: dict = {'ts': 0.0, 'stations': []}


def _load_stations() -> list[dict]:
    """Active stations from the daily-KL station list (cached in-process)."""
    if _station_cache['stations'] and time.time() - _station_cache['ts'] < _STATION_CACHE_TTL:
        return _station_cache['stations']

    resp = requests.get(_STATION_LIST_URL, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    text = resp.content.decode('latin-1')

    cutoff = (date.today() - timedelta(days=30)).strftime('%Y%m%d')
    stations = []
    for line in text.splitlines()[2:]:
        m = re.match(r'\s*(\d+)\s+(\d{8})\s+(\d{8})\s+(-?\d+)\s+'
                     r'(-?[\d.]+)\s+(-?[\d.]+)\s+(\S.*)$', line)
        if not m:
            continue
        sid, _von, bis, height, lat, lon, rest = m.groups()
        tokens = rest.split()
        if tokens and tokens[-1] == 'Frei':   # trailing 'Abgabe' column
            tokens = tokens[:-1]
        if len(tokens) < 2:
            continue
        if bis < cutoff:                       # station no longer reporting
            continue
        stations.append({
            'id': sid.zfill(5),
            'name': ' '.join(tokens[:-1]),
            'state': tokens[-1],
            'latitude': float(lat),
            'longitude': float(lon),
            'height': int(height),
        })

    _station_cache['stations'] = stations
    _station_cache['ts'] = time.time()
    logger.info('Loaded %d active DWD stations', len(stations))
    return stations


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    a = (math.sin((rlat2 - rlat1) / 2) ** 2 +
         math.cos(rlat1) * math.cos(rlat2) * math.sin((rlon2 - rlon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _read_zip_product(url: str) -> list[dict]:
    """Download a DWD zip and parse its produkt_*.txt into per-row dicts
    keyed by the ';'-separated header names. [] if the file does not exist."""
    resp = requests.get(url, headers=_HEADERS, timeout=60)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    name = next((n for n in z.namelist() if n.startswith('produkt')), None)
    if not name:
        return []
    lines = z.read(name).decode('latin-1').splitlines()
    header = [h.strip() for h in lines[0].split(';')]
    rows = []
    for line in lines[1:]:
        parts = [p.strip() for p in line.split(';')]
        if len(parts) == len(header):
            rows.append(dict(zip(header, parts)))
    return rows


def _fval(row: dict, key: str) -> Optional[float]:
    v = row.get(key)
    if v in (None, ''):
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return None if f <= -999 else f


def _compass(deg: Optional[float]) -> Optional[str]:
    if deg is None or deg < 0:
        return None
    return _COMPASS[round(deg / 22.5) % 16]


def _utc_hour_to_local_iso(mess_datum: str) -> str:
    """Hourly MESS_DATUM (YYYYMMDDHH, UTC) → local Europe/Berlin ISO string."""
    dt = datetime.strptime(mess_datum[:10], '%Y%m%d%H').replace(tzinfo=timezone.utc)
    return dt.astimezone(_TZ).replace(tzinfo=None).isoformat()


class DwdProvider(WeatherProvider):
    name = 'dwd'
    display_name = 'DWD'
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
        """Return the full list of active DWD stations."""
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

    def fetch_daily(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        sid = provider_location_id.zfill(5)
        rows = _read_zip_product(f'{_BASE}/daily/kl/recent/tageswerte_KL_{sid}_akt.zip')
        entries = []
        for row in rows:
            d = row.get('MESS_DATUM', '')
            if len(d) != 8:
                continue
            fm = _fval(row, 'FM')       # daily mean wind, m/s
            nm = _fval(row, 'NM')       # daily mean cloud cover, octas
            upm = _fval(row, 'UPM')     # daily mean rel. humidity, %
            e = ForecastEntry(
                forecast_time=f'{d[:4]}-{d[4:6]}-{d[6:8]}T00:00:00',
                granularity='daily',
                temperature=_fval(row, 'TMK'),
                temp_max=_fval(row, 'TXK'),
                temp_min=_fval(row, 'TNK'),
                precip_amount=_fval(row, 'RSK'),
                sunshine_hours=_fval(row, 'SDK'),
                wind_speed=round(fm * 3.6) if fm is not None else None,
                cloud_cover=round(nm / 8 * 100) if nm is not None else None,
                pressure=_fval(row, 'PM'),
                humidity=round(upm) if upm is not None else None,
            )
            if any(v is not None for v in (e.temperature, e.temp_max, e.temp_min,
                                           e.precip_amount, e.sunshine_hours,
                                           e.wind_speed, e.cloud_cover,
                                           e.pressure, e.humidity)):
                entries.append(e)
        return entries

    def fetch_hourly(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        sid = provider_location_id.zfill(5)
        merged: dict[str, dict] = {}

        def slot(row) -> Optional[dict]:
            ts = row.get('MESS_DATUM', '')
            return merged.setdefault(ts[:10], {}) if len(ts) >= 10 else None

        for directory, code in _HOURLY_SOURCES:
            url = f'{_BASE}/hourly/{directory}/recent/stundenwerte_{code}_{sid}_akt.zip'
            try:
                rows = _read_zip_product(url)
            except Exception as e:
                logger.warning('DWD hourly %s for station %s failed: %s', code, sid, e)
                continue
            for row in rows:
                s = slot(row)
                if s is None:
                    continue
                if code == 'TU':
                    s['temperature'] = _fval(row, 'TT_TU')
                    rf = _fval(row, 'RF_TU')
                    s['humidity'] = round(rf) if rf is not None else None
                elif code == 'RR':
                    s['precip_amount'] = _fval(row, 'R1')
                elif code == 'FF':
                    f = _fval(row, 'F')
                    s['wind_speed'] = round(f * 3.6) if f is not None else None
                    s['wind_direction'] = _compass(_fval(row, 'D'))
                elif code == 'P0':
                    s['pressure'] = _fval(row, 'P')     # reduced to sea level
                elif code == 'SD':
                    sd = _fval(row, 'SD_SO')            # minutes of sunshine
                    s['sunshine_hours'] = round(sd / 60, 2) if sd is not None else None
                elif code == 'N':
                    vn = _fval(row, 'V_N')              # octas, -1 = missing
                    s['cloud_cover'] = round(vn / 8 * 100) if vn is not None and vn >= 0 else None

        entries = []
        for ts in sorted(merged):
            values = {k: v for k, v in merged[ts].items() if v is not None}
            if not values:
                continue
            entries.append(ForecastEntry(
                forecast_time=_utc_hour_to_local_iso(ts),
                granularity='hourly',
                **values,
            ))
        return entries
