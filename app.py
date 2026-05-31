import collections
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import defaultdict
from datetime import datetime
from functools import wraps

from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, session, url_for)

import db
import providers
from poller import _poll_source, poll_all_due

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', '')
if not app.secret_key:
    raise RuntimeError('SECRET_KEY environment variable is not set')

_ADMIN_USER = os.environ.get('BETHER_USER', 'admin')
_ADMIN_PASSWORD = os.environ.get('BETHER_PASSWORD', '')
if not _ADMIN_PASSWORD:
    logger.warning('BETHER_PASSWORD not set — login will always fail')

db.init_db()


# ── in-memory log buffer ──────────────────────────────────────────────────────

_log_lines: collections.deque = collections.deque(maxlen=500)
_log_counter = 0
_log_lock = threading.Lock()


class _MemHandler(logging.Handler):
    def emit(self, record):
        global _log_counter
        with _log_lock:
            _log_counter += 1
            n = _log_counter
        _log_lines.append({
            'n': n,
            'level': record.levelname,
            'msg': self.format(record),
        })


_mem_handler = _MemHandler()
_mem_handler.setFormatter(
    logging.Formatter('%(asctime)s %(levelname)-8s %(name)s — %(message)s')
)
logging.getLogger().addHandler(_mem_handler)


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


@app.context_processor
def inject_provider_colors():
    try:
        colors = json.loads(db.get_setting('provider_colors', '{}'))
    except json.JSONDecodeError:
        colors = {}
    defaults = {'wetter_com': '#3b82f6', 'meteoblue': '#22c55e'}
    return {'provider_colors': {**defaults, **colors}}


# ── provider color helpers ────────────────────────────────────────────────────

_DEFAULT_COLOR_HEX = '#a855f7'


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip('#')
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _provider_rgba(provider: str, alpha: float) -> str:
    try:
        colors = json.loads(db.get_setting('provider_colors', '{}'))
    except json.JSONDecodeError:
        colors = {}
    defaults = {'wetter_com': '#3b82f6', 'meteoblue': '#22c55e'}
    hex_color = {**defaults, **colors}.get(provider, _DEFAULT_COLOR_HEX)
    r, g, b = _hex_to_rgb(hex_color)
    return f'rgba({r},{g},{b},{alpha:.2f})'


# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    locs = db.get_locations()
    provider_labels = {p.name: p.display_name for p in providers.all_providers()}

    location_data = []
    for loc in locs:
        sources = db.get_location_sources(loc['id'])
        rows = []
        all_dates: set[str] = set()

        for source in sources:
            entries = db.get_latest_forecast(loc['id'], provider=source['provider'], granularity='daily')
            by_date = {e['forecast_time'][:10]: e for e in entries}
            all_dates.update(by_date.keys())
            rows.append({
                'provider': source['provider'],
                'fetched_at': entries[0]['fetched_at'] if entries else None,
                'by_date': by_date,
            })

        location_data.append({
            'location': loc,
            'all_dates': sorted(all_dates),
            'rows': rows,
        })

    return render_template(
        'index.html',
        location_data=location_data,
        provider_labels=provider_labels,
        all_providers=providers.all_providers(),
    )


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

    for source in db.get_location_sources(location_id):
        try:
            _poll_source(location_id, source)
        except Exception as e:
            logger.error('Initial poll failed for %s: %s', source['provider'], e)

    return redirect(url_for('location_plot', location_id=location_id))


@app.route('/location/<int:location_id>/plot')
def location_plot(location_id):
    location = db.get_location(location_id)
    if not location:
        return redirect(url_for('index'))

    sources = db.get_location_sources(location_id)
    provider_labels = {p.name: p.display_name for p in providers.all_providers()}
    available_dates = db.get_available_forecast_dates(location_id)
    all_provider_names = [s['provider'] for s in sources]

    target_date = request.args.get('date', available_dates[0] if available_dates else '')
    url_providers = request.args.getlist('providers')
    default_provider = url_providers[0] if len(url_providers) == 1 else None

    rows = db.get_forecast_evolution(location_id, target_date, all_provider_names) if target_date else []
    traces_json = json.dumps(_build_evolution_traces(rows, provider_labels))

    return render_template(
        'plot.html',
        location=location,
        available_dates=available_dates,
        target_date=target_date,
        all_providers=all_provider_names,
        provider_labels=provider_labels,
        default_provider=default_provider,
        traces_json=traces_json,
    )


def _build_evolution_traces(rows: list[dict], provider_labels: dict) -> list[dict]:
    if not rows:
        return []

    groups: dict[tuple, list] = defaultdict(list)
    for row in rows:
        groups[(row['provider'], row['granularity'])].append(row)
    for key in groups:
        groups[key].sort(key=lambda r: r['fetched_at'])

    simple_metrics = [
        ('sunshine_hours',     'sunshine_hours'),
        ('precip_probability', 'precipitation_precip_probability'),
        ('precip_amount',      'precipitation_precip_amount'),
        ('wind_speed',         'wind_wind_speed'),
        ('cloud_cover',        'cloud_cloud_cover'),
        ('pressure',           'pressure_pressure'),
        ('humidity',           'humidity_humidity'),
    ]

    all_traces = []
    legend_shown: set[str] = set()

    for (provider, granularity), entries in sorted(groups.items()):
        color      = _provider_rgba(provider, 1.0)
        fill_color = _provider_rgba(provider, 0.15)
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
                'line': {'color': color, 'width': 2, 'dash': 'dot'},
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

        for field, metric_key in simple_metrics:
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

    return all_traces


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
            try:
                _poll_source(location_id, source)
            except Exception as e:
                logger.error('Poll failed after adding source %s: %s', pname, e)

    return redirect(url_for('location_plot', location_id=location_id))


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
    return redirect(url_for('settings_page', tab='locations'))


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

_DB_TABLES  = ('locations', 'location_sources', 'forecast_snapshots')
_DB_PAGE_SIZE = 200


@app.route('/settings')
@login_required
def settings_page():
    tab = request.args.get('tab', 'schedule')

    all_settings  = db.get_all_settings()
    all_locations = db.get_locations(show_hidden=True)
    all_providers_list = providers.all_providers()
    provider_labels    = {p.name: p.display_name for p in all_providers_list}

    try:
        stored_colors = json.loads(all_settings.get('provider_colors', '{}'))
    except json.JSONDecodeError:
        stored_colors = {}
    try:
        stored_delays = json.loads(all_settings.get('provider_delays', '{}'))
    except json.JSONDecodeError:
        stored_delays = {}

    db_tables_summary = []
    with db.get_db() as conn:
        for t in _DB_TABLES:
            count = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            cols  = [r[1] for r in conn.execute(f'PRAGMA table_info({t})')]
            db_tables_summary.append({'name': t, 'count': count, 'cols': cols})

    db_active_table = request.args.get('db_table') if tab == 'database' else None
    db_rows = db_cols = db_total = db_filtered_total = None
    db_page    = max(0, int(request.args.get('db_page', 0)))
    db_filters = {k[2:]: v for k, v in request.args.items()
                  if k.startswith('f_') and v.strip()}

    if db_active_table and db_active_table in _DB_TABLES:
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

    return render_template(
        'settings.html',
        tab=tab,
        server_tz=server_tz,
        all_settings=all_settings,
        all_locations=all_locations,
        all_providers=all_providers_list,
        provider_labels=provider_labels,
        stored_colors=stored_colors,
        stored_delays=stored_delays,
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


@app.route('/settings/schedule', methods=['POST'])
@login_required
@csrf_protect
def settings_save_schedule():
    cron = request.form.get('poll_cron', '').strip()
    if cron and len(cron.split()) == 5:
        db.set_setting('poll_cron', cron)
    return redirect(url_for('settings_page', tab='schedule'))


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


# ── db browser (legacy standalone routes kept for compatibility) ──────────────

@app.route('/db')
def db_overview():
    tables = []
    with db.get_db() as conn:
        for t in _DB_TABLES:
            count = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            cols  = [r[1] for r in conn.execute(f'PRAGMA table_info({t})')]
            tables.append({'name': t, 'count': count, 'cols': cols})
    return render_template('db_browser.html', tables=tables, active_table=None)


@app.route('/db/<table>')
def db_table(table):
    if table not in _DB_TABLES:
        return redirect(url_for('db_overview'))

    page    = max(0, int(request.args.get('page', 0)))
    filters = {k: v for k, v in request.args.items() if k not in ('page',) and v.strip()}

    with db.get_db() as conn:
        cols  = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
        total = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

        where_parts, params = [], []
        for col, val in filters.items():
            if col in cols:
                where_parts.append(f'{col} LIKE ?')
                params.append(f'%{val}%')
        where = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''

        filtered_total = conn.execute(
            f'SELECT COUNT(*) FROM {table} {where}', params
        ).fetchone()[0]
        rows = [list(r) for r in conn.execute(
            f'SELECT * FROM {table} {where} LIMIT ? OFFSET ?',
            params + [_DB_PAGE_SIZE, page * _DB_PAGE_SIZE],
        ).fetchall()]

    tables_summary = []
    with db.get_db() as conn:
        for t in _DB_TABLES:
            count = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            tables_summary.append({'name': t, 'count': count, 'cols': []})

    return render_template(
        'db_browser.html',
        tables=tables_summary,
        active_table=table,
        cols=cols,
        rows=rows,
        total=total,
        filtered_total=filtered_total,
        page=page,
        page_size=_DB_PAGE_SIZE,
        filters=filters,
    )


# ── log API ───────────────────────────────────────────────────────────────────

@app.route('/api/logs')
@login_required
def api_logs():
    since = int(request.args.get('since', 0))
    lines = [l for l in list(_log_lines) if l['n'] > since]
    return jsonify({'lines': lines, 'count': _log_counter})


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
