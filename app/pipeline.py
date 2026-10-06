"""Stage runners: the bridge between database state and the stage modules.

Each function takes a topic id, does one stage, and advances Topic.status. They're
idempotent enough to re-run: a completed stage is overwritten, not duplicated.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from app.assets.pipeline import produce_assets
from app.db import session_scope
from app.detect.velocity import run_detection, top_articles_for_topic
from app.freshness import check_freshness
from app.ingest.fetch import ingest_once
from app.models import (
    AssetBundle, Draft, FactSheet, ScrapedArticle, Topic, TopicStatus,
)
from app.approval import service as approval_service
from app.scrape.client import scrape
from app.settings import detect_config, get_settings
from app.synth.engine import SourceDoc, SynthesisError

log = logging.getLogger(__name__)


def _fail(topic_id: int, message: str) -> None:
    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        if topic:
            topic.status = TopicStatus.failed
            topic.error = message[:2000]
    log.error("topic %s failed: %s", topic_id, message)


def run_poll() -> dict:
    """Stage 1 + 2: ingest every feed, then score the window."""
    report = ingest_once()
    detections = run_detection()
    fired = [d for d in detections if d["fired"]]
    return {
        "inserted": report.inserted,
        "duplicates": report.skipped_duplicates,
        "failed_sources": report.failed_sources,
        "clusters": len(detections),
        "topics_fired": len(fired),
        "topic_ids": [d["topic_id"] for d in fired],
        "detections": detections,
    }


def run_scrape(topic_id: int) -> dict:
    """Stage 2b: pull full text for the topic's top source articles."""
    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            return {"topic_id": topic_id, "scraped": 0, "error": "topic not found"}
        check = check_freshness(topic.created_at, detect_config()["freshness"]["max_age_hours"])
    if not check.fresh:
        _fail(topic_id, check.reason)
        return {"topic_id": topic_id, "scraped": 0, "error": check.reason}

    limit = detect_config()["scrape"]["top_n_articles"]
    items = top_articles_for_topic(topic_id, limit)
    if not items:
        _fail(topic_id, "no source articles found for topic")
        return {"topic_id": topic_id, "scraped": 0}

    scraped = 0
    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            return {"topic_id": topic_id, "scraped": 0}
        s.query(ScrapedArticle).filter(ScrapedArticle.topic_id == topic_id).delete()

        index = 1
        for item in items:
            result = scrape(item.canonical_url)
            if result is None:
                continue
            s.add(ScrapedArticle(
                topic_id=topic_id, source_index=index, url=item.canonical_url,
                domain=item.domain, title=result.title or item.title,
                byline=result.byline, published_at=result.published_at or item.published_at,
                markdown=result.markdown, scraper=result.scraper,
            ))
            index += 1
            scraped += 1

        if scraped >= 2:
            topic.status = TopicStatus.scraped
            topic.error = ""
        else:
            topic.status = TopicStatus.failed
            topic.error = f"only {scraped} source(s) scraped; need >= 2 for consensus"

    log.info("topic %s: scraped %d source(s)", topic_id, scraped)
    return {"topic_id": topic_id, "scraped": scraped}


def run_synthesis(topic_id: int) -> dict:
    """Stage 3: fact sheet, then article + script + metadata."""
    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            return {"topic_id": topic_id, "ok": False, "error": "topic not found"}
        check = check_freshness(topic.created_at, detect_config()["freshness"]["max_age_hours"])
    if not check.fresh:
        _fail(topic_id, check.reason)
        return {"topic_id": topic_id, "ok": False, "error": check.reason}

    with session_scope() as s:
        articles = list(s.scalars(
            select(ScrapedArticle).where(ScrapedArticle.topic_id == topic_id)
            .order_by(ScrapedArticle.source_index)
        ))
        docs = [
            SourceDoc(index=a.source_index, url=a.url, domain=a.domain,
                      title=a.title, markdown=a.markdown)
            for a in articles
        ]

    if len(docs) < 2:
        _fail(topic_id, f"need >= 2 scraped sources, have {len(docs)}")
        return {"topic_id": topic_id, "ok": False}

    provider = get_settings().synthesis_provider
    if provider == "openai":
        from app.synth.engine_openai import synthesize
    elif provider == "fal":
        from app.synth.engine_fal import synthesize
    else:
        from app.synth.engine import synthesize

    try:
        facts, draft, notes = synthesize(docs)
    except SynthesisError as exc:
        _fail(topic_id, f"synthesis refused: {exc}")
        return {"topic_id": topic_id, "ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        _fail(topic_id, f"synthesis error: {exc}")
        return {"topic_id": topic_id, "ok": False, "error": str(exc)}

    with session_scope() as s:
        s.query(FactSheet).filter(FactSheet.topic_id == topic_id).delete()
        s.query(Draft).filter(Draft.topic_id == topic_id).delete()
        s.add(FactSheet(topic_id=topic_id, payload=facts.payload, model=facts.model,
                        input_tokens=facts.input_tokens, output_tokens=facts.output_tokens))
        s.add(Draft(
            topic_id=topic_id, article=draft.payload["article"],
            script=draft.payload["script"], meta=draft.payload["metadata"],
            model=draft.model, input_tokens=draft.input_tokens,
            output_tokens=draft.output_tokens, validation_notes=notes,
        ))
        topic = s.get(Topic, topic_id)
        topic.status = TopicStatus.synthesized
        topic.title = draft.payload["article"].get("title") or topic.title
        topic.error = ""

    return {
        "topic_id": topic_id, "ok": True, "validation_notes": notes,
        "tokens": {
            "input": facts.input_tokens + draft.input_tokens,
            "output": facts.output_tokens + draft.output_tokens,
        },
    }


def run_assets(topic_id: int) -> dict:
    """Stage 4: hero image, voiceover, subtitles, rendered vertical MP4."""
    with session_scope() as s:
        draft = s.scalar(select(Draft).where(Draft.topic_id == topic_id))
        topic = s.get(Topic, topic_id)
        if draft is None or topic is None:
            _fail(topic_id, "no draft to produce assets from")
            return {"topic_id": topic_id, "ok": False}
        check = check_freshness(topic.created_at, detect_config()["freshness"]["max_age_hours"])
    if not check.fresh:
        _fail(topic_id, check.reason)
        return {"topic_id": topic_id, "ok": False, "error": check.reason}
        article, script, meta = dict(draft.article), dict(draft.script), dict(draft.meta)
        entity = topic.primary_entity

    try:
        result = produce_assets(topic_id, article, script, meta, entity=entity)
    except Exception as exc:  # noqa: BLE001
        _fail(topic_id, f"asset production error: {exc}")
        return {"topic_id": topic_id, "ok": False, "error": str(exc)}

    with session_scope() as s:
        s.query(AssetBundle).filter(AssetBundle.topic_id == topic_id).delete()
        s.add(AssetBundle(
            topic_id=topic_id, directory=str(result.directory),
            hero_image=str(result.hero_image), voiceover=str(result.voiceover),
            subtitles=str(result.subtitles), video=str(result.video),
            duration_seconds=result.duration, providers_used=result.providers_used,
        ))
        topic = s.get(Topic, topic_id)
        topic.status = TopicStatus.assets_ready
        topic.error = ""

    # Stage 5: hand off to a human. If Telegram isn't configured, the topic simply
    # stays at assets_ready — same behavior as before stage 5 existed, so this never
    # breaks a setup that hasn't added Telegram credentials yet.
    approval = {"sent": False, "reason": "not attempted"}
    if approval_service.is_configured():
        approval = approval_service.push_for_approval(topic_id)
        if not approval.get("sent"):
            log.warning("topic %s: approval card not sent: %s", topic_id, approval.get("reason"))
    else:
        approval = {"sent": False, "reason": "Telegram not configured"}

    return {
        "topic_id": topic_id, "ok": True, "video": str(result.video),
        "duration": result.duration, "providers": result.providers_used,
        "approval": approval,
    }


def produce(topic_id: int) -> dict:
    """Stages 2b -> 4 for one topic."""
    scraped = run_scrape(topic_id)
    if scraped["scraped"] < 2:
        return {"topic_id": topic_id, "ok": False, "stage": "scrape", **scraped}
    synth = run_synthesis(topic_id)
    if not synth.get("ok"):
        return {"topic_id": topic_id, "ok": False, "stage": "synthesis", **synth}
    return {"topic_id": topic_id, "ok": True, "stage": "assets", **run_assets(topic_id)}
