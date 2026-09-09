import numpy as np

from app.detect.cluster import Candidate, cluster_candidates


class KeywordEmbedder:
    """Deterministic stand-in for the sentence transformer: bag-of-words over a fixed
    vocabulary, L2-normalized. Enough to exercise the clustering logic offline."""

    VOCAB = ["nvlink", "nvidia", "license", "openai", "lawsuit", "copyright", "chip", "funding"]

    def encode(self, texts):
        rows = []
        for text in texts:
            lowered = text.lower()
            vec = np.array([1.0 if word in lowered else 0.0 for word in self.VOCAB])
            norm = np.linalg.norm(vec)
            rows.append(vec / norm if norm else vec)
        return np.array(rows, dtype=np.float32)


def make(idx, title, domain, tier=1, entities=None):
    return Candidate(idx, title, domain, tier, entities or [])


def test_same_story_across_outlets_merges():
    cands = [
        make(1, "Nvidia will license NVLink to rivals", "techcrunch.com", 1, ["Nvidia"]),
        make(2, "Nvidia to license NVLink interconnect", "theverge.com", 1, ["Nvidia"]),
        make(3, "Nvidia opens NVLink license programme", "arstechnica.com", 1, ["Nvidia"]),
    ]
    clusters = cluster_candidates(cands, KeywordEmbedder(), threshold=0.7)
    assert len(clusters) == 1
    assert clusters[0].distinct_domains == 3
    assert clusters[0].has_tier1


def test_unrelated_stories_about_one_entity_stay_apart():
    """The failure mode entity-only matching would cause: two different OpenAI stories
    must not become one cluster."""
    cands = [
        make(1, "OpenAI faces copyright lawsuit over training", "nyt.com", 1, ["OpenAI"]),
        make(2, "OpenAI sued in copyright lawsuit", "reuters.com", 1, ["OpenAI"]),
        make(3, "OpenAI announces new funding round", "techcrunch.com", 1, ["OpenAI"]),
    ]
    clusters = cluster_candidates(cands, KeywordEmbedder(), threshold=0.7)
    assert len(clusters) == 2
    sizes = sorted(len(c.members) for c in clusters)
    assert sizes == [1, 2]


def test_distinct_domains_not_item_count():
    """Wire copy republished by the same outlet is one source, not three."""
    cands = [
        make(1, "Nvidia will license NVLink to rivals", "techcrunch.com", 1, ["Nvidia"]),
        make(2, "Nvidia will license NVLink to rivals", "techcrunch.com", 1, ["Nvidia"]),
        make(3, "Nvidia will license NVLink to rivals", "techcrunch.com", 1, ["Nvidia"]),
    ]
    clusters = cluster_candidates(cands, KeywordEmbedder(), threshold=0.7)
    assert clusters[0].distinct_domains == 1


def test_short_titles_are_dropped():
    cands = [make(1, "Nvidia", "x.com", 1, ["Nvidia"])]
    assert cluster_candidates(cands, KeywordEmbedder(), min_title_chars=20) == []


def test_fingerprint_is_stable_across_member_order():
    a = [make(1, "Nvidia will license NVLink to rivals", "a.com", 1, ["Nvidia", "NVLink"]),
         make(2, "Nvidia to license NVLink interconnect", "b.com", 1, ["Nvidia", "NVLink"])]
    b = list(reversed(a))
    fa = cluster_candidates(a, KeywordEmbedder(), threshold=0.7)[0].fingerprint()
    fb = cluster_candidates(b, KeywordEmbedder(), threshold=0.7)[0].fingerprint()
    assert fa == fb
