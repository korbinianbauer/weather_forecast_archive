import logging
from datetime import datetime

import db
import providers

logger = logging.getLogger(__name__)


def _poll_source(location_id: int, source: dict):
    provider = providers.get(source['provider'])
    plid = source['provider_location_id']
    meta = source['metadata']
    fetched_at = datetime.utcnow().isoformat()

    entries = provider.fetch_all(plid, meta)
    if entries:
        db.save_forecast_batch(location_id, provider.name, fetched_at, entries)
        by_gran: dict[str, int] = {}
        for e in entries:
            by_gran[e.granularity] = by_gran.get(e.granularity, 0) + 1
        logger.info('Polled location %d via %s: %s entries', location_id, provider.name,
                    ', '.join(f'{n} {g}' for g, n in sorted(by_gran.items())))
    else:
        logger.warning('No data returned for location %d via %s', location_id, provider.name)


def poll_all_due():
    for loc in db.get_locations():
        for source in db.get_location_sources(loc['id']):
            if not source.get('enabled', 1):
                continue
            if not db.recently_polled(loc['id'], source['provider']):
                try:
                    _poll_source(loc['id'], source)
                except Exception as e:
                    logger.error('Poll failed for location %d: %s', loc['id'], e)
