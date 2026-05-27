"""Standalone polling process — run separately from the web server."""
import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from poller import poll_all_due

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def _shutdown(signum, frame):
    logger.info('Shutting down poller')
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    scheduler = BlockingScheduler()
    scheduler.add_job(poll_all_due, CronTrigger(hour=6, minute=0))
    logger.info('Poller started — daily poll at 06:00 UTC')
    scheduler.start()
