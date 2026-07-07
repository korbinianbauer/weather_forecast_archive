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
        html = self._fetch_page(provider_location_id, extra)
        return _parse_forecast(html) if html else []

    def fetch_hourly(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        html = self._fetch_page(provider_location_id, extra)
        return _parse_sub_daily(html) if html else []

    def fetch_all(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        # Daily, hourly (hourcast) and 6-h interval data all live on the same
        # city page — fetch it once.
        html = self._fetch_page(provider_location_id, extra)
        if not html:
            return []
        return _parse_forecast(html) + _parse_sub_daily(html)

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


def _icon_base(html: str) -> str:
    m = re.search(r'st\.wetteronline\.de/dr/(\d+\.\d+\.\d+)/', html)
    if m:
        return f'https://st.wetteronline.de/dr/{m.group(1)}/city/prozess/graphiken/symbole/standard/farbe/png/40x28'
    return ''


# ── HTML / JSON parsing ───────────────────────────────────────────────────────

def _parse_forecast(html: str) -> list[ForecastEntry]:
    base = _icon_base(html)
    for script in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        if 'metadata_p_city_local_LongTerm' not in script:
            continue
        try:
            return _extract_longterm(script, base)
        except Exception as e:
            logger.warning('WetterOnline JSON extraction failed: %s', e)
    logger.warning('WetterOnline: metadata_p_city_local_LongTerm not found in page')
    return []


def _extract_longterm(script: str, icon_base: str) -> list[ForecastEntry]:
    key = '"metadata_p_city_local_LongTerm"'
    idx = script.find(key)
    if idx == -1:
        return []
    colon = script.index(':', idx + len(key))
    arr_start = script.index('[', colon)
    days, _ = json.JSONDecoder().raw_decode(script, arr_start)

    today = date.today()
    result = []
    for day_data in days:
        try:
            entry = _parse_day(day_data, today, icon_base)
            if entry and entry.forecast_time:
                result.append(entry)
        except Exception as e:
            logger.warning('WetterOnline day parse error: %s', e)
    return result


def _parse_day(d: dict, ref: date, icon_base: str = '') -> Optional[ForecastEntry]:
    # date is "DD.MM." without year
    m = re.match(r'(\d{1,2})\.(\d{2})\.', d.get('date', ''))
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    year = ref.year + (1 if month < ref.month else 0)
    try:
        forecast_date = date(year, month, day)
    except ValueError:
        return None

    wind_dir: Optional[str] = None
    if d.get('windDirection') is not None:
        try:
            wind_dir = _CARDINAL[round(float(d['windDirection']) / 45) % 8]
        except (ValueError, TypeError):
            pass

    symbol = d.get('symbol', '')
    icon_url = f'{icon_base}/{symbol}.png' if (symbol and icon_base) else None

    return ForecastEntry(
        forecast_time=f'{forecast_date}T00:00:00',
        granularity='daily',
        condition_text=_html.unescape(d['symbolText']) if d.get('symbolText') else None,
        icon_url=icon_url,
        temp_max=_int(d.get('maxTemperature')),
        temp_min=_int(d.get('minTemperature')),
        precip_probability=_int(d.get('precipitationProbability')),
        precip_amount=_float(d.get('precipitationAmount24')),
        wind_direction=wind_dir,
        wind_speed=_wind_force_to_kmh(d.get('windForce')),
        sunshine_hours=_float(d.get('absoluteSunshineDuration')),
    )


# ── sub-daily parsing (hourcast + 6-h intervals) ──────────────────────────────

def _parse_sub_daily(html: str) -> list[ForecastEntry]:
    """Everything sub-daily the page offers: ~49 h of true hourly data
    (hourcast) plus 6-h interval data for ~4 days (MediumTerm). Where both
    cover the same timestamp the richer hourcast entry wins."""
    entries = _parse_hourcast(html)
    seen = {e.forecast_time for e in entries}
    for entry in _parse_medium_term(html):
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


def _parse_medium_term(html: str) -> list[ForecastEntry]:
    """Parse the embedded MediumTerm JSON: 6-h intervals for ~4 days.

    Available per interval: condition + symbol, precip probability/amount,
    wind direction + max gusts. No temperature.
    """
    icon_base = _icon_base(html)
    key = '"metadata_p_city_local_MediumTerm"'
    idx = html.find(key)
    if idx == -1:
        return []
    try:
        colon = html.index(':', idx + len(key))
        arr_start = html.index('[', colon)
        days, _ = json.JSONDecoder().raw_decode(html, arr_start)
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning('WetterOnline MediumTerm extraction failed: %s', e)
        return []

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
