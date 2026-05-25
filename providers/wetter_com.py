import logging
import re
from datetime import date
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .base import ForecastEntry, LocationResult, WeatherProvider

logger = logging.getLogger(__name__)

_BASE = 'https://www.wetter.com'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}


class WetterComProvider(WeatherProvider):
    name = 'wetter_com'
    display_name = 'Wetter.com'

    supports_daily = True
    supports_hourly = False

    def search(self, query: str) -> list[LocationResult]:
        url = f'{_BASE}/search/autosuggest/{quote(query)}'
        try:
            resp = requests.get(
                url,
                headers={**_HEADERS, 'X-Requested-With': 'XMLHttpRequest'},
                timeout=10,
            )
            resp.raise_for_status()
            return [
                LocationResult(
                    name=item['title'].split(' - ')[0],
                    provider_location_id=item['code'],
                    latitude=item.get('latitude'),
                    longitude=item.get('longitude'),
                    extra={'seo_string': item['seoString'], 'title': item['title']},
                )
                for item in resp.json().get('locations', [])
            ]
        except Exception as e:
            logger.error('Wetter.com search failed: %s', e)
            return []

    def fetch_daily(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        seo = extra.get('seo_string', '')
        url = (f'{_BASE}/wetter_aktuell/wettervorhersage/'
               f'16_tagesvorhersage/{seo}/{provider_location_id}.html')
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            return _parse_daily(resp.text)
        except Exception as e:
            logger.error('Wetter.com daily fetch failed for %s: %s', provider_location_id, e)
            return []


# ── HTML parsing ──────────────────────────────────────────────────────────────

def _parse_daily(html: str) -> list[ForecastEntry]:
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.find_all('div', class_='swg-row-wrapper', attrs={'data-grid-child': True})
    today = date.today()
    result = []
    for row in rows:
        try:
            entry = _parse_daily_row(row, today)
            if entry and entry.forecast_time:
                result.append(entry)
        except Exception as e:
            logger.warning('Row parse error: %s', e)
    return result


def _parse_daily_row(row, ref: date) -> ForecastEntry:
    period = row.find('div', class_='swg-col-period')
    forecast_date = _parse_date(period.get_text(strip=True) if period else '', ref)
    forecast_time = f'{forecast_date}T00:00:00' if forecast_date else ''

    icon_url: Optional[str] = None
    icon_div = row.find('div', class_='swg-col-icon')
    if icon_div:
        img = icon_div.find('img', attrs={'data-single-src': True})
        if img:
            icon_url = img['data-single-src']

    temp_max = temp_min = None
    temp_div = row.find('div', class_='swg-col-temperature')
    if temp_div:
        temp_max = _int(temp_div.find('span', class_='swg-text-large'))
        temp_min = _int(temp_div.find('span', class_='swg-text-small'))

    wind_dir: Optional[str] = None
    wi3 = row.find('div', class_='swg-col-wi3')
    if wi3:
        span = wi3.find('span')
        if span:
            span.decompose()
        wind_dir = _clean(wi3.get_text()) or None

    text_div = row.find('div', class_='swg-col-text')

    return ForecastEntry(
        forecast_time=forecast_time,
        granularity='daily',
        condition_text=(text_div.get_text(strip=True) if text_div else None) or None,
        icon_url=icon_url,
        temp_max=temp_max,
        temp_min=temp_min,
        precip_probability=_int(row.find('div', class_='swg-col-wv1')),
        precip_amount=_float(row.find('div', class_='swg-col-wv2')),
        wind_direction=wind_dir,
        wind_speed=_int(row.find('div', class_='swg-col-wv3')),
    )


# ── helpers ───────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return text.replace('\xa0', ' ').replace(' ', '').strip()


def _parse_date(text: str, ref: date) -> str:
    m = re.search(r'(\d{1,2})\.(\d{2})\.', text)
    if not m:
        return ''
    day, month = int(m.group(1)), int(m.group(2))
    year = ref.year + (1 if month < ref.month - 1 else 0)
    try:
        return str(date(year, month, day))
    except ValueError:
        return ''


def _int(el) -> Optional[int]:
    if el is None:
        return None
    raw = el.get_text() if hasattr(el, 'get_text') else str(el)
    m = re.search(r'(-?\d+)', _clean(raw))
    return int(m.group(1)) if m else None


def _float(el) -> Optional[float]:
    if el is None:
        return None
    raw = el.get_text() if hasattr(el, 'get_text') else str(el)
    m = re.search(r'(\d+(?:[.,]\d+)?)', _clean(raw))
    return float(m.group(1).replace(',', '.')) if m else None
