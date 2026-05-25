import logging
import re
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .base import ForecastEntry, LocationResult, WeatherProvider

logger = logging.getLogger(__name__)

_BASE = 'https://www.meteoblue.com'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
}
_SEARCH_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Referer': 'https://www.meteoblue.com/',
}


class MeteoblueProvider(WeatherProvider):
    name = 'meteoblue'
    display_name = 'Meteoblue'

    supports_daily = True
    supports_hourly = False

    def search(self, query: str) -> list[LocationResult]:
        url = f'{_BASE}/en/server/search/query3?query={quote(query)}'
        try:
            resp = requests.get(url, headers=_SEARCH_HEADERS, timeout=10)
            resp.raise_for_status()
            return [
                LocationResult(
                    name=item['name'],
                    provider_location_id=item['url'],
                    latitude=item.get('lat'),
                    longitude=item.get('lon'),
                    extra={
                        'title': f"{item['name']}, {item.get('admin1', '')}, {item.get('iso2', '')}",
                    },
                )
                for item in resp.json().get('results', [])
            ]
        except Exception as e:
            logger.error('Meteoblue search failed: %s', e)
            return []

    def fetch_daily(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        url = f'{_BASE}/en/weather/forecast/week/{provider_location_id}'
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            return _parse_week(resp.text)
        except Exception as e:
            logger.error('Meteoblue daily fetch failed for %s: %s', provider_location_id, e)
            return []


# ── HTML parsing ──────────────────────────────────────────────────────────────

def _parse_week(html: str) -> list[ForecastEntry]:
    soup = BeautifulSoup(html, 'html.parser')
    day_tabs = soup.find_all('div', id=re.compile(r'^day\d+$'))
    result = []
    for tab in day_tabs[:14]:
        try:
            entry = _parse_day_tab(tab)
            if entry and entry.forecast_date:
                result.append(entry)
        except Exception as e:
            logger.warning('Day tab parse error: %s', e)
    return result


def _parse_day_tab(tab) -> ForecastEntry:
    date_el = tab.find('time', class_='date')
    forecast_date = date_el['datetime'] if date_el and date_el.get('datetime') else ''

    picto = tab.find('img', class_='weather-pictogram')
    condition_text: Optional[str] = picto.get('alt') or None if picto else None
    icon_url: Optional[str] = picto.get('src') or None if picto else None

    temp_max = _parse_temp(tab.find(class_='tab-temp-max'))
    temp_min = _parse_temp(tab.find(class_='tab-temp-min'))

    wind_direction: Optional[str] = None
    wind_speed: Optional[int] = None
    wind_el = tab.find(class_='wind')
    if wind_el:
        glyph = wind_el.find('span', class_='winddir')
        if glyph:
            dirs = [c for c in glyph.get('class', []) if c not in ('glyph', 'winddir')]
            wind_direction = dirs[0].upper() if dirs else None
        wind_speed = _parse_int(wind_el.get_text())

    precip_el = tab.find(class_='tab-precip')
    precip_amount = _parse_precip(precip_el.get_text() if precip_el else '')

    return ForecastEntry(
        forecast_date=forecast_date,
        granularity='daily',
        condition_text=condition_text,
        icon_url=icon_url,
        temp_max=temp_max,
        temp_min=temp_min,
        wind_direction=wind_direction,
        wind_speed=wind_speed,
        precip_amount=precip_amount,
    )


# ── value parsers ─────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return text.replace('\xa0', ' ').strip()


def _parse_temp(el) -> Optional[int]:
    if el is None:
        return None
    m = re.search(r'(-?\d+)', _clean(el.get_text()))
    return int(m.group(1)) if m else None


def _parse_int(text: str) -> Optional[int]:
    m = re.search(r'(\d+)', _clean(text))
    return int(m.group(1)) if m else None


def _parse_precip(text: str) -> Optional[float]:
    text = _clean(text)
    if not text or text == '-':
        return None
    m = re.search(r'(\d+(?:[.,]\d+)?)', text)
    return float(m.group(1).replace(',', '.')) if m else None
