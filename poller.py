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
            if not source.get('enabled', 1):
                continue
            if not db.already_polled_today(loc['id'], source['provider']):
                try:
                    _poll_source(loc['id'], source)
                except Exception as e:
                    logger.error('Poll failed for location %d: %s', loc['id'], e)
