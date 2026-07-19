import logging
from datetime import datetime

import db
import providers

logger = logging.getLogger(__name__)


def _loc_label(location_id: int) -> str:
    """'123 (Name)' for log messages; falls back to the bare id."""
    try:
        loc = db.get_location(location_id)
    except Exception:
        loc = None
    return f"{location_id} ({loc['name']})" if loc else str(location_id)


def _poll_source(location_id: int, source: dict):
    provider = providers.get(source['provider'])
    plid = source['provider_location_id']
    meta = source['metadata']
    fetched_at = datetime.utcnow().isoformat()
    loc_label = _loc_label(location_id)

    # For observation providers, check what's needed before fetching.
    if getattr(provider, 'is_observation', False):
        needed = db.get_unobserved_forecast_dates(location_id, provider.name)
        if not needed:
            logger.info('Location %s via %s: all observation data already archived',
                        loc_label, provider.name)
            return
        meta = dict(meta, _relevant_dates=needed)

    entries = provider.fetch_all(plid, meta)
    if not entries:
        logger.warning('No data returned for location %s via %s', loc_label, provider.name)
        return

    if getattr(provider, 'is_observation', False):
        existing = db.get_existing_forecast_times(location_id, provider.name)
        entries = [e for e in entries if (e.granularity, e.forecast_time) not in existing]
        if not entries:
            return

    db.save_forecast_batch(location_id, provider.name, fetched_at, entries)
    by_gran: dict[str, int] = {}
    for e in entries:
        by_gran[e.granularity] = by_gran.get(e.granularity, 0) + 1
    logger.info('Polled location %s via %s: %s entries', loc_label, provider.name,
                ', '.join(f'{n} {g}' for g, n in sorted(by_gran.items())))


def poll_all_due():
    for loc in db.get_locations(show_hidden=True):
        for source in db.get_location_sources(loc['id']):
            if not source.get('enabled', 1):
                continue
            if not db.recently_polled(loc['id'], source['provider']):
                try:
                    _poll_source(loc['id'], source)
                except Exception as e:
                    logger.error('Poll failed for location %d (%s): %s',
                                 loc['id'], loc['name'], e)
