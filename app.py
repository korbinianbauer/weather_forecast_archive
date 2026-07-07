import hmac
import json
import logging
import os
import secrets
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import date as _date, datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, jsonify, make_response, redirect,
                   render_template, request, session, url_for)

import db
import providers
from poller import _poll_source, poll_all_due

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.json.sort_keys = False
app.secret_key = os.environ.get('SECRET_KEY', '')
if not app.secret_key:
    raise RuntimeError('SECRET_KEY environment variable is not set')

_ADMIN_USER = os.environ.get('BETHER_USER', 'admin')
_ADMIN_PASSWORD = os.environ.get('BETHER_PASSWORD', '')
if not _ADMIN_PASSWORD:
    logger.warning('BETHER_PASSWORD not set — login will always fail')

db.init_db()


# ── file logging ──────────────────────────────────────────────────────────────

_LOG_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_FORMAT = logging.Formatter('%(asctime)s %(levelname)-8s %(name)s — %(message)s')

_file_handler = logging.FileHandler(os.path.join(_LOG_DIR, 'app.log'))
_file_handler.setFormatter(_LOG_FORMAT)
logging.getLogger().addHandler(_file_handler)

_LOG_FILES = {
    'app':    os.path.join(_LOG_DIR, 'app.log'),
    'poll':   os.path.join(_LOG_DIR, 'poll.log'),
    'access': os.path.join(_LOG_DIR, 'access.log'),
}

# Route werkzeug access logs to access.log (dev server; gunicorn writes it directly)
_access_handler = logging.FileHandler(os.path.join(_LOG_DIR, 'access.log'))
_access_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logging.getLogger('werkzeug').addHandler(_access_handler)


# ── poll process management ───────────────────────────────────────────────────

def _ensure_poll_running():
    """Start poll.py if it is not already running."""
    pidfile = os.path.join(_LOG_DIR, 'poll.pid')
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return  # already running
    except (OSError, ValueError):
        pass
    log_path = os.path.join(_LOG_DIR, 'poll.log')
    with open(log_path, 'a') as log_fh:
        subprocess.Popen(
            [sys.executable, os.path.join(_LOG_DIR, 'poll.py')],
            cwd=_LOG_DIR,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    logger.info('Started poll.py process')


_ensure_poll_running()


# ── CSRF ──────────────────────────────────────────────────────────────────────

def _csrf_token() -> str:
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_hex(16)
    return session['_csrf']

app.jinja_env.globals['csrf_token'] = _csrf_token


def csrf_protect(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = (request.form.get('_csrf') or
                 request.headers.get('X-CSRF-Token') or '')
        expected = session.get('_csrf', '')
        if not expected or not hmac.compare_digest(token, expected):
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── rate limiting ─────────────────────────────────────────────────────────────

_login_attempts: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_WINDOW = 300
_RATE_LIMIT_MAX    = 10


def _is_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _login_attempts[ip] if now - t < _RATE_LIMIT_WINDOW]
    _login_attempts[ip] = recent
    return len(recent) >= _RATE_LIMIT_MAX


def _record_failed_login(ip: str) -> None:
    _login_attempts[ip].append(time.monotonic())


# ── auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        ip = request.remote_addr or ''
        if _is_rate_limited(ip):
            error = 'Too many attempts. Try again later.'
        else:
            u = request.form.get('username', '')
            p = request.form.get('password', '')
            user_ok = hmac.compare_digest(u, _ADMIN_USER)
            pass_ok = bool(_ADMIN_PASSWORD) and hmac.compare_digest(p, _ADMIN_PASSWORD)
            if user_ok and pass_ok:
                session['logged_in'] = True
                next_url = request.args.get('next') or url_for('index')
                if not next_url.startswith('/') or next_url.startswith('//'):
                    next_url = url_for('index')
                return redirect(next_url)
            _record_failed_login(ip)
            error = 'Invalid username or password.'
    return render_template('login.html', error=error)


@app.route('/logout', methods=['POST'])
@csrf_protect
def logout():
    session.clear()
    return redirect(url_for('index'))


# ── template helpers ──────────────────────────────────────────────────────────

@app.template_filter('dt')
def fmt_dt(iso):
    if not iso:
        return '—'
    try:
        return datetime.fromisoformat(iso).strftime('%d.%m.%Y %H:%M')
    except ValueError:
        return iso


@app.template_filter('opt')
def fmt_opt(value, suffix=''):
    return f'{value}{suffix}' if value is not None else '—'


def _load_provider_colors() -> dict[str, str]:
    try:
        colors = json.loads(db.get_setting('provider_colors', '{}'))
    except json.JSONDecodeError:
        colors = {}
    defaults = {'wetter_com': '#3b82f6', 'meteoblue': '#22c55e', 'median': '#111827', 'dwd': '#dc2626'}
    return {**defaults, **colors}


# Providers delivering measured ground truth instead of forecasts (e.g. DWD).
# They are excluded from the forecast median and drawn specially in the plots.
_OBSERVATION_PROVIDERS = {p.name for p in providers.all_providers()
                          if getattr(p, 'is_observation', False)}


@app.context_processor
def inject_provider_colors():
    return {'provider_colors': _load_provider_colors()}


# ── provider color helpers ────────────────────────────────────────────────────

_DEFAULT_COLOR_HEX = '#a855f7'


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    return f'rgba({r},{g},{b},{alpha:.2f})'


# ── routes ────────────────────────────────────────────────────────────────────

def _fmt_date(s: str) -> str:
    return datetime.strptime(s, '%Y-%m-%d').strftime('%-d %b %Y')


def _current_weather(location_id: int, provider: str) -> dict | None:
    """Best 'now' snapshot for the overview card: the hourly entry closest to
    the current time from the newest poll, falling back to today's daily row."""
    now = datetime.now()
    best = None
    for e in db.get_latest_forecast(location_id, provider=provider, granularity='hourly'):
        try:
            t = datetime.fromisoformat(e['forecast_time'])
        except ValueError:
            continue
        diff = abs((t - now).total_seconds())
        if best is None or diff < best[0]:
            best = (diff, e)
    if best and best[0] <= 3 * 3600:
        return best[1]
    today = now.date().isoformat()
    rows = db.get_latest_forecast_per_date(location_id, provider, today, today)
    return rows[0] if rows else None


@app.route('/')
def index():
    locs = db.get_locations()
    provider_labels = {p.name: p.display_name for p in providers.all_providers()}

    default_start = _date.today().isoformat()
    date_start = request.args.get('start', default_start)

    try:
        start_dt = _date.fromisoformat(date_start)
    except ValueError:
        start_dt = _date.today()
        date_start = start_dt.isoformat()

    # Always show exactly 16 days starting from date_start; empty cells where no data exists
    window_dates = [(start_dt + timedelta(days=i)).isoformat() for i in range(16)]
    date_end = window_dates[-1]

    location_data = []
    for loc in locs:
        sources = db.get_location_sources(loc['id'])
        rows = []

        configured_providers = {s['provider'] for s in sources}
        for source in sources:
            # Per date, the most recent archived forecast — so past days keep
            # showing the last forecast that covered them.
            entries = db.get_latest_forecast_per_date(
                loc['id'], source['provider'], window_dates[0], date_end)
            by_date = {e['forecast_time'][:10]: e for e in entries}
            rows.append({
                'provider': source['provider'],
                'enabled': source.get('enabled', 1),
                'fetched_at': max((e['fetched_at'] for e in entries), default=None),
                'by_date': by_date,
            })

        available_providers = [p for p in providers.all_providers()
                               if p.name not in configured_providers]
        # Overview card: prefer a forecast provider — observation data (DWD)
        # lags too far behind to represent "now".
        first_provider = next(
            (s['provider'] for s in sources if s['provider'] not in _OBSERVATION_PROVIDERS),
            sources[0]['provider'] if sources else None)
        location_data.append({
            'location': loc,
            'all_dates': window_dates,
            'rows': rows,
            'available_providers': available_providers,
            'pressure_range': db.get_metric_range(loc['id'], 'pressure'),
            'current': _current_weather(loc['id'], first_provider) if first_provider else None,
            'current_provider': first_provider,
        })

    r = make_response(render_template(
        'index.html',
        location_data=location_data,
        provider_labels=provider_labels,
        all_providers=providers.all_providers(),
        date_start=date_start,
        date_end=date_end,
        date_end_display=_fmt_date(date_end),
        polling_location_ids=list(_active_refreshes),
        today_iso=_date.today().isoformat(),
    ))
    r.headers['Cache-Control'] = 'no-store'
    return r


@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    provider_name = request.args.get('provider', '')
    if len(q) < 2 or provider_name not in providers.REGISTRY:
        return jsonify([])
    results = providers.get(provider_name).search(q)
    return jsonify([
        {
            'name': r.name,
            'provider_location_id': r.provider_location_id,
            'latitude': r.latitude,
            'longitude': r.longitude,
            'extra': r.extra,
        }
        for r in results[:8]
    ])


@app.route('/dwd_stations')
def dwd_stations():
    """Nearest DWD stations (with distance) for the add-location dialog."""
    try:
        lat = float(request.args['lat'])
        lon = float(request.args['lon'])
    except (KeyError, TypeError, ValueError):
        return jsonify([])
    try:
        stations = providers.get('dwd').nearest_stations(lat, lon, limit=5)
    except Exception as e:
        logger.error('DWD station lookup failed: %s', e)
        return jsonify([])
    return jsonify([
        {
            'name': s['name'],
            'provider_location_id': s['id'],
            'latitude': s['latitude'],
            'longitude': s['longitude'],
            'distance_km': s['distance_km'],
            'extra': {
                'title': f"{s['name']} ({s['state']}) · {s['distance_km']:.1f} km",
                'station_name': s['name'],
                'state': s['state'],
                'height': s['height'],
                'distance_km': s['distance_km'],
            },
        }
        for s in stations
    ])


@app.route('/add', methods=['POST'])
@login_required
@csrf_protect
def add_location():
    name = request.form.get('name', '').strip()
    latitude = float(request.form.get('latitude') or 0)
    longitude = float(request.form.get('longitude') or 0)
    provider_names = request.form.getlist('provider')
    provider_location_ids = request.form.getlist('provider_location_id')
    metadatas = request.form.getlist('metadata')

    if not name or not provider_names:
        return redirect(url_for('index'))

    location_id = db.add_location(name, latitude, longitude)

    for pname, plid, meta_str in zip(provider_names, provider_location_ids, metadatas):
        if pname not in providers.REGISTRY or not plid:
            continue
        try:
            metadata = json.loads(meta_str or '{}')
        except ValueError:
            metadata = {}
        db.add_location_source(location_id, pname, plid, metadata)

    _active_refreshes.add(location_id)

    def _initial_poll():
        for source in db.get_location_sources(location_id):
            try:
                _poll_source(location_id, source)
            except Exception as e:
                logger.error('Initial poll failed for %s: %s', source['provider'], e)
        _active_refreshes.discard(location_id)

    threading.Thread(target=_initial_poll, daemon=True).start()
    return redirect(url_for('index'))


# DB field → metric key used by the chart JS (shared by both trace builders)
_SIMPLE_METRICS = [
    ('sunshine_hours',     'sunshine_hours'),
    ('precip_probability', 'precipitation_precip_probability'),
    ('precip_amount',      'precipitation_precip_amount'),
    ('wind_speed',         'wind_wind_speed'),
    ('cloud_cover',        'cloud_cloud_cover'),
    ('pressure',           'pressure_pressure'),
    ('humidity',           'humidity_humidity'),
]

_MEDIAN_FIELDS = ['temperature', 'temp_max', 'temp_min'] + [f for f, _ in _SIMPLE_METRICS]


def _median_entry(entries: list[dict]) -> dict:
    """Field-wise median over one entry per provider; only fields with ≥2 values."""
    out = {}
    for field in _MEDIAN_FIELDS:
        vals = [e.get(field) for e in entries if e.get(field) is not None]
        if len(vals) >= 2:
            out[field] = round(statistics.median(vals), 1)
    return out


def _median_evolution_rows(rows: list[dict]) -> list[dict]:
    """Synthesize 'median' pseudo-provider rows for the daily evolution plot.

    Providers within one poll run are fetched minutes apart, so rows are
    clustered by fetched_at (≤30 min chain gap); each cluster with ≥2
    providers yields one median row."""
    rows = [r for r in rows if r['provider'] not in _OBSERVATION_PROVIDERS]
    if len({r['provider'] for r in rows}) < 2:
        return []

    clusters: list[list[dict]] = []
    prev_ts = None
    for r in sorted(rows, key=lambda r: r['fetched_at']):
        ts = datetime.fromisoformat(r['fetched_at'])
        if prev_ts is None or (ts - prev_ts).total_seconds() > 1800:
            clusters.append([])
        clusters[-1].append(r)
        prev_ts = ts

    result = []
    for cluster in clusters:
        latest_per_provider = {r['provider']: r for r in cluster}  # sorted → last wins
        if len(latest_per_provider) < 2:
            continue
        med = _median_entry(list(latest_per_provider.values()))
        if med:
            result.append({
                'provider': 'median',
                'granularity': 'daily',
                'fetched_at': max(r['fetched_at'] for r in latest_per_provider.values()),
                **med,
            })
    return result


def _build_evolution_traces(rows: list[dict], provider_labels: dict) -> list[dict]:
    if not rows:
        return []

    groups: dict[tuple, list] = defaultdict(list)
    for row in rows:
        groups[(row['provider'], row['granularity'])].append(row)
    for key in groups:
        groups[key].sort(key=lambda r: r['fetched_at'])

    provider_colors = _load_provider_colors()

    # Observations are archived once per forecast_time, so they would show as
    # a lone point — stretch the measured value into a horizontal reference
    # line across the full poll range instead.
    x_min = min(r['fetched_at'] for r in rows)
    x_max = max(r['fetched_at'] for r in rows)
    obs_labels: set[str] = set()

    all_traces = []
    legend_shown: set[str] = set()

    for (provider, granularity), entries in sorted(groups.items()):
        if provider in _OBSERVATION_PROVIDERS:
            obs_labels.add(provider_labels.get(provider, provider))
            if x_max > x_min:
                last = entries[-1]
                entries = [{**last, 'fetched_at': x_min},
                           {**last, 'fetched_at': x_max}]
        hex_color  = provider_colors.get(provider, _DEFAULT_COLOR_HEX)
        color      = _rgba(hex_color, 1.0)
        fill_color = _rgba(hex_color, 0.15)
        label      = provider_labels.get(provider, provider)
        xs = [e['fetched_at'] for e in entries]

        ymx = [e.get('temp_max')    for e in entries]
        ymn = [e.get('temp_min')    for e in entries]
        yt  = [e.get('temperature') for e in entries]
        has_max = any(v is not None for v in ymx)
        has_min = any(v is not None for v in ymn)
        has_t   = any(v is not None for v in yt)

        if has_max and has_min:
            all_traces.append({
                'x': xs, 'y': ymx,
                'type': 'scatter', 'mode': 'lines',
                'line': {'width': 0}, 'showlegend': False,
                'hoverinfo': 'skip', 'metric': 'temperature',
                'legendgroup': label,
            })
            all_traces.append({
                'x': xs, 'y': ymn,
                'type': 'scatter', 'mode': 'lines',
                'fill': 'tonexty', 'fillcolor': fill_color,
                'line': {'width': 0}, 'showlegend': False,
                'hoverinfo': 'skip', 'metric': 'temperature',
                'legendgroup': label,
            })

        if has_t:
            show = label not in legend_shown
            if show:
                legend_shown.add(label)
            all_traces.append({
                'x': xs, 'y': yt,
                'type': 'scatter', 'mode': 'lines+markers',
                'name': label, 'showlegend': show,
                'line': {'color': color, 'width': 2},
                'marker': {'size': 6, 'color': color},
                'metric': 'temperature', 'legendgroup': label,
            })
        elif has_max or has_min:
            show = label not in legend_shown
            if show:
                legend_shown.add(label)
            all_traces.append({
                'x': xs, 'y': ymx,
                'type': 'scatter', 'mode': 'lines+markers',
                'name': label, 'showlegend': show,
                'line': {'color': color, 'width': 2},
                'marker': {'size': 6, 'color': color, 'symbol': 'triangle-up'},
                'metric': 'temperature', 'legendgroup': label,
            })
            all_traces.append({
                'x': xs, 'y': ymn,
                'type': 'scatter', 'mode': 'lines+markers',
                'name': label, 'showlegend': False,
                'line': {'color': color, 'width': 2},
                'marker': {'size': 6, 'color': color, 'symbol': 'triangle-down'},
                'metric': 'temperature', 'legendgroup': label,
            })
        else:
            show = label not in legend_shown
            if show:
                legend_shown.add(label)
            all_traces.append({
                'x': xs, 'y': [None] * len(xs),
                'type': 'scatter', 'mode': 'lines+markers',
                'name': label, 'showlegend': show,
                'line': {'color': color, 'width': 2},
                'marker': {'size': 6, 'color': color},
                'metric': 'temperature', 'legendgroup': label,
            })

        for field, metric_key in _SIMPLE_METRICS:
            ys = [e.get(field) for e in entries]
            show = label not in legend_shown
            if show:
                legend_shown.add(label)
            all_traces.append({
                'x': xs, 'y': ys,
                'type': 'scatter', 'mode': 'lines+markers',
                'name': label, 'showlegend': show,
                'line': {'color': color, 'width': 2},
                'marker': {'size': 6, 'color': color},
                'metric': metric_key, 'legendgroup': label,
            })

    for t in all_traces:
        if t.get('legendgroup') in obs_labels and t.get('line', {}).get('width'):
            t['line']['dash'] = 'dot'

    return all_traces


def _build_hourly_traces(rows: list[dict], provider_labels: dict) -> list[dict]:
    """One line per archived poll, showing the hourly forecast curve for the
    target date; older polls are drawn progressively more transparent."""
    if not rows:
        return []

    by_provider: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        by_provider[row['provider']][row['fetched_at']].append(row)

    # Observation hours are archived incrementally (each hour once, at the
    # first poll that saw it) — merge them into a single ground-truth curve.
    for p in _OBSERVATION_PROVIDERS & set(by_provider):
        if len(by_provider[p]) > 1:
            merged: dict[str, dict] = {}
            for fetched_at in sorted(by_provider[p]):
                for e in by_provider[p][fetched_at]:
                    merged[e['forecast_time']] = e
            by_provider[p] = {max(by_provider[p]): [merged[t] for t in sorted(merged)]}

    # Median pseudo-provider: one curve over the newest run of each forecast
    # provider, at forecast times where ≥2 providers have a value.
    forecast_providers = set(by_provider) - _OBSERVATION_PROVIDERS
    if len(forecast_providers) >= 2:
        newest_runs = {p: max(runs) for p, runs in by_provider.items()
                       if p in forecast_providers}
        by_time: dict[str, list] = defaultdict(list)
        for p, fetched_at in newest_runs.items():
            for e in by_provider[p][fetched_at]:
                by_time[e['forecast_time']].append(e)
        med_entries = []
        for t in sorted(by_time):
            med = _median_entry(by_time[t])
            if med:
                med_entries.append({'forecast_time': t, **med})
        if med_entries:
            by_provider['median'][max(newest_runs.values())] = med_entries

    provider_colors = _load_provider_colors()
    metrics = [('temperature', 'temperature')] + _SIMPLE_METRICS

    traces = []
    for provider in sorted(by_provider):
        label = provider_labels.get(provider, provider)
        hex_color = provider_colors.get(provider, _DEFAULT_COLOR_HEX)
        polls = sorted(by_provider[provider])
        n = len(polls)
        for i, fetched_at in enumerate(polls):
            entries = sorted(by_provider[provider][fetched_at],
                             key=lambda e: e['forecast_time'])
            newest = i == n - 1
            alpha = 1.0 if newest else 0.15 + 0.55 * (i + 1) / n
            color = _rgba(hex_color, alpha)
            xs = [e['forecast_time'] for e in entries]
            name = f"{label} · {fetched_at[:16].replace('T', ' ')}"
            for field, metric_key in metrics:
                ys = [e.get(field) for e in entries]
                if not any(v is not None for v in ys):
                    continue
                line = {'color': color, 'width': 2 if newest else 1.5}
                if provider in _OBSERVATION_PROVIDERS:
                    line['dash'] = 'dot'
                traces.append({
                    'x': xs, 'y': ys,
                    'type': 'scatter', 'mode': 'lines+markers',
                    'name': name, 'showlegend': newest,
                    'line': line,
                    'marker': {'size': 5 if newest else 3, 'color': color},
                    'metric': metric_key, 'legendgroup': label,
                })
    return traces


_EVAL_VARIABLES = [
    ('temperature',        'temperature_temperature'),
    ('temp_max',           'temp_max_temperature'),
    ('temp_min',           'temp_min_temperature'),
    ('precip_probability', 'precipitation_precip_probability'),
    ('precip_amount',      'precipitation_precip_amount'),
    ('wind_speed',         'wind_wind_speed'),
    ('sunshine_hours',     'sunshine_hours_sunshine'),
    ('cloud_cover',        'cloud_cloud_cover'),
    ('pressure',           'pressure_pressure'),
    ('humidity',           'humidity_humidity'),
]


_VAR_ORDER = [
    'temp_max', 'temp_min', 'temperature',
    'sunshine_hours',
    'precip_probability', 'precip_amount',
    'wind_speed',
    'cloud_cover',
    'pressure',
    'humidity',
]

_VAR_LABELS = {
    'temp_max': 'Max Temp',
    'temp_min': 'Min Temp',
    'temperature': 'Avg Temp',
    'sunshine_hours': 'Sunshine',
    'precip_probability': 'Precip Prob',
    'precip_amount': 'Precip Amount',
    'wind_speed': 'Wind Speed',
    'cloud_cover': 'Cloud Cover',
    'pressure': 'Pressure',
    'humidity': 'Humidity',
}


def _eval_var_traces(
    forecast_rows: list[dict],
    gt_values: dict[tuple[int, str], dict[str, float | None]],
    variable: str,
    selected: list[str] | None,
    provider_labels: dict[str, str],
    colors: dict[str, str],
) -> dict[str, list[dict]]:
    """Build Plotly traces for a single variable — returns {rmse: ..., bias: ...}."""
    err: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    med_collect: dict[tuple[int, int, str], dict[str, float]] = {}

    for r in forecast_rows:
        lid, dt = r['_loc_id'], r['forecast_time'][:10]
        gt_val = gt_values.get((lid, dt), {}).get(variable)
        if gt_val is None:
            continue
        fv = r.get(variable)
        if fv is None:
            continue
        try:
            lead = (_date.fromisoformat(dt) - _date.fromisoformat(r['fetched_at'][:10])).days
        except (ValueError, TypeError):
            continue
        if lead < 0:
            continue
        err[r['provider']][lead].append(fv - gt_val)
        key = (lead, lid, dt)
        if key not in med_collect:
            med_collect[key] = {}
        med_collect[key][r['provider']] = fv

    med_err: dict[int, list[float]] = defaultdict(list)
    for (lead, lid, dt), pv in med_collect.items():
        gt_val = gt_values.get((lid, dt), {}).get(variable)
        if gt_val is None:
            continue
        vs = [v for v in pv.values() if v is not None]
        if len(vs) >= 2:
            med_err[lead].append(statistics.median(vs) - gt_val)

    def _build(metric: str) -> list[dict]:
        traces: list[dict] = []
        for prov in sorted(err):
            if prov in _OBSERVATION_PROVIDERS:
                continue
            if selected and prov not in selected:
                continue
            label = provider_labels.get(prov, prov)
            color = colors.get(prov, _DEFAULT_COLOR_HEX)
            leads = sorted(err[prov])
            if not leads:
                continue
            if metric == 'rmse':
                ys = [statistics.mean(v ** 2 for v in err[prov][l]) ** 0.5 for l in leads]
            else:
                ys = [statistics.mean(err[prov][l]) for l in leads]
            traces.append({
                'x': leads, 'y': ys,
                'type': 'scatter', 'mode': 'lines+markers',
                'name': label,
                'line': {'color': color, 'width': 2},
                'marker': {'size': 6, 'color': color},
            })
        if len(med_err) >= 2:
            leads = sorted(med_err)
            if metric == 'rmse':
                ys = [statistics.mean(v ** 2 for v in med_err[l]) ** 0.5 for l in leads]
            else:
                ys = [statistics.mean(med_err[l]) for l in leads]
            mc = colors.get('median', '#111827')
            traces.append({
                'x': leads, 'y': ys,
                'type': 'scatter', 'mode': 'lines+markers',
                'name': 'Median',
                'line': {'color': mc, 'width': 2, 'dash': 'dash'},
                'marker': {'size': 6, 'color': mc},
            })
        return traces

    return {'rmse': _build('rmse'), 'bias': _build('bias')}


@app.route('/api/evaluation')
@login_required
def api_evaluation():
    location_param = request.args.get('location_id', 'all')
    date_start = request.args.get('date_start', (_date.today() - timedelta(days=365)).isoformat())
    date_end   = request.args.get('date_end', _date.today().isoformat())
    selected   = request.args.getlist('providers') or None

    # Determine locations
    if not location_param or location_param == 'all':
        locations = db.get_locations(show_hidden=True)
        if not locations:
            return jsonify({'results': {}, 'warning': 'No locations.'})
        location_ids = [loc['id'] for loc in locations]
    else:
        try:
            loc_id = int(location_param)
        except (ValueError, TypeError):
            return jsonify({'error': 'invalid location_id'}), 400
        loc = db.get_location(loc_id)
        if not loc:
            return jsonify({'error': 'not found'}), 404
        location_ids = [loc_id]

    # Fetch data
    all_rows: list[dict] = []
    for lid in location_ids:
        rows = db.get_daily_entries_in_range(lid, date_start, date_end, providers=None)
        for r in rows:
            r['_loc_id'] = lid
        all_rows.extend(rows)

    if not all_rows:
        return jsonify({'results': {}, 'warning': 'No data in selected range.'})

    dwd_rows      = [r for r in all_rows if r['provider'] == 'dwd']
    forecast_rows = [r for r in all_rows if r['provider'] not in _OBSERVATION_PROVIDERS]
    if not forecast_rows:
        return jsonify({'results': {}, 'warning': 'No forecast data available.'})

    # ── ground truth per (location_id, target_date) for ALL variables ───────
    by_loc_date: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for r in all_rows:
        by_loc_date[(r['_loc_id'], r['forecast_time'][:10])].append(r)

    gt_values: dict[tuple[int, str], dict[str, float | None]] = {}
    for (lid, dt), date_rows in sorted(by_loc_date.items()):
        # 1) DWD observation (has all variables)
        dwd_row = next((r for r in date_rows if r['provider'] == 'dwd'), None)
        if dwd_row is not None:
            gt_values[(lid, dt)] = {var: dwd_row.get(var) for var in _VAR_ORDER}
            continue
        # 2) median of latest forecasts per variable
        latest: dict[str, dict] = {}
        for r in date_rows:
            if r['provider'] in _OBSERVATION_PROVIDERS:
                continue
            p = r['provider']
            if p not in latest or r['fetched_at'] > latest[p]['fetched_at']:
                latest[p] = r
        if not latest:
            continue
        row_vals: dict[str, float | None] = {}
        for var in _VAR_ORDER:
            vals = [r.get(var) for r in latest.values() if r.get(var) is not None]
            if len(vals) >= 2:
                row_vals[var] = statistics.median(vals)
            elif len(vals) == 1:
                row_vals[var] = vals[0]
        if row_vals:
            gt_values[(lid, dt)] = row_vals

    if not gt_values:
        return jsonify({'results': {}, 'warning': 'Could not determine ground truth.'})

    # ── compute traces per variable ─────────────────────────────────────────
    provider_labels = {p.name: p.display_name for p in providers.all_providers()}
    provider_labels['median'] = 'Median'
    colors = _load_provider_colors()

    results: dict[str, dict] = {}
    for var in _VAR_ORDER:
        tr = _eval_var_traces(forecast_rows, gt_values, var, selected,
                              provider_labels, colors)
        rmse = tr.get('rmse', [])
        bias = tr.get('bias', [])
        if rmse or bias:
            results[var] = {}
            if rmse:
                results[var]['rmse'] = {'traces': rmse}
            if bias:
                results[var]['bias'] = {'traces': bias}

    # ── ground truth description ────────────────────────────────────────────
    dwd_loc_dates = {(r['_loc_id'], r['forecast_time'][:10]) for r in dwd_rows}
    gt_dwd   = sum(1 for k in gt_values if k in dwd_loc_dates)
    gt_total = len(gt_values)
    n_locs   = len(location_ids)
    if gt_dwd:
        if gt_dwd == gt_total:
            gt_src = 'DWD weather station observations'
        else:
            gt_src = f'DWD ({gt_dwd}/{gt_total} location-days) + median forecast'
    else:
        gt_src = 'Median of latest forecasts (no weather station)'
    if n_locs > 1:
        gt_src += f' · {n_locs} locations'

    return jsonify({
        'results': results,
        'ground_truth_source': gt_src,
        'has_dwd_data': bool(dwd_rows),
        'n_dates': gt_total,
        'n_locations': n_locs,
        'provider_labels': provider_labels,
        'provider_colors': colors,
        'var_labels': _VAR_LABELS,
    })


@app.route('/api/location/<int:location_id>/evolution')
def api_evolution(location_id):
    location = db.get_location(location_id)
    if not location:
        return jsonify({'error': 'not found'}), 404
    target_date = request.args.get('date', '')
    if not target_date:
        return jsonify({'error': 'missing date'}), 400
    sources = db.get_location_sources(location_id)
    all_provider_names = [s['provider'] for s in sources]
    provider_labels_map = {p.name: p.display_name for p in providers.all_providers()}
    provider_labels_map['median'] = 'Median'
    url_providers = request.args.getlist('providers')
    default_provider = url_providers[0] if len(url_providers) == 1 else None
    mode = 'hourly' if request.args.get('mode') == 'hourly' else 'daily'
    if mode == 'hourly':
        rows = db.get_hourly_runs(location_id, target_date, all_provider_names)
        traces = _build_hourly_traces(rows, provider_labels_map)
    else:
        rows = db.get_forecast_evolution(location_id, target_date, all_provider_names)
        rows = rows + _median_evolution_rows(rows)
        traces = _build_evolution_traces(rows, provider_labels_map)
    # Draw the median as a dashed line so it stands out as synthetic
    for t in traces:
        if t.get('legendgroup') == 'Median' and t.get('line', {}).get('width'):
            t['line']['dash'] = 'dash'
    return jsonify({
        'traces': traces,
        'target_date': target_date,
        'mode': mode,
        'provider_labels': provider_labels_map,
        'default_provider': default_provider,
        'pressure_range': db.get_metric_range(location_id, 'pressure'),
    })


@app.route('/location/<int:location_id>/add_source', methods=['POST'])
@login_required
@csrf_protect
def add_location_source_route(location_id):
    location = db.get_location(location_id)
    if not location:
        return redirect(url_for('index'))

    provider_names = request.form.getlist('provider')
    provider_location_ids = request.form.getlist('provider_location_id')
    metadatas = request.form.getlist('metadata')

    for pname, plid, meta_str in zip(provider_names, provider_location_ids, metadatas):
        if pname not in providers.REGISTRY or not plid:
            continue
        try:
            metadata = json.loads(meta_str or '{}')
        except ValueError:
            metadata = {}
        db.add_location_source(location_id, pname, plid, metadata)
        source = next((s for s in db.get_location_sources(location_id) if s['provider'] == pname), None)
        if source:
            _active_refreshes.add(location_id)
            def _bg(loc_id=location_id, src=source):
                try:
                    _poll_source(loc_id, src)
                finally:
                    _active_refreshes.discard(loc_id)
            threading.Thread(target=_bg, daemon=True).start()

    return redirect(url_for('settings_page', tab='locations'))


@app.route('/location/<int:location_id>/source/<provider>/toggle_enabled', methods=['POST'])
@login_required
@csrf_protect
def toggle_source_enabled(location_id, provider):
    sources = db.get_location_sources(location_id)
    source = next((s for s in sources if s['provider'] == provider), None)
    if source:
        db.set_source_enabled(location_id, provider, not source.get('enabled', 1))
    return {'ok': True}



_active_refreshes: set[int] = set()

@app.route('/location/<int:location_id>/refresh', methods=['POST'])
@login_required
@csrf_protect
def refresh_location(location_id):
    if location_id in _active_refreshes:
        return jsonify({'status': 'running'}), 202
    sources = db.get_location_sources(location_id)
    _active_refreshes.add(location_id)

    def _run():
        for source in sources:
            if not source.get('enabled', 1):
                continue
            try:
                _poll_source(location_id, source)
            except Exception as e:
                logger.error('Refresh failed: %s', e)
        _active_refreshes.discard(location_id)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'started'}), 202


@app.route('/location/<int:location_id>/refresh/status')
@login_required
def refresh_status(location_id):
    running = location_id in _active_refreshes
    return jsonify({'running': running})


@app.route('/location/<int:location_id>/delete', methods=['POST'])
@login_required
@csrf_protect
def delete_location(location_id):
    db.delete_location(location_id)
    next_url = request.form.get('next', url_for('index'))
    if not next_url.startswith('/') or next_url.startswith('//'):
        next_url = url_for('index')
    return redirect(next_url)


@app.route('/location/<int:location_id>/toggle_hidden', methods=['POST'])
@login_required
@csrf_protect
def toggle_location_hidden(location_id):
    db.toggle_location_hidden(location_id)
    return {'ok': True}


@app.route('/settings/locations/reorder', methods=['POST'])
@login_required
@csrf_protect
def reorder_locations():
    data = request.get_json(silent=True) or {}
    order = data.get('order', [])
    for i, loc_id in enumerate(order):
        db.update_location_sort_order(int(loc_id), i)
    return jsonify({'ok': True})


# ── settings ──────────────────────────────────────────────────────────────────

_DB_PAGE_SIZE = 200


def _db_tables() -> list[str]:
    with db.get_db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]


@app.route('/settings')
@login_required
def settings_page():
    tab = request.args.get('tab', 'schedule')

    all_settings  = db.get_all_settings()
    all_locations = db.get_locations(show_hidden=True)
    all_providers_list = providers.all_providers()
    provider_labels    = {p.name: p.display_name for p in all_providers_list}

    location_sources: dict[int, list] = {}
    available_providers_by_loc: dict[int, list] = {}
    for loc in all_locations:
        srcs = db.get_location_sources(loc['id'])
        location_sources[loc['id']] = srcs
        configured = {s['provider'] for s in srcs}
        available_providers_by_loc[loc['id']] = [p for p in all_providers_list if p.name not in configured]

    # Separate DWD bulk-imported locations (metadata flag set by /locations/import_dwd);
    # locations where the user attached a DWD source manually stay in the regular list.
    regular_locations = []
    dwd_locations = []
    bulk_imported_dwd_ids = db.get_bulk_imported_dwd_ids()
    for loc in all_locations:
        srcs = location_sources.get(loc['id'], [])
        is_dwd_import = any(
            s['provider'] == 'dwd' and s['provider_location_id'] in bulk_imported_dwd_ids
            for s in srcs
        )
        if is_dwd_import:
            dwd_locations.append(loc)
        else:
            regular_locations.append(loc)

    # Load DWD stations for the import UI. Any existing DWD source (bulk or manual)
    # counts as imported so the picker never creates a duplicate location.
    dwd_stations_by_state: list[tuple[str, list[dict]]] = []
    if tab == 'locations':
        try:
            dwd_stations = providers.get('dwd').all_stations()
        except Exception:
            logger.exception('Could not load DWD station list')
            dwd_stations = []
        used_dwd_ids = db.get_imported_dwd_ids()
        state_map: dict[str, list[dict]] = {}
        for s in dwd_stations:
            state_map.setdefault(s['state'], []).append(s)
        for state in sorted(state_map, key=lambda st: -len(state_map[st])):
            stations = [dict(s, imported=s['id'] in used_dwd_ids)
                        for s in state_map[state]]
            dwd_stations_by_state.append((state, stations))

    try:
        stored_colors = json.loads(all_settings.get('provider_colors', '{}'))
    except json.JSONDecodeError:
        stored_colors = {}
    try:
        stored_delays = json.loads(all_settings.get('provider_delays', '{}'))
    except json.JSONDecodeError:
        stored_delays = {}

    all_db_tables = _db_tables()
    db_tables_summary = []
    with db.get_db() as conn:
        for t in all_db_tables:
            count = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            cols  = [r[1] for r in conn.execute(f'PRAGMA table_info({t})')]
            db_tables_summary.append({'name': t, 'count': count, 'cols': cols})

    db_active_table = request.args.get('db_table') if tab == 'database' else None
    db_rows = db_cols = db_total = db_filtered_total = None
    db_page    = max(0, int(request.args.get('db_page', 0)))
    db_filters = {k[2:]: v for k, v in request.args.items()
                  if k.startswith('f_') and v.strip()}

    if db_active_table and db_active_table in all_db_tables:
        with db.get_db() as conn:
            db_cols  = [r[1] for r in conn.execute(f'PRAGMA table_info({db_active_table})')]
            db_total = conn.execute(f'SELECT COUNT(*) FROM {db_active_table}').fetchone()[0]

            where_parts, params = [], []
            for col, val in db_filters.items():
                if col in db_cols:
                    where_parts.append(f'{col} LIKE ?')
                    params.append(f'%{val}%')
            where = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''

            db_filtered_total = conn.execute(
                f'SELECT COUNT(*) FROM {db_active_table} {where}', params
            ).fetchone()[0]
            db_rows = [list(r) for r in conn.execute(
                f'SELECT * FROM {db_active_table} {where} LIMIT ? OFFSET ?',
                params + [_DB_PAGE_SIZE, db_page * _DB_PAGE_SIZE],
            ).fetchall()]

    # Pre-build DB pagination/table URLs so the template doesn't need **-unpacking
    def _db_url(**extra):
        base = {'tab': 'database'}
        if db_active_table:
            base['db_table'] = db_active_table
        for k, v in db_filters.items():
            base['f_' + k] = v
        base.update(extra)
        return url_for('settings_page', **base)

    db_table_urls  = {t['name']: _db_url(db_table=t['name'], db_page=0) for t in db_tables_summary}
    db_prev_url    = _db_url(db_page=db_page - 1) if db_page > 0 else None
    db_next_url    = _db_url(db_page=db_page + 1) if db_filtered_total and (db_page + 1) * _DB_PAGE_SIZE < db_filtered_total else None
    db_clear_url   = _db_url(db_page=0) if db_filters else None

    server_tz = datetime.now().astimezone().tzname()
    today_iso = _date.today().isoformat()
    year_ago_iso = (_date.today() - timedelta(days=365)).isoformat()

    return render_template(
        'settings.html',
        tab=tab,
        server_tz=server_tz,
        today_iso=today_iso,
        year_ago_iso=year_ago_iso,
        all_settings=all_settings,
        all_locations=all_locations,
        regular_locations=regular_locations,
        dwd_locations=dwd_locations,
        location_sources=location_sources,
        available_providers_by_loc=available_providers_by_loc,
        all_providers=all_providers_list,
        provider_labels=provider_labels,
        stored_colors=stored_colors,
        stored_delays=stored_delays,
        dwd_stations_by_state=dwd_stations_by_state,
        db_tables=db_tables_summary,
        db_active_table=db_active_table,
        db_rows=db_rows,
        db_cols=db_cols,
        db_total=db_total,
        db_filtered_total=db_filtered_total,
        db_filters=db_filters,
        db_page=db_page,
        db_page_size=_DB_PAGE_SIZE,
        db_table_urls=db_table_urls,
        db_prev_url=db_prev_url,
        db_next_url=db_next_url,
        db_clear_url=db_clear_url,
    )


_import_dwd_lock = threading.Lock()


@app.route('/locations/import_dwd', methods=['POST'])
@login_required
@csrf_protect
def import_dwd_stations():
    station_ids = request.form.getlist('station_id')
    if not station_ids:
        flash('No DWD stations selected.')
        return redirect(url_for('settings_page', tab='locations'))

    if not _import_dwd_lock.acquire(blocking=False):
        flash('An import is already in progress.')
        return redirect(url_for('settings_page', tab='locations'))

    # From here on the lock is held; it is released either by the background
    # matcher thread (once started) or by the finally below.
    thread_owns_lock = False
    try:
        all_stations = {s['id']: s for s in providers.get('dwd').all_stations()}
        already = db.get_imported_dwd_ids()

        new_locations: list[tuple[int, dict]] = []
        for sid in station_ids:
            if sid in already:
                continue
            station = all_stations.get(sid)
            if not station:
                continue
            loc_id = db.add_location(
                station['name'], station['latitude'], station['longitude'],
                hidden=True,
            )
            db.add_location_source(loc_id, 'dwd', sid, {
                'station_name': station['name'],
                'state': station['state'],
                'height': station['height'],
                'imported_dwd': True,
            })
            new_locations.append((loc_id, station))

        if not new_locations:
            flash('All selected stations are already imported.')
            return redirect(url_for('settings_page', tab='locations'))

        def _match_providers():
            try:
                search_cache: dict[tuple[str, str], object] = {}
                for idx, (loc_id, station) in enumerate(new_locations):
                    city = station['name'].split('-')[0].split('(')[0].strip()
                    for pname in ('wetter_com', 'meteoblue', 'wetteronline'):
                        key = (pname, city)
                        if key not in search_cache:
                            # Rate-limit: 1.5s between provider searches
                            if idx > 0 or search_cache:
                                time.sleep(1.5)
                            try:
                                results = providers.get(pname).search(city)
                                search_cache[key] = results[0] if results else None
                            except Exception:
                                search_cache[key] = None
                        cached = search_cache[key]
                        if cached:
                            db.add_location_source(
                                loc_id, pname,
                                cached.provider_location_id,
                                cached.extra or {},
                            )
            finally:
                _import_dwd_lock.release()

        threading.Thread(target=_match_providers, daemon=True).start()
        thread_owns_lock = True

        flash(f'Added {len(new_locations)} DWD station(s). Matching forecast providers in background…')
    finally:
        if not thread_owns_lock:
            _import_dwd_lock.release()

    return redirect(url_for('settings_page', tab='locations'))


@app.route('/settings/schedule', methods=['POST'])
@login_required
@csrf_protect
def settings_save_schedule():
    cron = request.form.get('poll_cron', '').strip()
    if cron and len(cron.split()) == 5:
        db.set_setting('poll_cron', cron)
        _notify_poller()
    return redirect(url_for('settings_page', tab='schedule'))


def _notify_poller():
    """Send SIGHUP to the poller process to reload its schedule."""
    import signal as _signal
    pidfile = os.path.join(os.path.dirname(__file__), 'poll.pid')
    try:
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, _signal.SIGHUP)
    except (OSError, ValueError):
        pass


@app.route('/settings/providers', methods=['POST'])
@login_required
@csrf_protect
def settings_save_providers():
    colors = {}
    delays = {}
    for p in providers.all_providers():
        color = request.form.get(f'color_{p.name}', '').strip()
        if color and len(color) == 7 and color.startswith('#'):
            colors[p.name] = color
        try:
            delays[p.name] = max(0.0, min(30.0, float(request.form.get(f'delay_{p.name}', '') or 0.25)))
        except ValueError:
            delays[p.name] = 0.25
    db.set_setting('provider_colors', json.dumps(colors))
    db.set_setting('provider_delays', json.dumps(delays))
    return redirect(url_for('settings_page', tab='providers'))


# ── log API ───────────────────────────────────────────────────────────────────

@app.route('/api/logs')
@login_required
def api_logs():
    file_key = request.args.get('file', 'app')
    if file_key not in _LOG_FILES:
        return jsonify({'error': 'unknown file'}), 400

    path = _LOG_FILES[file_key]
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            size = f.tell()

            if 'offset' in request.args:
                offset = max(0, int(request.args['offset']))
                if offset >= size:
                    return jsonify({'lines': [], 'offset': size})
                f.seek(offset)
                data = f.read()
            else:
                # tail last N lines
                n = min(int(request.args.get('tail', 300)), 2000)
                chunk = min(n * 120, size)
                f.seek(max(0, size - chunk))
                data = f.read()

            lines = data.decode('utf-8', errors='replace').splitlines()
            if 'offset' not in request.args:
                lines = lines[-n:]
            return jsonify({'lines': lines, 'offset': size})
    except FileNotFoundError:
        return jsonify({'lines': [], 'offset': 0, 'missing': True})


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
