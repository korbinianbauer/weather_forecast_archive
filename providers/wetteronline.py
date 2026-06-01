import html as _html
import json
import logging
import re
from datetime import date
from typing import Optional

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


class WetterOnlineProvider(WeatherProvider):
    name = 'wetteronline'
    display_name = 'WetterOnline'

    supports_daily  = True
    supports_hourly = False

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
        slug = extra.get('slug', provider_location_id)
        url = f'{_BASE}/wetter/{slug}'
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            return _parse_forecast(resp.text)
        except Exception as e:
            logger.error('WetterOnline fetch failed for %s: %s', slug, e)
            return []


# ── HTML / JSON parsing ───────────────────────────────────────────────────────

def _parse_forecast(html: str) -> list[ForecastEntry]:
    for script in re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL):
        if 'metadata_p_city_local_LongTerm' not in script:
            continue
        try:
            return _extract_longterm(script)
        except Exception as e:
            logger.warning('WetterOnline JSON extraction failed: %s', e)
    logger.warning('WetterOnline: metadata_p_city_local_LongTerm not found in page')
    return []


def _extract_longterm(script: str) -> list[ForecastEntry]:
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
            entry = _parse_day(day_data, today)
            if entry and entry.forecast_time:
                result.append(entry)
        except Exception as e:
            logger.warning('WetterOnline day parse error: %s', e)
    return result


def _parse_day(d: dict, ref: date) -> Optional[ForecastEntry]:
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
    icon_url = f'https://st.wetteronline.de/icons/wetter-icons/s/{symbol}.png' if symbol else None

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
        wind_speed=_int(d.get('windGustKmh')),
        sunshine_hours=_float(d.get('absoluteSunshineDuration')),
    )


# ── value parsers ─────────────────────────────────────────────────────────────

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
