from app.detect.cluster import Candidate, Cluster
from app.detect.velocity import BaselineStats, evaluate, update_ewma

CFG = {
    "trigger": {"min_distinct_domains": 3, "require_tier1": True,
                "baseline_sigma": 2.0, "suppression_hours": 24},
    "baseline": {"ewma_alpha": 0.15, "min_observations": 8},
}


def cluster_with(domains, tiers=None):
    tiers = tiers or [1] * len(domains)
    return Cluster(members=[
        Candidate(i, f"Story about the thing number {i}", d, t, ["Nvidia"])
        for i, (d, t) in enumerate(zip(domains, tiers))
    ])


def test_fires_on_three_distinct_domains_with_tier1():
    d = evaluate(cluster_with(["a.com", "b.com", "c.com"]), BaselineStats(), CFG)
    assert d.fired


def test_does_not_fire_below_domain_floor():
    d = evaluate(cluster_with(["a.com", "b.com"]), BaselineStats(), CFG)
    assert not d.fired and "distinct domain" in d.reason


def test_does_not_fire_without_tier1_source():
    d = evaluate(cluster_with(["a.com", "b.com", "c.com"], tiers=[3, 3, 3]),
                 BaselineStats(), CFG)
    assert not d.fired and "tier-1" in d.reason


def test_busy_entity_needs_a_real_spike():
    """The whole point: 'OpenAI' routinely clears 3 domains, so once we have history
    the bar rises to baseline + 2 sigma."""
    busy = BaselineStats(mean=6.0, variance=1.0, observations=40)
    assert not evaluate(cluster_with([f"{i}.com" for i in range(5)]), busy, CFG).fired
    assert evaluate(cluster_with([f"{i}.com" for i in range(9)]), busy, CFG).fired


def test_quiet_entity_fires_at_the_floor():
    quiet = BaselineStats(mean=0.4, variance=0.1, observations=40)
    assert evaluate(cluster_with(["a.com", "b.com", "c.com"]), quiet, CFG).fired


def test_thin_history_falls_back_to_flat_rule():
    thin = BaselineStats(mean=9.0, variance=4.0, observations=2)
    d = evaluate(cluster_with(["a.com", "b.com", "c.com"]), thin, CFG)
    assert d.fired and d.threshold == 3.0


def test_suppression_blocks_refire():
    d = evaluate(cluster_with(["a.com", "b.com", "c.com"]), BaselineStats(), CFG,
                 suppressed=True)
    assert not d.fired and "suppressed" in d.reason


def test_ewma_converges_and_tracks_variance():
    stats = BaselineStats()
    for _ in range(60):
        stats = update_ewma(stats, 5.0, 0.15)
    assert abs(stats.mean - 5.0) < 0.01
    assert stats.stddev < 0.1

    spiked = update_ewma(stats, 20.0, 0.15)
    assert spiked.mean > stats.mean
    assert spiked.stddev > stats.stddev


def test_first_observation_seeds_the_mean():
    stats = update_ewma(BaselineStats(), 4.0, 0.15)
    assert stats.mean == 4.0 and stats.observations == 1
