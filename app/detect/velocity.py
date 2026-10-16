"""Stage 2 — the velocity trigger.

The naive spec rule ("3+ sources in 30 min") fires constantly in tech/AI: 'OpenAI'
clears three sources on a dead news day. So the bar is a *spike against that entity's
own rolling baseline*, and all four conditions must hold:

  1. >= min_distinct_domains distinct DOMAINS (not items — wire copy republished 5x is
     one source),
  2. >= 1 tier-1 original-reporting source,
  3. domain count >= baseline.mean + N*sigma once we have enough observations,
  4. the story hasn't already fired in the last 24h.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db import session_scope
from app.detect.cluster import Candidate, Cluster, cluster_candidates, default_embedder
from app.detect.entities import extract_entities
from app.freshness import check_freshness, event_time, split_fresh
from app.ingest.normalize import is_article_url
from app.models import (
    Cluster as ClusterRow, ClusterItem, EntityBaseline, FiredSuppression, RawItem,
    Topic, TopicStatus,
)
from app.settings import detect_config

log = logging.getLogger(__name__)


@dataclass
class BaselineStats:
    mean: float = 0.0
    variance: float = 0.0
    observations: int = 0

    @property
    def stddev(self) -> float:
        return math.sqrt(max(self.variance, 0.0))


@dataclass
class Decision:
    fired: bool
    reason: str
    threshold: float
    distinct_domains: int
    baseline: float


def update_ewma(stats: BaselineStats, observation: float, alpha: float) -> BaselineStats:
    """Exponentially-weighted mean and variance. Pure — unit-tested directly."""
    if stats.observations == 0:
        return BaselineStats(mean=observation, variance=0.0, observations=1)
    delta = observation - stats.mean
    new_mean = stats.mean + alpha * delta
    # West's incremental EWMA variance.
    new_var = (1 - alpha) * (stats.variance + alpha * delta * delta)
    return BaselineStats(new_mean, new_var, stats.observations + 1)


def evaluate(
    cluster: Cluster,
    stats: BaselineStats,
    cfg: dict,
    suppressed: bool = False,
) -> Decision:
    """Decide whether one cluster is a High Velocity Topic. Pure function."""
    trigger = cfg["trigger"]
    baseline_cfg = cfg["baseline"]

    domains = cluster.distinct_domains
    floor = trigger["min_distinct_domains"]

    if stats.observations >= baseline_cfg["min_observations"]:
        spike = stats.mean + trigger["baseline_sigma"] * stats.stddev
        threshold = max(float(floor), spike)
    else:
        # Not enough history for this entity yet — fall back to the flat rule.
        threshold = float(floor)

    if suppressed:
        return Decision(False, "suppressed: already fired within suppression window",
                        threshold, domains, stats.mean)
    if domains < floor:
        return Decision(False, f"only {domains} distinct domain(s), need {floor}",
                        threshold, domains, stats.mean)
    if trigger["require_tier1"] and not cluster.has_tier1:
        return Decision(False, "no tier-1 original-reporting source in cluster",
                        threshold, domains, stats.mean)
    if domains < threshold:
        return Decision(
            False,
            f"{domains} domains below spike threshold {threshold:.2f} "
            f"(baseline {stats.mean:.2f} +/- {stats.stddev:.2f})",
            threshold, domains, stats.mean,
        )

    return Decision(
        True,
        f"{domains} distinct domains >= threshold {threshold:.2f} "
        f"(baseline {stats.mean:.2f}), tier-1 present",
        threshold, domains, stats.mean,
    )


def _load_stats(session, entity: str) -> tuple[BaselineStats, EntityBaseline | None]:
    row = session.scalar(select(EntityBaseline).where(EntityBaseline.entity == entity))
    if row is None:
        return BaselineStats(), None
    return BaselineStats(row.mean, row.variance, row.observations), row


def _is_suppressed(session, fingerprint: str, hours: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return session.scalar(
        select(FiredSuppression.id)
        .where(FiredSuppression.fingerprint == fingerprint, FiredSuppression.fired_at >= cutoff)
        .limit(1)
    ) is not None


def remaining_topic_budget(session, max_per_day: int | None, now: datetime) -> int | None:
    """How many more topics may be created in the rolling 24h; None = unlimited."""
    if not max_per_day:
        return None
    created = session.scalar(
        select(func.count()).select_from(Topic).where(
            Topic.created_at >= now - timedelta(hours=24),
            Topic.status != TopicStatus.failed))   # a failed topic produced nothing: no budget used
    return max(0, max_per_day - int(created or 0))


def run_detection(dry_run: bool = False, window_minutes: int | None = None,
                  embedder=None) -> list[dict]:
    """Cluster the current window, score it, and create Topics for anything that fires.

    Returns one report dict per cluster — the same structure the CLI prints and the API
    returns, so `--dry-run` output is exactly what production would have done.
    """
    cfg = detect_config()
    window = window_minutes or cfg["window_minutes"]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window)
    embedder = embedder or default_embedder(cfg["clustering"]["embedding_model"])

    reports: list[dict] = []

    with session_scope() as s:
        items = list(s.scalars(
            select(RawItem).where(RawItem.fetched_at >= cutoff).order_by(RawItem.fetched_at)
        ))
        if not items:
            return reports

        # The window above is an INGEST window (what we fetched recently). Whether a
        # story is *breaking* depends on when it was PUBLISHED, so drop anything older
        # than the freshness limit before clustering. Stale items are still marked
        # processed below so they don't pile up.
        max_age = cfg["freshness"]["max_age_hours"]
        all_items = items
        items, stale_items = split_fresh(all_items, max_age)
        if stale_items:
            log.info("detection: ignored %d item(s) published more than %sh ago",
                     len(stale_items), max_age)
        if not items:
            for item in all_items:
                item.processed = True
            return reports

        candidates = []
        for item in items:
            entities = item.entities or extract_entities(f"{item.title}. {item.summary}")
            if not dry_run and not item.entities:
                item.entities = entities
            candidates.append(Candidate(
                item_id=item.id, title=item.title, domain=item.domain,
                tier=item.source_tier, entities=entities,
            ))

        clusters = cluster_candidates(
            candidates, embedder,
            threshold=cfg["clustering"]["cosine_threshold"],
            min_title_chars=cfg["clustering"]["min_title_chars"],
        )

        now = datetime.now(timezone.utc)
        items_by_id = {i.id: i for i in items}
        remaining = remaining_topic_budget(
            s, cfg["trigger"].get("max_topics_per_day"), now)
        for cluster in clusters:
            entity = cluster.primary_entity()
            if not entity:
                continue
            fingerprint = cluster.fingerprint()
            stats, row = _load_stats(s, entity)
            suppressed = _is_suppressed(s, fingerprint, cfg["trigger"]["suppression_hours"])
            decision = evaluate(cluster, stats, cfg, suppressed)
            if decision.fired and remaining is not None and remaining <= 0:
                decision = Decision(
                    False,
                    f"daily cap of {cfg['trigger']['max_topics_per_day']} topics reached "
                    f"(would otherwise fire: {decision.reason})",
                    decision.threshold, decision.distinct_domains, decision.baseline)
            if decision.fired and remaining is not None:
                remaining -= 1      # counted in dry-run too, so --dry-run matches production

            report = {
                "entity": entity,
                "title": cluster.representative.title,
                "members": len(cluster.members),
                "distinct_domains": decision.distinct_domains,
                "has_tier1": cluster.has_tier1,
                "baseline": round(stats.mean, 2),
                "threshold": round(decision.threshold, 2),
                "fired": decision.fired,
                "reason": decision.reason,
                "domains": sorted({m.domain for m in cluster.members}),
                "newest_source_age_hours": round(min(
                    check_freshness(event_time(items_by_id[m.item_id]), max_age, now).age_hours
                    for m in cluster.members), 1),
                "topic_id": None,
            }

            if dry_run:
                reports.append(report)
                continue

            # Baseline learns from every window, fired or not — otherwise a busy entity
            # never builds the history that would have held it back.
            new_stats = update_ewma(stats, float(decision.distinct_domains),
                                    cfg["baseline"]["ewma_alpha"])
            if row is None:
                s.add(EntityBaseline(entity=entity, mean=new_stats.mean,
                                     variance=new_stats.variance,
                                     observations=new_stats.observations))
            else:
                row.mean, row.variance, row.observations = (
                    new_stats.mean, new_stats.variance, new_stats.observations
                )

            cluster_row = ClusterRow(
                fingerprint=fingerprint, primary_entity=entity,
                entities=sorted(cluster.entity_counts()),
                representative_title=cluster.representative.title,
                distinct_domains=decision.distinct_domains, has_tier1=cluster.has_tier1,
                velocity_score=float(decision.distinct_domains), baseline=stats.mean,
                fired=decision.fired, reason=decision.reason,
                window_start=cutoff, window_end=now,
            )
            s.add(cluster_row)
            s.flush()
            for member, sim in zip(cluster.members, cluster.similarities):
                s.add(ClusterItem(cluster_id=cluster_row.id, raw_item_id=member.item_id,
                                  similarity=float(sim)))

            if decision.fired:
                topic = Topic(cluster_id=cluster_row.id,
                              title=cluster.representative.title,
                              primary_entity=entity, status=TopicStatus.detected)
                s.add(topic)
                s.add(FiredSuppression(fingerprint=fingerprint, entity=entity))
                s.flush()
                report["topic_id"] = topic.id
                log.info("FIRED topic %s: %s", topic.id, cluster.representative.title)

            reports.append(report)

        for item in all_items:
            item.processed = True

    return reports


def top_articles_for_topic(topic_id: int, limit: int) -> list[RawItem]:
    """The n source articles to scrape: tier-1 first, then most recent.

    De-duplicated by domain so we never scrape the same outlet twice and hand the
    synthesis engine three copies of one newsroom's framing.
    """
    with session_scope() as s:
        topic = s.get(Topic, topic_id)
        if topic is None or topic.cluster_id is None:
            return []
        rows = list(s.scalars(
            select(RawItem)
            .join(ClusterItem, ClusterItem.raw_item_id == RawItem.id)
            .where(ClusterItem.cluster_id == topic.cluster_id)
        ))
        rows.sort(key=lambda r: (r.source_tier, -(r.published_at or r.fetched_at).timestamp()))
        picked, seen_domains = [], set()
        for row in rows:
            if row.domain in seen_domains:
                continue
            # A homepage or section index scrapes "successfully" and returns navigation
            # boilerplate, so drop it rather than let it reach the fact extractor.
            if not is_article_url(row.canonical_url):
                log.info("skipping non-article URL for topic %s: %s", topic_id, row.canonical_url)
                continue
            seen_domains.add(row.domain)
            picked.append(row)
            if len(picked) >= limit:
                break
        return picked
