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

_BASE = 'https://www.wetter.com'
_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0',
    'Accept-Language': 'de-DE,de;q=0.9',
}


class WetterComProvider(WeatherProvider):
    name = 'wetter_com'
    display_name = 'Wetter.com'

    supports_daily = True
    supports_hourly = True

    def search(self, query: str) -> list[LocationResult]:
        url = f'{_BASE}/search/autosuggest/{quote(query)}'
        try:
            resp = requests.get(
                url,
                headers={**_HEADERS, 'X-Requested-With': 'XMLHttpRequest'},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                return []
            return [
                LocationResult(
                    name=item['title'].split(' - ')[0],
                    provider_location_id=item['code'],
                    latitude=item.get('latitude'),
                    longitude=item.get('longitude'),
                    extra={'seo_string': item['seoString'], 'title': item['title']},
                )
                for item in data.get('locations', [])
            ]
        except Exception as e:
            logger.error('Wetter.com search failed: %s', e)
            return []

    def fetch_daily(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        entries = self._fetch_16day(provider_location_id, extra)
        tables = _fetch_detail_tables(extra.get('seo_string', ''), provider_location_id)
        _apply_detail_extras(entries, tables)
        return entries

    def fetch_hourly(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        tables = _fetch_detail_tables(extra.get('seo_string', ''), provider_location_id)
        return _parse_hourly_tables(tables)

    def fetch_all(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
        # Daily extras and hourly entries come from the same detail pages —
        # fetch them once and use them for both.
        entries = self._fetch_16day(provider_location_id, extra)
        tables = _fetch_detail_tables(extra.get('seo_string', ''), provider_location_id)
        _apply_detail_extras(entries, tables)
        return entries + _parse_hourly_tables(tables)

    def _fetch_16day(self, provider_location_id: str, extra: dict) -> list[ForecastEntry]:
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

    # Build sunshine-hours lookup: data-grid-child index → hours float
    sun_by_idx: dict[str, Optional[float]] = {}
    for astro in soup.find_all('div', class_='astronomy-strip', attrs={'data-grid-parent': True}):
        idx = astro.get('data-grid-parent')
        sun_span = astro.find('span', class_='icon-sun_hours')
        if sun_span:
            val_span = sun_span.find_next_sibling('span')
            if val_span:
                sun_by_idx[idx] = _float(val_span)

    result = []
    for row in rows:
        try:
            entry = _parse_daily_row(row, today, sun_by_idx)
            if entry and entry.forecast_time:
                result.append(entry)
        except Exception as e:
            logger.warning('Row parse error: %s', e)
    return result


def _parse_daily_row(row, ref: date, sun_by_idx: dict) -> ForecastEntry:
    idx = row.get('data-grid-child')
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
        sunshine_hours=sun_by_idx.get(idx),
    )


def _fetch_detail_tables(seo: str, loc_id: str) -> dict[str, 'BeautifulSoup']:
    """Fetch per-day detail pages (today + 7 days ahead) and return date → hourly diagram table."""
    today = date.today()
    result: dict[str, BeautifulSoup] = {}

    for offset in range(8):
        target = today + timedelta(days=offset)
        if offset == 0:
            url = f'{_BASE}/{seo}/{loc_id}.html'
        elif offset == 1:
            url = f'{_BASE}/wetter_aktuell/wettervorhersage/morgen/{seo}/{loc_id}.html'
        else:
            url = f'{_BASE}/wetter_aktuell/wettervorhersage/in-{offset}-tagen/{seo}/{loc_id}.html'

        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')
            table = soup.find('table', id='vhs-detail-diagram')
            if table:
                result[str(target)] = table
        except Exception as e:
            logger.warning('Wetter.com detail fetch +%d failed: %s', offset, e)

        time.sleep(db.get_provider_delay('wetter_com'))

    return result


def _apply_detail_extras(entries: list[ForecastEntry], tables: dict) -> None:
    """Fill daily cloud_cover / humidity / pressure means from the hourly diagram tables."""
    extras = {date_str: _parse_detail_extras(table) for date_str, table in tables.items()}
    for entry in entries:
        d = extras.get(entry.forecast_time[:10])
        if d is None:
            continue
        entry.cloud_cover = d.get('cloud_cover')
        entry.humidity = d.get('humidity')
        entry.pressure = d.get('pressure')


# ── hourly parsing ────────────────────────────────────────────────────────────

def _parse_hourly_tables(tables: dict) -> list[ForecastEntry]:
    # Pages overlap into the next day (a 3-hourly page by up to ~21 h), so a
    # timestamp can appear on two pages. The page whose date matches the
    # forecast date wins — it has the finer resolution for that day.
    by_time: dict[str, ForecastEntry] = {}
    for date_str, table in sorted(tables.items()):
        try:
            base = date.fromisoformat(date_str)
        except ValueError:
            continue
        for entry in _parse_hourly_table(table, base):
            if entry.forecast_time[:10] == date_str or entry.forecast_time not in by_time:
                by_time[entry.forecast_time] = entry
    return [by_time[t] for t in sorted(by_time)]


def _parse_hourly_table(table, base: date) -> list[ForecastEntry]:
    """Parse one vhs-detail-diagram table into hourly entries.

    The table holds one column per time step: hourly on near-term pages,
    3-hourly on pages further out. The hour labels (HH:MM) in the time row
    anchor the column → hour mapping and their spacing gives the step size;
    columns past 24:00 roll over into the next day.
    """
    rows = table.find_all('tr')

    hours: list[int] | None = None
    columns: dict[str, list] = {}
    header = ''
    rows_for_header: dict[str, list] = {}

    for row in rows:
        cells = row.find_all('td')
        if len(cells) == 1:
            header = cells[0].get_text(strip=True)
            continue
        if len(cells) < 2:
            continue

        if hours is None:
            anchors: list[tuple[int, int]] = []
            for i, td in enumerate(cells):
                m = re.search(r'(\d{1,2}):00', td.get_text(strip=True))
                if m:
                    h = int(m.group(1))
                    if not anchors:
                        h = h or 24  # page labels midnight as 00:00 = end of day
                    else:
                        while h <= anchors[-1][1]:  # rolled past midnight
                            h += 24
                    anchors.append((i, h))
            if anchors and not header:
                idx0, h0 = anchors[0]
                step = 1
                for i1, h1 in anchors[1:]:
                    di, dh = i1 - idx0, h1 - h0
                    if di > 0 and dh > 0 and dh % di == 0:
                        step = dh // di
                        break
                hours = [h0 + (j - idx0) * step for j in range(len(cells))]
            continue

        rows_for_header.setdefault(header, []).append(cells)

    if hours is None:
        return []

    for key, cells_list in rows_for_header.items():
        if 'Temperatur' in key:
            columns['temperature'] = [_int(td) for td in cells_list[0]]
        elif 'Niederschlagsrisiko' in key:
            columns['precip_probability'] = [_int(td) for td in cells_list[0]]
        elif 'Niederschlagsmenge' in key:
            columns['precip_amount'] = [_float(td) for td in cells_list[0]]
        elif 'Windrichtung' in key and len(cells_list) >= 2:
            columns['wind_direction'] = [(_clean(td.get_text()) or None) for td in cells_list[0]]
            columns['wind_speed'] = [_int(td) for td in cells_list[1]]
        elif 'Luftdruck' in key:
            columns['pressure'] = [_float(td) for td in cells_list[0]]
        elif 'Feucht' in key:
            columns['humidity'] = [_int(td) for td in cells_list[0]]
        elif 'Bew' in key and 'lkung' in key:
            columns['cloud_cover'] = [_cell_octant(td) for td in cells_list[0]]
        elif 'Wetterzustand' in key:
            conds, icons = [], []
            for td in cells_list[0]:
                img = td.find('img')
                conds.append((img.get('title') or img.get('alt')) if img else None)
                icons.append(img.get('data-single-src') or img.get('src') if img else None)
            columns['condition_text'] = conds
            columns['icon_url'] = icons

    def col(name: str, j: int):
        vals = columns.get(name)
        return vals[j] if vals and j < len(vals) else None

    result = []
    for j, hour in enumerate(hours):
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
            pressure=col('pressure', j),
            humidity=col('humidity', j),
            cloud_cover=col('cloud_cover', j),
        )
        if entry.temperature is not None or entry.precip_probability is not None:
            result.append(entry)
    return result


def _cell_octant(td) -> Optional[int]:
    m = re.search(r'(\d+)/8', td.get_text(strip=True))
    return round(int(m.group(1)) * 100 / 8) if m else None


def _parse_detail_extras(table) -> dict:
    """Extract daily mean cloud_cover (%), humidity (%), pressure (hPa) from hourly diagram table."""
    rows = table.find_all('tr')
    data: dict = {}

    for i, row in enumerate(rows[:-1]):
        header = row.get_text(separator=' ', strip=True)
        next_row = rows[i + 1]

        if 'Luftdruck' in header:
            vals = _row_floats(next_row)
            if vals:
                data['pressure'] = round(sum(vals) / len(vals), 1)
        elif 'Relative Feuchte' in header or ('Feucht' in header and '%' in header):
            vals = _row_ints(next_row)
            if vals:
                data['humidity'] = round(sum(vals) / len(vals))
        elif 'Bew' in header and 'lkung' in header:  # Bewölkungsgrad
            vals = _row_octants(next_row)
            if vals:
                data['cloud_cover'] = round(sum(vals) / len(vals))

    return data


def _row_floats(row) -> list[float]:
    result = []
    for td in row.find_all(['td', 'th']):
        m = re.search(r'-?\d+(?:[.,]\d+)?', td.get_text(strip=True))
        if m:
            result.append(float(m.group().replace(',', '.')))
    return result


def _row_ints(row) -> list[int]:
    result = []
    for td in row.find_all(['td', 'th']):
        m = re.search(r'-?\d+', td.get_text(strip=True))
        if m:
            result.append(int(m.group()))
    return result


def _row_octants(row) -> list[float]:
    """Parse 'X/8' cloud cover cells and return percentage values."""
    result = []
    for td in row.find_all(['td', 'th']):
        m = re.search(r'(\d+)/8', td.get_text(strip=True))
        if m:
            result.append(int(m.group(1)) * 100 / 8)
    return result


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
