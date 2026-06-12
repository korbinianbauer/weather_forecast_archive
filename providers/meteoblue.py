import logging
import re
import time
from datetime import date, timedelta
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

import db
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
    supports_hourly = True

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
        entries = self._fetch_week(provider_location_id)
        soups = _fetch_oneday_pages(provider_location_id, len(entries))
        _apply_precip_probs(entries, soups)
        return entries

    def fetch_hourly(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        daily = self._fetch_week(provider_location_id)
        soups = _fetch_oneday_pages(provider_location_id, len(daily))
        return _parse_hourly(soups, [e.forecast_time[:10] for e in daily])

    def fetch_all(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        # Daily precip probabilities and 3-hourly entries come from the same
        # oneday pages — fetch them once and use them for both.
        entries = self._fetch_week(provider_location_id)
        soups = _fetch_oneday_pages(provider_location_id, len(entries))
        _apply_precip_probs(entries, soups)
        return entries + _parse_hourly(soups, [e.forecast_time[:10] for e in entries])

    def _fetch_week(self, provider_location_id: str) -> list[ForecastEntry]:
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
            if entry and entry.forecast_time:
                result.append(entry)
        except Exception as e:
            logger.warning('Day tab parse error: %s', e)
    return result


def _parse_day_tab(tab) -> ForecastEntry:
    date_el = tab.find('time', class_='date')
    forecast_date = date_el['datetime'] if date_el and date_el.get('datetime') else ''
    forecast_time = f'{forecast_date}T00:00:00' if forecast_date else ''

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
    precip_amount = _parse_precip_amount(precip_el.get_text() if precip_el else '')

    sunshine_hours: Optional[float] = None
    sun_el = tab.find(class_='tab-sun')
    if sun_el:
        for glyph in sun_el.find_all('span'):
            glyph.decompose()
        sunshine_hours = _parse_float(sun_el.get_text())

    return ForecastEntry(
        forecast_time=forecast_time,
        granularity='daily',
        condition_text=condition_text,
        icon_url=icon_url,
        temp_max=temp_max,
        temp_min=temp_min,
        wind_direction=wind_direction,
        wind_speed=wind_speed,
        precip_amount=precip_amount,
        sunshine_hours=sunshine_hours,
    )


def _fetch_oneday_pages(location_id: str, num_days: int) -> dict[int, BeautifulSoup]:
    """Fetch the per-day detail pages (3-hourly tables); keys are 1-based day numbers."""
    soups: dict[int, BeautifulSoup] = {}
    detail_headers = {**_HEADERS, 'X-Requested-With': 'XMLHttpRequest'}
    for day_num in range(1, num_days + 1):
        url = f'{_BASE}/en/weather/week/oneday/{location_id}?day={day_num}'
        try:
            resp = requests.get(url, headers=detail_headers, timeout=10)
            resp.raise_for_status()
            soups[day_num] = BeautifulSoup(resp.text, 'html.parser')
        except Exception as e:
            logger.warning('Meteoblue oneday fetch failed for day %d: %s', day_num, e)
        time.sleep(db.get_provider_delay('meteoblue'))
    return soups


def _apply_precip_probs(entries: list[ForecastEntry], soups: dict[int, BeautifulSoup]) -> None:
    """Set each daily entry's precip_probability to the day's max 3-hourly probability."""
    for i, entry in enumerate(entries):
        soup = soups.get(i + 1)
        tbl = soup.find('table') if soup else None
        prob_row = tbl.find('tr', class_='precipprobs') if tbl else None
        if not prob_row:
            continue
        max_prob = 0
        for td in prob_row.find_all('td'):
            sp = td.find(class_='precip-prob')
            if sp:
                val = _parse_int(sp.get_text(strip=True).rstrip('%'))
                if val is not None:
                    max_prob = max(max_prob, val)
        entry.precip_probability = max_prob


# ── 3-hourly parsing ──────────────────────────────────────────────────────────

def _parse_hourly(soups: dict[int, BeautifulSoup], dates: list[str]) -> list[ForecastEntry]:
    result = []
    for day_num, soup in sorted(soups.items()):
        if day_num - 1 >= len(dates):
            continue
        try:
            base = date.fromisoformat(dates[day_num - 1])
        except ValueError:
            continue
        try:
            result.extend(_parse_oneday(soup, base))
        except Exception as e:
            logger.warning('Meteoblue oneday parse failed for day %d: %s', day_num, e)
    return result


def _parse_oneday(soup: BeautifulSoup, base: date) -> list[ForecastEntry]:
    """Parse one oneday table (columns at 03:00 … 24:00 local time) into 3-hourly entries."""
    tbl = soup.find('table')
    if not tbl:
        return []

    def data_cells(cls: str) -> list:
        row = tbl.find('tr', class_=cls)
        return row.find_all('td') if row else []

    times = []
    for td in data_cells('times'):
        m = re.search(r'(\d{1,2})\s', td.get_text(' ', strip=True) + ' ')
        times.append(int(m.group(1)) if m else None)

    cols: dict[str, list] = {}
    cols['temperature'] = [_parse_signed_int(td.get_text()) for td in data_cells('temperatures')]
    cols['wind_direction'] = [(td.get_text(strip=True) or None) for td in data_cells('winddirs')]
    cols['wind_speed'] = [_parse_int(td.get_text()) for td in data_cells('windspeeds')]
    cols['precip_probability'] = [_parse_int(td.get_text()) for td in data_cells('precipprobs')]
    # find('tr', class_='precips') matches the plain row, not the
    # 'precips precip-hourly-title' interval row further down.
    cols['precip_amount'] = [
        _parse_precip_amount(re.sub(r'\d+\s*%', '', td.get_text(' ', strip=True)))
        for td in data_cells('precips')
    ]

    conds, icons = [], []
    for td in data_cells('icons'):
        img = td.find('img')
        conds.append((img.get('title') or img.get('alt')) if img else None)
        icons.append(img.get('src') if img else None)
    cols['condition_text'] = conds
    cols['icon_url'] = icons

    def col(name: str, j: int):
        vals = cols.get(name)
        return vals[j] if vals and j < len(vals) else None

    result = []
    for j, hour in enumerate(times):
        if hour is None:
            continue
        day = base + timedelta(days=hour // 24)
        entry = ForecastEntry(
            forecast_time=f'{day}T{hour % 24:02d}:00:00',
            granularity='hourly',
            condition_text=col('condition_text', j),
            icon_url=col('icon_url', j),
            temperature=col('temperature', j),
            precip_probability=col('precip_probability', j),
            precip_amount=col('precip_amount', j),
            wind_direction=col('wind_direction', j),
            wind_speed=col('wind_speed', j),
        )
        if entry.temperature is not None:
            result.append(entry)
    return result


def _parse_signed_int(text: str) -> Optional[int]:
    m = re.search(r'(-?\d+)', _clean(text))
    return int(m.group(1)) if m else None


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


def _parse_float(text: str) -> Optional[float]:
    text = _clean(text)
    m = re.search(r'(\d+(?:[.,]\d+)?)', text)
    return float(m.group(1).replace(',', '.')) if m else None


def _parse_precip_amount(text: str) -> float:
    """Parse precipitation amount, returning 0.0 for no-rain indicators.

    Handles: '-', 'dry', ranges like '0-10 mm' (→ midpoint), '<1 mm', plain values.
    """
    text = _clean(text)
    if not text or text == '-':
        return 0.0
    # Range: "X-Y mm" or "X - Y mm"
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*[-–]\s*(\d+(?:[.,]\d+)?)', text)
    if m:
        lo = float(m.group(1).replace(',', '.'))
        hi = float(m.group(2).replace(',', '.'))
        return (lo + hi) / 2.0
    # Plain number (possibly with "< " prefix)
    m = re.search(r'(\d+(?:[.,]\d+)?)', text)
    return float(m.group(1).replace(',', '.')) if m else 0.0
