import json
import logging
import os
from datetime import datetime
from functools import wraps

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

import db
import providers

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or ''
if not app.secret_key:
    logger.warning('SECRET_KEY not set — sessions will not persist across restarts')

_ADMIN_USER = os.environ.get('BETHER_USER', 'admin')
_ADMIN_PASSWORD = os.environ.get('BETHER_PASSWORD', '')
if not _ADMIN_PASSWORD:
    logger.warning('BETHER_PASSWORD not set — login will always fail')

db.init_db()


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
        u = request.form.get('username', '')
        p = request.form.get('password', '')
        if u == _ADMIN_USER and p == _ADMIN_PASSWORD:
            session['logged_in'] = True
            next_url = request.args.get('next') or url_for('index')
            # guard against open-redirect
            if not next_url.startswith('/'):
                next_url = url_for('index')
            return redirect(next_url)
        error = 'Invalid username or password.'
    return render_template('login.html', error=error)


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('index'))


# ── polling ───────────────────────────────────────────────────────────────────

def _poll_source(location_id: int, source: dict):
    provider = providers.get(source['provider'])
    plid = source['provider_location_id']
    meta = source['metadata']
    fetched_at = datetime.utcnow().isoformat()
    total = 0

    if provider.supports_daily:
        entries = provider.fetch_daily(plid, meta)
        if entries:
            db.save_forecast_batch(location_id, provider.name, fetched_at, entries)
            total += len(entries)

    if provider.supports_hourly:
        entries = provider.fetch_hourly(plid, meta)
        if entries:
            db.save_forecast_batch(location_id, provider.name, fetched_at, entries)
            total += len(entries)

    if total:
        logger.info('Polled location %d via %s: %d entries', location_id, provider.name, total)
    else:
        logger.warning('No data returned for location %d via %s', location_id, provider.name)


def poll_all_due():
    for loc in db.get_locations():
        for source in db.get_location_sources(loc['id']):
            if not db.already_polled_today(loc['id'], source['provider']):
                try:
                    _poll_source(loc['id'], source)
                except Exception as e:
                    logger.error('Poll failed for location %d: %s', loc['id'], e)


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
    """Render a nullable value; returns '—' if None."""
    return f'{value}{suffix}' if value is not None else '—'


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
    selected_providers = request.args.getlist('providers') or all_provider_names

    rows = db.get_forecast_evolution(location_id, target_date, selected_providers) if target_date else []
    traces_json = json.dumps(_build_evolution_traces(rows, provider_labels))

    return render_template(
        'plot.html',
        location=location,
        available_dates=available_dates,
        target_date=target_date,
        all_providers=all_provider_names,
        selected_providers=selected_providers,
        provider_labels=provider_labels,
        traces_json=traces_json,
    )


# Provider base colors for plot traces
_PROVIDER_COLORS = {
    'wetter_com':  (59,  130, 246),   # blue
    'meteoblue':   (34,  197, 94),    # green
}
_DEFAULT_COLOR = (168, 85,  247)      # purple


def _provider_rgba(provider: str, alpha: float) -> str:
    r, g, b = _PROVIDER_COLORS.get(provider, _DEFAULT_COLOR)
    return f'rgba({r},{g},{b},{alpha:.2f})'


def _build_evolution_traces(rows: list[dict], provider_labels: dict) -> list[dict]:
    """
    Build Plotly traces showing how forecasts for a specific target date/time
    evolved over successive polls.

    X-axis: fetched_at (when the poll was taken)
    Y-axis: the forecasted metric value for the target date/time
    One line per (provider, granularity) combination.
    """
    if not rows:
        return []

    from collections import defaultdict
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

        ymx = [e.get('temp_max')   for e in entries]
        ymn = [e.get('temp_min')   for e in entries]
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
        elif not (has_max or has_min):
            # No temperature data at all — still emit an empty trace so the chart renders
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


@app.route('/location/<int:location_id>/refresh', methods=['POST'])
@login_required
def refresh_location(location_id):
    for source in db.get_location_sources(location_id):
        try:
            _poll_source(location_id, source)
        except Exception as e:
            logger.error('Refresh failed: %s', e)
    return redirect(url_for('index'))


@app.route('/location/<int:location_id>/delete', methods=['POST'])
@login_required
def delete_location(location_id):
    db.delete_location(location_id)
    return redirect(url_for('index'))


# ── db browser ───────────────────────────────────────────────────────────────

_DB_TABLES = ('locations', 'location_sources', 'forecast_snapshots')
_DB_PAGE_SIZE = 200


@app.route('/db')
def db_overview():
    tables = []
    with db.get_db() as conn:
        for t in _DB_TABLES:
            count = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info({t})')]
            tables.append({'name': t, 'count': count, 'cols': cols})
    return render_template('db_browser.html', tables=tables, active_table=None)


@app.route('/db/<table>')
def db_table(table):
    if table not in _DB_TABLES:
        return redirect(url_for('db_overview'))

    page = max(0, int(request.args.get('page', 0)))
    filters = {k: v for k, v in request.args.items()
               if k not in ('page',) and v.strip()}

    with db.get_db() as conn:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info({table})')]
        total = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

        # Build WHERE clause from column filters
        where_parts, params = [], []
        for col, val in filters.items():
            if col in cols:
                where_parts.append(f'{col} LIKE ?')
                params.append(f'%{val}%')
        where = ('WHERE ' + ' AND '.join(where_parts)) if where_parts else ''

        filtered_total = conn.execute(
            f'SELECT COUNT(*) FROM {table} {where}', params
        ).fetchone()[0]

        rows = conn.execute(
            f'SELECT * FROM {table} {where} LIMIT ? OFFSET ?',
            params + [_DB_PAGE_SIZE, page * _DB_PAGE_SIZE],
        ).fetchall()
        rows = [list(r) for r in rows]

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


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(poll_all_due, CronTrigger(hour=6, minute=0))
    scheduler.start()
    logger.info('Scheduler running — daily poll at 06:00 UTC')
    app.run(host='0.0.0.0', port=5000, debug=False)
