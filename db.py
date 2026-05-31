import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), 'weather.db')


# ── init / migration ──────────────────────────────────────────────────────────

_SNAPSHOT_DDL = '''
    CREATE TABLE forecast_snapshots (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        location_id          INTEGER NOT NULL
                                 REFERENCES locations(id) ON DELETE CASCADE,
        provider             TEXT NOT NULL,
        granularity          TEXT NOT NULL DEFAULT 'daily',
        fetched_at           TEXT NOT NULL,
        forecast_time        TEXT NOT NULL,
        condition_text       TEXT,
        icon_url             TEXT,
        temperature          REAL,
        temp_max             REAL,
        temp_min             REAL,
        precip_probability   INTEGER,
        precip_amount        REAL,
        wind_direction       TEXT,
        wind_speed           INTEGER,
        sunshine_hours       REAL,
        cloud_cover          INTEGER,
        pressure             REAL,
        humidity             INTEGER
    )
'''

_SNAPSHOT_INDEX_DDL = '''
    CREATE INDEX IF NOT EXISTS idx_snap_loc_provider_gran
        ON forecast_snapshots(location_id, provider, granularity, fetched_at, forecast_time)
'''


def init_db():
    with get_db() as conn:
        existing_tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        # Drop pre-provider-architecture schema if location_sources is missing
        if existing_tables and 'location_sources' not in existing_tables:
            conn.executescript('''
                DROP TABLE IF EXISTS forecast_snapshots;
                DROP TABLE IF EXISTS locations;
            ''')

        conn.executescript('''
            CREATE TABLE IF NOT EXISTS locations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                latitude   REAL,
                longitude  REAL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS location_sources (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                location_id          INTEGER NOT NULL
                                         REFERENCES locations(id) ON DELETE CASCADE,
                provider             TEXT NOT NULL,
                provider_location_id TEXT NOT NULL,
                metadata             TEXT NOT NULL DEFAULT '{}',
                UNIQUE(location_id, provider)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );
        ''')

        if 'forecast_snapshots' not in existing_tables:
            conn.executescript(_SNAPSHOT_DDL + '; ' + _SNAPSHOT_INDEX_DDL)
        else:
            _migrate_forecast_time(conn)
            _add_columns_if_missing(conn, 'forecast_snapshots', {
                'sunshine_hours': 'REAL',
            })

        _add_columns_if_missing(conn, 'locations', {
            'sort_order': 'INTEGER DEFAULT 0',
            'hidden':     'INTEGER DEFAULT 0',
        })
        _init_default_settings(conn)


def _init_default_settings(conn):
    defaults = {
        'poll_cron':        '0 6 * * *',
        'provider_colors':  json.dumps({'wetter_com': '#3b82f6', 'meteoblue': '#22c55e'}),
        'provider_delays':  json.dumps({'wetter_com': 0.25, 'meteoblue': 0.25}),
    }
    for key, val in defaults.items():
        conn.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, val))


def _add_columns_if_missing(conn, table: str, columns: dict[str, str]):
    existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
    for col, typedef in columns.items():
        if col not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {typedef}')


def _migrate_forecast_time(conn):
    """Migrate forecast_date + forecast_hour → forecast_time, then drop old columns."""
    cols = {row[1] for row in conn.execute('PRAGMA table_info(forecast_snapshots)')}
    if 'forecast_date' not in cols:
        return  # already on new schema

    if 'forecast_time' not in cols:
        conn.execute("ALTER TABLE forecast_snapshots ADD COLUMN forecast_time TEXT")
    conn.execute("""
        UPDATE forecast_snapshots
        SET forecast_time = forecast_date || 'T' ||
            printf('%02d', COALESCE(forecast_hour, 0)) || ':00:00'
        WHERE forecast_time IS NULL
    """)

    conn.executescript(f'''
        CREATE TABLE forecast_snapshots_new {_SNAPSHOT_DDL.split("CREATE TABLE forecast_snapshots")[1]};
        INSERT INTO forecast_snapshots_new
            SELECT id, location_id, provider, granularity, fetched_at, forecast_time,
                   condition_text, icon_url, temperature, temp_max, temp_min,
                   precip_probability, precip_amount, wind_direction, wind_speed,
                   cloud_cover, pressure, humidity
            FROM forecast_snapshots
            WHERE forecast_time IS NOT NULL;
        DROP TABLE forecast_snapshots;
        ALTER TABLE forecast_snapshots_new RENAME TO forecast_snapshots;
        {_SNAPSHOT_INDEX_DDL};
    ''')


# ── connection ────────────────────────────────────────────────────────────────

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = '') -> str:
    with get_db() as conn:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))


def get_all_settings() -> dict[str, str]:
    with get_db() as conn:
        return {r[0]: r[1] for r in conn.execute('SELECT key, value FROM settings')}


def get_provider_delay(provider: str) -> float:
    try:
        delays = json.loads(get_setting('provider_delays', '{}'))
        return max(0.0, float(delays.get(provider, 0.25)))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0.25


# ── locations ─────────────────────────────────────────────────────────────────

def add_location(name: str, latitude: float, longitude: float) -> int:
    with get_db() as conn:
        cur = conn.execute(
            'INSERT INTO locations (name, latitude, longitude) VALUES (?, ?, ?)',
            (name, latitude, longitude),
        )
        return cur.lastrowid


def add_location_source(
    location_id: int,
    provider: str,
    provider_location_id: str,
    metadata: dict,
):
    with get_db() as conn:
        conn.execute(
            '''INSERT OR REPLACE INTO location_sources
               (location_id, provider, provider_location_id, metadata)
               VALUES (?, ?, ?, ?)''',
            (location_id, provider, provider_location_id, json.dumps(metadata)),
        )


def get_location(location_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute('SELECT * FROM locations WHERE id = ?', (location_id,)).fetchone()
        return dict(row) if row else None


def get_location_sources(location_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            'SELECT * FROM location_sources WHERE location_id = ? ORDER BY provider',
            (location_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['metadata'] = json.loads(d['metadata'] or '{}')
            result.append(d)
        return result


def get_locations(show_hidden: bool = False) -> list[dict]:
    hidden_clause = '' if show_hidden else 'WHERE COALESCE(l.hidden, 0) = 0'
    with get_db() as conn:
        return [dict(r) for r in conn.execute(f'''
            SELECT l.*,
                   (SELECT MAX(fetched_at) FROM forecast_snapshots
                    WHERE location_id = l.id)                          AS last_polled,
                   (SELECT COUNT(DISTINCT provider || fetched_at) FROM forecast_snapshots
                    WHERE location_id = l.id)                          AS poll_count,
                   (SELECT GROUP_CONCAT(provider, ', ') FROM location_sources
                    WHERE location_id = l.id)                          AS providers
            FROM locations l {hidden_clause}
            ORDER BY COALESCE(l.sort_order, 0), l.id
        ''')]


def delete_location(location_id: int):
    with get_db() as conn:
        conn.execute('DELETE FROM locations WHERE id = ?', (location_id,))


def toggle_location_hidden(location_id: int):
    with get_db() as conn:
        conn.execute(
            'UPDATE locations SET hidden = 1 - COALESCE(hidden, 0) WHERE id = ?',
            (location_id,),
        )


def update_location_sort_order(location_id: int, sort_order: int):
    with get_db() as conn:
        conn.execute(
            'UPDATE locations SET sort_order = ? WHERE id = ?',
            (sort_order, location_id),
        )


# ── forecast snapshots ────────────────────────────────────────────────────────

def save_forecast_batch(
    location_id: int,
    provider: str,
    fetched_at: str,
    entries: list,              # list[ForecastEntry]
):
    with get_db() as conn:
        conn.executemany(
            '''INSERT INTO forecast_snapshots
               (location_id, provider, granularity, fetched_at,
                forecast_time,
                condition_text, icon_url,
                temperature, temp_max, temp_min,
                precip_probability, precip_amount,
                wind_direction, wind_speed,
                sunshine_hours,
                cloud_cover, pressure, humidity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            [
                (
                    location_id, provider, e.granularity, fetched_at,
                    e.forecast_time,
                    e.condition_text, e.icon_url,
                    e.temperature, e.temp_max, e.temp_min,
                    e.precip_probability, e.precip_amount,
                    e.wind_direction, e.wind_speed,
                    e.sunshine_hours,
                    e.cloud_cover, e.pressure, e.humidity,
                )
                for e in entries
            ],
        )


def get_latest_forecast(
    location_id: int,
    provider: str | None = None,
    granularity: str | None = None,
) -> list[dict]:
    with get_db() as conn:
        clauses = ['location_id = ?']
        params: list = [location_id]
        if provider:
            clauses.append('provider = ?')
            params.append(provider)
        if granularity:
            clauses.append('granularity = ?')
            params.append(granularity)
        where = ' AND '.join(clauses)

        latest_ts = conn.execute(
            f'SELECT MAX(fetched_at) FROM forecast_snapshots WHERE {where}',
            params,
        ).fetchone()[0]
        if not latest_ts:
            return []

        rows = conn.execute(
            f'''SELECT * FROM forecast_snapshots
                WHERE {where} AND fetched_at = ?
                ORDER BY forecast_time''',
            params + [latest_ts],
        ).fetchall()
        return [dict(r) for r in rows]


def get_poll_history(
    location_id: int,
    provider: str | None = None,
    granularity: str | None = None,
) -> list[tuple[str, str, str]]:
    """Return (fetched_at, provider, granularity) tuples, newest first."""
    with get_db() as conn:
        clauses = ['location_id = ?']
        params: list = [location_id]
        if provider:
            clauses.append('provider = ?')
            params.append(provider)
        if granularity:
            clauses.append('granularity = ?')
            params.append(granularity)
        where = ' AND '.join(clauses)

        rows = conn.execute(
            f'''SELECT DISTINCT fetched_at, provider, granularity
                FROM forecast_snapshots WHERE {where}
                ORDER BY fetched_at DESC''',
            params,
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]


def get_forecasts_by_poll(
    location_id: int,
    fetched_at: str,
    provider: str,
    granularity: str,
) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            '''SELECT * FROM forecast_snapshots
               WHERE location_id = ? AND provider = ? AND granularity = ? AND fetched_at = ?
               ORDER BY forecast_time''',
            (location_id, provider, granularity, fetched_at),
        ).fetchall()
        return [dict(r) for r in rows]


def get_available_forecast_dates(location_id: int) -> list[str]:
    """All unique forecast dates archived for a location, newest first."""
    with get_db() as conn:
        return [r[0] for r in conn.execute(
            '''SELECT DISTINCT date(forecast_time) FROM forecast_snapshots
               WHERE location_id = ? ORDER BY forecast_time DESC''',
            (location_id,),
        )]


def get_polls_covering_date(
    location_id: int,
    target_date: str,
    providers: list[str] | None = None,
) -> list[dict]:
    with get_db() as conn:
        if providers:
            ph = ','.join('?' * len(providers))
            rows = conn.execute(
                f'''SELECT DISTINCT fetched_at, provider, granularity
                    FROM forecast_snapshots
                    WHERE location_id = ? AND date(forecast_time) = ?
                      AND provider IN ({ph})
                    ORDER BY fetched_at''',
                [location_id, target_date] + list(providers),
            ).fetchall()
        else:
            rows = conn.execute(
                '''SELECT DISTINCT fetched_at, provider, granularity
                   FROM forecast_snapshots
                   WHERE location_id = ? AND date(forecast_time) = ?
                   ORDER BY fetched_at''',
                [location_id, target_date],
            ).fetchall()

        result = []
        for fetched_at, provider, granularity in rows:
            entries = conn.execute(
                '''SELECT * FROM forecast_snapshots
                   WHERE location_id = ? AND provider = ?
                     AND granularity = ? AND fetched_at = ?
                     AND forecast_time <= ?
                   ORDER BY forecast_time''',
                [location_id, provider, granularity, fetched_at, target_date + 'T23:59:59'],
            ).fetchall()
            result.append({
                'fetched_at': fetched_at,
                'provider': provider,
                'granularity': granularity,
                'entries': [dict(e) for e in entries],
            })
        return result


def get_forecast_evolution(
    location_id: int,
    target_date: str,
    providers: list[str] | None = None,
) -> list[dict]:
    with get_db() as conn:
        if providers:
            ph = ','.join('?' * len(providers))
            provider_clause = f'AND provider IN ({ph})'
            params: list = [location_id, target_date] + list(providers)
        else:
            provider_clause = ''
            params = [location_id, target_date]

        rows = conn.execute(
            f'''SELECT * FROM forecast_snapshots
                WHERE location_id = ? AND granularity = 'daily' AND date(forecast_time) = ?
                {provider_clause}
                ORDER BY fetched_at ASC''',
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def already_polled_today(location_id: int, provider: str) -> bool:
    with get_db() as conn:
        count = conn.execute(
            '''SELECT COUNT(*) FROM forecast_snapshots
               WHERE location_id = ? AND provider = ?
               AND fetched_at >= strftime('%Y-%m-%d', 'now')''',
            (location_id, provider),
        ).fetchone()[0]
        return count > 0
