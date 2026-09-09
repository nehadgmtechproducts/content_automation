"""Celery task graph and beat schedule."""
from __future__ import annotations

import logging

from celery import Celery
from celery.schedules import crontab

from app import pipeline
from app.assets.pipeline import prune_old_media
from app.settings import detect_config, get_settings

log = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("content_engine", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,   # renders are long; don't hoard tasks
    task_time_limit=3600,
    task_soft_time_limit=3300,
    broker_connection_retry_on_startup=True,
)


@celery_app.task(name="poll_and_detect")
def poll_and_detect() -> dict:
    result = pipeline.run_poll()
    for topic_id in result["topic_ids"]:
        produce_topic.delay(topic_id)
    # Detection reports are verbose; the API returns them, the queue doesn't need them.
    return {k: v for k, v in result.items() if k != "detections"}


@celery_app.task(name="produce_topic", bind=True, max_retries=1)
def produce_topic(self, topic_id: int) -> dict:
    return pipeline.produce(topic_id)


@celery_app.task(name="scrape_topic")
def scrape_topic(topic_id: int) -> dict:
    return pipeline.run_scrape(topic_id)


@celery_app.task(name="synthesize_topic")
def synthesize_topic(topic_id: int) -> dict:
    return pipeline.run_synthesis(topic_id)


@celery_app.task(name="build_assets")
def build_assets(topic_id: int) -> dict:
    return pipeline.run_assets(topic_id)


@celery_app.task(name="prune_media")
def prune_media() -> dict:
    return {"removed": prune_old_media()}


celery_app.conf.beat_schedule = {
    "poll-every-10-min": {
        "task": "poll_and_detect",
        "schedule": detect_config()["poll_interval_minutes"] * 60.0,
    },
    "prune-media-daily": {
        "task": "prune_media",
        "schedule": crontab(hour=4, minute=0),
    },
}
