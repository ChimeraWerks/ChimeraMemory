from chimera_memory.cognitive import compute_zone_score


def test_zone_score_biases_by_review_status() -> None:
    base = compute_zone_score(importance=7, access_count=2, days_since_access=10, failure_count=0)
    confirmed = compute_zone_score(
        importance=7,
        access_count=2,
        days_since_access=10,
        failure_count=0,
        review_status="confirmed",
    )
    evidence_only = compute_zone_score(
        importance=7,
        access_count=2,
        days_since_access=10,
        failure_count=0,
        review_status="evidence_only",
    )
    pending = compute_zone_score(
        importance=7,
        access_count=2,
        days_since_access=10,
        failure_count=0,
        review_status="pending",
    )

    assert confirmed == min(1.0, round(base + 0.15, 4))
    assert evidence_only == round(base + 0.05, 4)
    assert pending == round(base - 0.08, 4)
