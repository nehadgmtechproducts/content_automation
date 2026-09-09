"""FastAPI surface. n8n drives the pipeline through these endpoints."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from app import pipeline
from app.db import session_scope
from app.models import AssetBundle, Draft, FactSheet, Topic, TopicStatus
from app.settings import get_settings

app = FastAPI(title="Automated Content Engine", version="0.1.0")


class PollResponse(BaseModel):
    inserted: int
    duplicates: int
    failed_sources: list[str]
    clusters: int
    topics_fired: int
    topic_ids: list[int]


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "ok": True,
        "anthropic_key_set": bool(settings.anthropic_api_key),
        "firecrawl_key_set": bool(settings.firecrawl_api_key),
        "pexels_key_set": bool(settings.pexels_api_key),
    }


@app.post("/v1/pipeline/poll", response_model=PollResponse)
def poll(enqueue: bool = True) -> PollResponse:
    """Stage 1 + 2. n8n's cron node calls this every 10 minutes."""
    result = pipeline.run_poll()
    if enqueue and result["topic_ids"]:
        from app.tasks import produce_topic

        for topic_id in result["topic_ids"]:
            produce_topic.delay(topic_id)
    return PollResponse(**{k: result[k] for k in PollResponse.model_fields})


@app.get("/v1/pipeline/preview")
def preview(window_minutes: int | None = None) -> dict:
    """Dry-run detection — shows what would fire, and why, without writing anything."""
    from app.detect.velocity import run_detection

    return {"detections": run_detection(dry_run=True, window_minutes=window_minutes)}


@app.get("/v1/topics")
def list_topics(status: TopicStatus | None = None, limit: int = 50) -> dict:
    with session_scope() as s:
        stmt = select(Topic).order_by(Topic.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Topic.status == status)
        topics = list(s.scalars(stmt))
        return {"topics": [
            {"id": t.id, "title": t.title, "entity": t.primary_entity,
             "status": t.status.value, "error": t.error,
             "created_at": t.created_at.isoformat()}
            for t in topics
        ]}


@app.get("/v1/topics/{topic_id}")
def get_topic(topic_id: int) -> dict:
    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            raise HTTPException(404, "topic not found")
        facts = s.scalar(select(FactSheet).where(FactSheet.topic_id == topic_id))
        draft = s.scalar(select(Draft).where(Draft.topic_id == topic_id))
        bundle = s.scalar(select(AssetBundle).where(AssetBundle.topic_id == topic_id))
        return {
            "id": topic.id, "title": topic.title, "entity": topic.primary_entity,
            "status": topic.status.value, "error": topic.error,
            "fact_sheet": facts.payload if facts else None,
            "article": draft.article if draft else None,
            "script": draft.script if draft else None,
            "metadata": draft.meta if draft else None,
            "validation_notes": draft.validation_notes if draft else "",
            "assets": {
                "video": bundle.video, "hero": bundle.hero_image,
                "duration": bundle.duration_seconds, "providers": bundle.providers_used,
            } if bundle else None,
        }


@app.post("/v1/topics/{topic_id}/produce")
def produce_topic_endpoint(topic_id: int, background: bool = True) -> dict:
    with session_scope() as s:
        if s.get(Topic, topic_id) is None:
            raise HTTPException(404, "topic not found")
    if background:
        from app.tasks import produce_topic

        task = produce_topic.delay(topic_id)
        return {"topic_id": topic_id, "queued": True, "task_id": task.id}
    return pipeline.produce(topic_id)


@app.post("/v1/topics/{topic_id}/notify")
def notify_topic(topic_id: int) -> dict:
    """Stage 5 — manually (re)send the Telegram approval card for one topic."""
    from app.approval.service import push_for_approval

    with session_scope() as s:
        if s.get(Topic, topic_id) is None:
            raise HTTPException(404, "topic not found")
    result = push_for_approval(topic_id)
    if not result.get("sent"):
        raise HTTPException(422, result.get("reason", "failed to send"))
    return result


@app.get("/v1/topics/{topic_id}/video")
def get_video(topic_id: int) -> FileResponse:
    """Serves the rendered MP4 — this is what the stage-5 Telegram card will attach."""
    with session_scope() as s:
        bundle = s.scalar(select(AssetBundle).where(AssetBundle.topic_id == topic_id))
        if bundle is None or not bundle.video:
            raise HTTPException(404, "no rendered video for this topic")
        path = bundle.video
    return FileResponse(path, media_type="video/mp4", filename=f"topic-{topic_id}.mp4")
