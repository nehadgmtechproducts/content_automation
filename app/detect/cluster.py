"""Semantic clustering of headlines inside a time window.

Entity match alone over-merges: on any given day 'OpenAI' appears in a dozen unrelated
stories, and keying purely on the entity would fuse them into one meaningless blob.
Embedding similarity separates them; the entity then keys the baseline lookup.
"""
from __future__ import annotations

import functools
import hashlib
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import numpy as np


class Embedder(Protocol):
    def encode(self, texts: Sequence[str]) -> np.ndarray: ...


@functools.lru_cache(maxsize=1)
def default_embedder(model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name)

    class _ST:
        def encode(self, texts: Sequence[str]) -> np.ndarray:
            return model.encode(
                list(texts), normalize_embeddings=True, show_progress_bar=False
            )

    return _ST()


@dataclass
class Candidate:
    """One headline entering the clusterer."""
    item_id: int
    title: str
    domain: str
    tier: int
    entities: list[str]


@dataclass
class Cluster:
    members: list[Candidate] = field(default_factory=list)
    similarities: list[float] = field(default_factory=list)
    centroid: np.ndarray | None = None

    @property
    def distinct_domains(self) -> int:
        return len({m.domain for m in self.members})

    @property
    def has_tier1(self) -> bool:
        return any(m.tier == 1 for m in self.members)

    @property
    def representative(self) -> Candidate:
        """Prefer a tier-1 headline as the cluster's public title."""
        return min(self.members, key=lambda m: (m.tier, -len(m.title)))

    def entity_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for m in self.members:
            for e in m.entities:
                counts[e] = counts.get(e, 0) + 1
        return counts

    def primary_entity(self) -> str | None:
        counts = self.entity_counts()
        if not counts:
            return None
        # Most shared entity wins; ties broken alphabetically for determinism.
        return max(sorted(counts), key=lambda e: counts[e])

    def fingerprint(self) -> str:
        """Stable id for this story, used for 24h re-fire suppression.

        Keyed on the primary entity plus the shared vocabulary of the cluster, so the
        same story arriving an hour later with new outlets still matches.
        """
        entity = self.primary_entity() or ""
        shared = sorted(e for e, c in self.entity_counts().items() if c >= 2)
        basis = "|".join([entity, *shared]) or self.representative.title.lower()
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def cosine_matrix(vectors: np.ndarray) -> np.ndarray:
    """Pairwise cosine similarity for L2-normalized rows."""
    return np.clip(vectors @ vectors.T, -1.0, 1.0)


def cluster_candidates(
    candidates: list[Candidate],
    embedder: Embedder,
    threshold: float = 0.78,
    min_title_chars: int = 20,
) -> list[Cluster]:
    """Greedy agglomerative clustering by embedding similarity.

    Greedy (not k-means/HDBSCAN) on purpose: the window holds tens-to-hundreds of items,
    cluster count is unknown up front, and greedy is deterministic and trivially
    explainable when you're debugging why two stories merged.
    """
    usable = [c for c in candidates if len(c.title) >= min_title_chars]
    if not usable:
        return []
    if len(usable) == 1:
        return [Cluster(members=[usable[0]], similarities=[1.0])]

    vectors = np.asarray(embedder.encode([c.title for c in usable]), dtype=np.float32)
    sims = cosine_matrix(vectors)

    assigned: dict[int, int] = {}
    clusters: list[Cluster] = []

    for i, cand in enumerate(usable):
        best_cluster, best_sim = None, threshold
        for ci, cluster in enumerate(clusters):
            # Average-linkage: compare against every member, not just the seed, so a
            # cluster doesn't drift away from its own centroid as it grows.
            member_sims = [sims[i][assigned_i] for assigned_i, c in assigned.items() if c == ci]
            if not member_sims:
                continue
            avg = float(np.mean(member_sims))
            if avg >= best_sim:
                best_cluster, best_sim = ci, avg

        if best_cluster is None:
            clusters.append(Cluster(members=[cand], similarities=[1.0], centroid=vectors[i]))
            assigned[i] = len(clusters) - 1
        else:
            clusters[best_cluster].members.append(cand)
            clusters[best_cluster].similarities.append(best_sim)
            assigned[i] = best_cluster

    return clusters
