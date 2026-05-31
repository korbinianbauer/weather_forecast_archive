"""Standalone polling process — run separately from the web server."""
import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import db
from poller import poll_all_due

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

_DEFAULT_CRON = '0 6 * * *'
_current_cron: str | None = None


def _load_cron() -> str:
    return db.get_setting('poll_cron', _DEFAULT_CRON).strip() or _DEFAULT_CRON


def _check_schedule(scheduler: BlockingScheduler):
    global _current_cron
    cron = _load_cron()
    if cron == _current_cron:
        return
    try:
        scheduler.reschedule_job('poll', trigger=CronTrigger.from_crontab(cron))
        logger.info('Poll schedule updated: %s', cron)
        _current_cron = cron
    except Exception as e:
        logger.error('Failed to apply new poll schedule %r: %s', cron, e)


def _shutdown(signum, frame):
    logger.info('Shutting down poller')
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    db.init_db()
    cron = _load_cron()
    _current_cron = cron

    scheduler = BlockingScheduler()
    scheduler.add_job(poll_all_due, CronTrigger.from_crontab(cron), id='poll')
    scheduler.add_job(
        lambda: _check_schedule(scheduler),
        'interval', minutes=1, id='watch_schedule',
    )
    logger.info('Poller started — schedule: %s', cron)
    scheduler.start()
