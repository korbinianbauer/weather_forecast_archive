import html as _html
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests

from .base import ForecastEntry, LocationResult, WeatherProvider

logger = logging.getLogger(__name__)

_BASE        = 'https://www.wetteronline.de'
_SEARCH_BASE = 'https://search.prod.geo.wo-cloud.com'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'de-DE,de;q=0.9',
    'Referer': 'https://www.wetteronline.de/',
}

_CARDINAL = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW']

# wetteronline.de serves all times in German local time; anchor the hourcast
# day inference to that zone, not the server clock (which may run UTC).
_SITE_TZ = ZoneInfo('Europe/Berlin')


class WetterOnlineProvider(WeatherProvider):
    name = 'wetteronline'
    display_name = 'WetterOnline'

    supports_daily  = True
    supports_hourly = True

    def search(self, query: str) -> list[LocationResult]:
        try:
            resp = requests.get(
                f'{_SEARCH_BASE}/v1/autosuggest',
                params={'language': 'de', 'application': 'web', 'region': 'DE', 'name': query},
                headers=_HEADERS,
                timeout=10,
            )
            resp.raise_for_status()
            candidates = resp.json()[:6]
        except Exception as e:
            logger.error('WetterOnline autosuggest failed: %s', e)
            return []

        results = []
        for c in candidates:
            key = c.get('geoObjectKey')
            if not key:
                continue
            try:
                r2 = requests.get(
                    f'{_BASE}/search',
                    params={
                        'ireq': 'true', 'geoObjectKey': key,
                        'searchpcid': 'pc_city_weather', 'searchpid': 'p_city_weather',
                        'output': 'json',
                    },
                    headers={**_HEADERS, 'X-Requested-With': 'XMLHttpRequest'},
                    timeout=10,
                )
                r2.raise_for_status()
                loc = next((x for x in r2.json() if isinstance(x, dict) and 'gid' in x), None)
                if not loc:
                    continue
                results.append(LocationResult(
                    name=loc['name'],
                    provider_location_id=loc['gid'],
                    latitude=float(loc['lat']) if loc.get('lat') else None,
                    longitude=float(loc['lon']) if loc.get('lon') else None,
                    extra={
                        'slug': loc['url'].split('/')[-1],
                        'title': f"{loc['name']}, {loc.get('statename', '')}".strip(', '),
                    },
                ))
            except Exception as e:
                logger.warning('WetterOnline resolve failed for key %s: %s', key, e)
        return results

    def fetch_daily(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        trend = self._fetch_trend(provider_location_id, extra)
        return _parse_forecast(trend) if trend else []

    def fetch_hourly(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        html = self._fetch_page(provider_location_id, extra)
        trend = self._fetch_trend(provider_location_id, extra)
        return _parse_sub_daily(html, trend)

    def fetch_all(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        # Hourcast (~49h true hourly) lives on the city page; daily and 6-h
        # interval (MediumTerm) forecasts moved to the /wettertrend page —
        # WetterOnline split these across two pages, so fetch both.
        html = self._fetch_page(provider_location_id, extra)
        trend = self._fetch_trend(provider_location_id, extra)
        return _parse_forecast(trend) + _parse_sub_daily(html, trend)

    def _fetch_page(self, provider_location_id: str, extra: dict) -> str:
        slug = extra.get('slug', provider_location_id)
        url = f'{_BASE}/wetter/{slug}'
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            logger.error('WetterOnline fetch failed for %s: %s', slug, e)
            return ''

    def _fetch_trend(self, provider_location_id: str, extra: dict) -> dict:
        """Fetch /wettertrend/{slug}: daily (longTerm) and 6-h interval
        (mediumTerm) forecast data, hydrated client-side via Angular's
        `ng-state` JSON blob under the 'p_city_local/forecast' key."""
        slug = extra.get('slug', provider_location_id)
        url = f'{_BASE}/wettertrend/{slug}'
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.error('WetterOnline trend fetch failed for %s: %s', slug, e)
            return {}
        m = re.search(r'<script id="ng-state"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not m:
            logger.warning('WetterOnline: ng-state not found on trend page for %s', slug)
            return {}
        try:
            state = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            logger.warning('WetterOnline: ng-state parse failed for %s: %s', slug, e)
            return {}
        forecast = state.get('p_city_local/forecast') or {}
        forecast['_icon_base'] = _icon_base(html)
        return forecast


def _icon_base(html: str) -> str:
    m = re.search(r'st\.wetteronline\.de/dr/(\d+\.\d+\.\d+)/', html)
    if m:
        return f'https://st.wetteronline.de/dr/{m.group(1)}/city/prozess/graphiken/symbole/standard/farbe/png/40x28'
    return ''


# ── HTML / JSON parsing ───────────────────────────────────────────────────────

def _parse_forecast(trend: dict) -> list[ForecastEntry]:
    """Daily forecast from the /wettertrend page's 'longTerm' array (~16 days).

    WetterOnline moved this off the city page (and dropped the old
    'metadata_p_city_local_LongTerm' embed) some time before 2026-08-08 in
    favor of an Angular app hydrated from ng-state JSON — see _fetch_trend.
    """
    days = trend.get('longTerm') or []
    icon_base = trend.get('_icon_base', '')
    result = []
    for day_data in days:
        try:
            entry = _parse_day(day_data, icon_base)
            if entry:
                result.append(entry)
        except Exception as e:
            logger.warning('WetterOnline day parse error: %s', e)
    return result


def _parse_day(d: dict, icon_base: str = '') -> Optional[ForecastEntry]:
    try:
        forecast_date = date.fromisoformat(d.get('date', ''))
    except ValueError:
        return None

    wind = d.get('wind') or {}
    wind_dir: Optional[str] = None
    if wind.get('direction') is not None:
        try:
            wind_dir = _CARDINAL[round(float(wind['direction']) / 45) % 8]
        except (ValueError, TypeError):
            pass

    symbol = d.get('symbol', '')
    icon_url = f'{icon_base}/{symbol}.png' if (symbol and icon_base) else None

    temp = d.get('airTemperature') or {}
    temp_max = (temp.get('max') or {}).get('celsius')
    temp_min = (temp.get('min') or {}).get('celsius')

    return ForecastEntry(
        forecast_time=f'{forecast_date}T00:00:00',
        granularity='daily',
        condition_text=None,
        icon_url=icon_url,
        temp_max=_int(temp_max),
        temp_min=_int(temp_min),
        precip_probability=_int(d.get('precipitationProbability')),
        precip_amount=_float(d.get('precipitationAmount24')),
        wind_direction=wind_dir,
        wind_speed=_wind_force_to_kmh(wind.get('force')),
        sunshine_hours=_float(d.get('absoluteSunshineDuration')),
    )


# ── sub-daily parsing (hourcast + 6-h intervals) ──────────────────────────────

def _parse_sub_daily(html: str, trend: dict) -> list[ForecastEntry]:
    """Everything sub-daily available: ~49 h of true hourly data (hourcast,
    from the city page) plus 6-h interval data for ~4 days (mediumTerm, from
    the trend page). Where both cover the same timestamp the richer hourcast
    entry wins."""
    entries = _parse_hourcast(html) if html else []
    seen = {e.forecast_time for e in entries}
    for entry in _parse_medium_term(trend):
        if entry.forecast_time not in seen:
            seen.add(entry.forecast_time)
            entries.append(entry)
    entries.sort(key=lambda e: e.forecast_time)
    return entries


def _parse_hourcast(html: str, now: Optional[datetime] = None) -> list[ForecastEntry]:
    """Parse the SSR'd <wo-forecast-hour> strip (next ~49 hours).

    Available per hour: temperature, condition + symbol, precip probability,
    wind direction (arrow rotation). No wind speed or precip amount.

    Parsed with regexes over the raw element chunks — BeautifulSoup's
    html.parser mis-nests the page's custom elements / declarative shadow
    DOM, so element text ends up detached from its parent.
    """
    chunks = html.split('<wo-forecast-hour')[1:]
    if not chunks:
        logger.warning('WetterOnline: no hourcast elements found')
        return []

    icon_base = _icon_base(html)
    now = now or datetime.now(_SITE_TZ)

    result = []
    day = None
    prev_hour = None
    for chunk in chunks:
        chunk = chunk.split('</wo-forecast-hour>')[0]
        m = re.search(r'<wo-date-hour[^>]*>\s*(\d{1,2}):00\s*</wo-date-hour>', chunk)
        if not m:
            continue
        hour = int(m.group(1))

        if day is None:
            # The strip starts at the next full hour; if that already wrapped
            # past midnight it belongs to tomorrow.
            day = now.date() if hour >= now.hour else now.date() + timedelta(days=1)
        elif prev_hour is not None and hour < prev_hour:
            day += timedelta(days=1)
        prev_hour = hour

        temp = None
        tm = re.search(r'class="temperature"[^>]*>\s*(-?\d+)', chunk)
        if tm:
            temp = int(tm.group(1))

        condition = icon_url = None
        sm = re.search(r'<img[^>]*class="symbol"[^>]*>', chunk)
        if sm:
            img_tag = sm.group(0)
            am = re.search(r'alt="([^"]*)"', img_tag)
            condition = _html.unescape(am.group(1)) if am and am.group(1) else None
            um = re.search(r'src="[^"]*/([a-z0-9_]+)\.svg', img_tag)
            if um and icon_base:
                icon_url = f'{icon_base}/{um.group(1)}.png'

        precip_prob = None
        pm = re.search(r'(\d+)\s*%\s*Niederschlagswahrscheinlichkeit', chunk)
        if not pm:
            pm = re.search(r'class="description"[^>]*>\s*(\d+)\s*%', chunk)
        if pm:
            precip_prob = int(pm.group(1))

        wind_dir = None
        rm = re.search(r'rotate\((\d+)deg\)', chunk)
        if rm:
            wind_dir = _CARDINAL[round(int(rm.group(1)) / 45) % 8]

        result.append(ForecastEntry(
            forecast_time=f'{day}T{hour:02d}:00:00',
            granularity='hourly',
            condition_text=condition,
            icon_url=icon_url,
            temperature=temp,
            precip_probability=precip_prob,
            wind_direction=wind_dir,
        ))
    return result


_INTERVAL_HOURS = {'morning': 9, 'afternoon': 15, 'evening': 21, 'night': 3}


def _parse_medium_term(trend: dict) -> list[ForecastEntry]:
    """6-h interval forecast for ~4 days, from the /wettertrend page's
    'mediumTerm' array.

    Available per interval: condition + symbol, precip probability, wind
    direction/force. No temperature, and no precip amount (the new payload
    only exposes a coarse 'amountClass' bucket, not a figure).
    """
    icon_base = trend.get('_icon_base', '')
    days = trend.get('mediumTerm') or []

    result = []
    for day_data in days:
        try:
            base = date.fromisoformat(day_data.get('date', ''))
        except ValueError:
            continue
        for iv in day_data.get('intervals', []):
            hour = _INTERVAL_HOURS.get(iv.get('time'))
            if hour is None:
                continue
            # 'night' is the night following the day → early hours of the next day
            day = base + timedelta(days=1) if iv['time'] == 'night' else base

            wind = iv.get('wind') or {}
            wind_dir = None
            if wind.get('direction') is not None:
                try:
                    wind_dir = _CARDINAL[round(float(wind['direction']) / 45) % 8]
                except (ValueError, TypeError):
                    pass

            precip = iv.get('precipitation') or {}
            symbol = iv.get('symbol', '')

            result.append(ForecastEntry(
                forecast_time=f'{day}T{hour:02d}:00:00',
                granularity='hourly',
                condition_text=None,
                icon_url=f'{icon_base}/{symbol}.png' if (symbol and icon_base) else None,
                precip_probability=_int(precip.get('probability')),
                precip_amount=_amount(precip.get('amount')),
                wind_direction=wind_dir,
                wind_speed=_wind_force_to_kmh(wind.get('force')),
            ))
    return result


def _amount(val) -> Optional[float]:
    """Parse interval precip amounts: '0', '&lt; 1', '2-5' (→ midpoint)."""
    if val is None:
        return None
    text = _html.unescape(str(val)).strip()
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*-\s*(\d+(?:[.,]\d+)?)', text)
    if m:
        return (float(m.group(1).replace(',', '.')) + float(m.group(2).replace(',', '.'))) / 2.0
    m = re.search(r'(\d+(?:[.,]\d+)?)', text)
    return float(m.group(1).replace(',', '.')) if m else None


# ── value parsers ─────────────────────────────────────────────────────────────

def _parse_bft(val) -> Optional[float]:
    """Parse a Beaufort force value – may be a range like '3-4' → midpoint."""
    if val is None:
        return None
    try:
        s = str(val).strip()
        m = re.match(r'(\d+(?:[.,]\d+)?)\s*-\s*(\d+(?:[.,]\d+)?)', s)
        if m:
            return (float(m.group(1).replace(',', '.'))
                    + float(m.group(2).replace(',', '.'))) / 2.0
        return float(s.replace(',', '.'))
    except (ValueError, TypeError):
        return None


def _wind_force_to_kmh(val) -> Optional[int]:
    """Convert WetterOnline Beaufort wind force to km/h (sustained)."""
    bft = _parse_bft(val)
    if bft is None:
        return None
    # v (km/h) = 3.6 × 0.836 × bft^(3/2)   — WMO formula
    return round(3.01 * (bft ** 1.5))


def _int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(str(val).replace(',', '.')))
    except (ValueError, TypeError):
        return None


def _float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(str(val).replace(',', '.'))
    except (ValueError, TypeError):
        return None
