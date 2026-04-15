"""Tests for alert suppression — 'don't page me for this'."""


from nthlayer_respond.sre.suppression import (
    Suppression,
    check_suppression_override,
    create_suppression,
)


class TestCreateSuppression:
    """Test suppression rule creation."""

    def test_creates_suppression_with_baseline(self):
        suppression = create_suppression(
            service="payment-api",
            metric="latency_p99",
            window={"type": "daily", "start": "02:00", "end": "04:00"},
            reason="nightly backup",
            baseline=350.0,
            override_multiplier=3.0,
            created_by="human:rob",
        )

        assert isinstance(suppression, Suppression)
        assert suppression.service == "payment-api"
        assert suppression.metric == "latency_p99"
        assert suppression.baseline == 350.0
        assert suppression.override_threshold == 1050.0  # 350 * 3
        assert suppression.reason == "nightly backup"
        assert suppression.created_by == "human:rob"

    def test_default_multiplier_is_3(self):
        suppression = create_suppression(
            service="payment-api",
            metric="latency_p99",
            window={"type": "daily", "start": "02:00", "end": "04:00"},
            reason="backup",
            baseline=100.0,
        )

        assert suppression.override_threshold == 300.0

    def test_window_stored(self):
        window = {"type": "daily", "start": "02:00", "end": "04:00", "timezone": "Europe/Dublin"}
        suppression = create_suppression(
            service="cache-service",
            metric="memory_usage",
            window=window,
            reason="gc cycle",
            baseline=500.0,
        )

        assert suppression.window == window

    def test_review_after_set(self):
        suppression = create_suppression(
            service="payment-api",
            metric="latency_p99",
            window={"type": "daily", "start": "02:00", "end": "04:00"},
            reason="backup",
            baseline=350.0,
        )

        assert suppression.review_after is not None


class TestCheckSuppressionOverride:
    """Test suppression override detection."""

    def test_within_threshold_not_overridden(self):
        suppression = Suppression(
            service="payment-api",
            metric="latency_p99",
            window={"type": "daily", "start": "02:00", "end": "04:00"},
            reason="backup",
            baseline=350.0,
            override_threshold=1050.0,
            created_by="human:rob",
        )

        assert check_suppression_override(suppression, 900.0) is False

    def test_exceeds_threshold_is_overridden(self):
        suppression = Suppression(
            service="payment-api",
            metric="latency_p99",
            window={"type": "daily", "start": "02:00", "end": "04:00"},
            reason="backup",
            baseline=350.0,
            override_threshold=1050.0,
            created_by="human:rob",
        )

        assert check_suppression_override(suppression, 1840.0) is True

    def test_exactly_at_threshold_not_overridden(self):
        suppression = Suppression(
            service="payment-api",
            metric="latency_p99",
            window={},
            reason="test",
            baseline=100.0,
            override_threshold=300.0,
            created_by="test",
        )

        # At exactly the threshold, not overridden (must exceed)
        assert check_suppression_override(suppression, 300.0) is False
