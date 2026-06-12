# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
python app.py          # starts Flask on port 5000
```

Auth credentials are read from env vars `BETHER_USER` (default: `admin`) and `BETHER_PASSWORD`. If `BETHER_PASSWORD` is unset, a random one is printed to the log on startup.

## Architecture

**bether** is a personal weather archive: it polls multiple weather provider websites daily, stores every forecast snapshot in SQLite, and shows how forecasts for a given target date evolved over time (forecast evolution plots).

### Data flow

1. `app.py` runs an APScheduler cron job at 06:00 UTC calling `poll_all_due()`.
2. For each location + provider pair, the appropriate `WeatherProvider` subclass scrapes the provider site and returns a list of `ForecastEntry` objects.
3. `db.save_forecast_batch()` stores each entry as a row in `forecast_snapshots` tagged with `fetched_at` (poll timestamp) and `forecast_time` (the date/time being forecasted).
4. The plot page (`/location/<id>/plot`) has two modes: the default daily mode calls `db.get_forecast_evolution()` (one row per archived poll for a chosen target date) and `_build_evolution_traces()`; `?mode=hourly` calls `db.get_hourly_runs()` and `_build_hourly_traces()`, which draws one hourly forecast curve per archived poll with older polls progressively more transparent.

### Key files

| File | Role |
|------|------|
| `app.py` | Flask routes, scheduler, auth, Plotly trace builder |
| `db.py` | All SQLite access; `init_db()` handles schema migrations |
| `providers/base.py` | `WeatherProvider` ABC, `ForecastEntry` dataclass, `LocationResult` dataclass |
| `providers/__init__.py` | `REGISTRY` dict; `get(name)` and `all_providers()` |
| `providers/wetter_com.py` | Scrapes wetter.com 16-day daily forecast + hourly forecasts (8 days, from the per-day detail diagram pages) |
| `providers/meteoblue.py` | Scrapes meteoblue.com weekly daily forecast + 3-hourly forecasts (14 days, from the `week/oneday` endpoint) |
| `providers/wetteronline.py` | Scrapes wetteronline.de 16-day daily forecast (embedded JSON) + hourly forecasts (~49 h from the SSR'd hourcast strip, regex-parsed) + 6-h interval data (~4 days, MediumTerm JSON; no temperature) |

### Database schema (`weather.db`)

- **`locations`** — user-defined locations (name, lat, lon)
- **`location_sources`** — one row per (location, provider) pair, stores `provider_location_id` and JSON `metadata` (e.g. `seo_string` for wetter.com)
- **`forecast_snapshots`** — immutable archive; every poll appends rows; key columns: `location_id`, `provider`, `granularity` (`daily`/`hourly`), `fetched_at`, `forecast_time`

`db.init_db()` runs on every startup and handles additive migrations via `_add_columns_if_missing`. Dropping columns requires the table-rebuild pattern already used in `_migrate_forecast_time`.

### Adding a new provider

1. Create `providers/<name>.py` with a class that subclasses `WeatherProvider`.
2. Set `name` (DB slug), `display_name`, `supports_daily`/`supports_hourly`.
3. Implement `search()` → `list[LocationResult]` and `fetch_daily()` / `fetch_hourly()` → `list[ForecastEntry]`. If both granularities come from the same pages, override `fetch_all()` so the poller fetches them in one pass (see wetter_com / meteoblue).
4. Instantiate and append to `_all` in `providers/__init__.py`.
5. Add a color entry in `_PROVIDER_COLORS` in `app.py` if desired.

### Auth

Single-user session auth via Flask `session`. The `@login_required` decorator guards all write routes (`/add`, `/refresh`, `/delete`, `/add_source`). Read routes (index, plot, db browser, search) are public.
