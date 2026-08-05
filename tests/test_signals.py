"""Unit tests untuk SignalEngine (logika murni, tanpa network)."""

import unittest

from analysis.signals import SignalEngine, Signal, SignalType, AggregatedSignal


def _make_bars(count=25, start=1.0, step=0.01):
    """Synthetic OHLCV dengan tren naik konsisten."""
    bars = []
    for i in range(count):
        close = start + i * step
        bars.append({
            "date": f"2026-08-{i + 1:02d}",
            "open": close - step,
            "high": close + step * 0.5,
            "low": close - step * 1.5,
            "close": close,
            "volume": 1000 + i * 10,
        })
    return bars


class TestEvaluateFromData(unittest.TestCase):
    def test_insufficient_data_neutral(self):
        sig = SignalEngine().evaluate_from_data([{"close": 1.0}])
        self.assertEqual(sig.type, SignalType.NEUTRAL)
        self.assertEqual(sig.confidence, 0.0)

    def test_uptrend_produces_signal(self):
        sig = SignalEngine().evaluate_from_data(_make_bars())
        self.assertIn(sig.type, SignalType)
        self.assertGreaterEqual(sig.confidence, 0.0)
        self.assertLessEqual(sig.confidence, 1.0)
        self.assertTrue(sig.components)

    def test_indicators_used_when_provided(self):
        sig = SignalEngine().evaluate_from_data(
            _make_bars(), technical_indicators={"rsi": 75.0}
        )
        sources = {c.source for c in sig.components}
        self.assertIn("momentum", sources)

    def test_aggregate_bullish_components(self):
        engine = SignalEngine()
        engine.signals = [
            Signal(type=SignalType.BULLISH, confidence=0.8, reason="r1", source="a"),
            Signal(type=SignalType.BULLISH, confidence=0.7, reason="r2", source="b"),
        ]
        sig = engine._aggregate()
        self.assertEqual(sig.type, SignalType.BULLISH)

    def test_aggregate_empty_neutral(self):
        sig = SignalEngine()._aggregate()
        self.assertEqual(sig.type, SignalType.NEUTRAL)
        self.assertEqual(sig.confidence, 0.0)


class TestEvaluateFromContext(unittest.TestCase):
    def test_bullish_context(self):
        sig = SignalEngine().evaluate_from_context(
            "harga naik menguat optimis positif rally"
        )
        self.assertEqual(sig.type, SignalType.BULLISH)

    def test_bearish_context(self):
        sig = SignalEngine().evaluate_from_context(
            "harga turun melemah negatif pesimis"
        )
        self.assertEqual(sig.type, SignalType.BEARISH)

    def test_neutral_context(self):
        sig = SignalEngine().evaluate_from_context("cuaca hari ini cerah")
        self.assertEqual(sig.type, SignalType.NEUTRAL)

    def test_structured_llm_signals(self):
        sig = SignalEngine().evaluate_from_context(
            "context",
            llm_analysis_signals=[
                {"type": "bullish", "confidence": 0.8, "reason": "r", "source": "llm"},
            ],
        )
        self.assertEqual(sig.type, SignalType.BULLISH)
        self.assertEqual(sig.confidence, 0.8)


class TestSignalHelpers(unittest.TestCase):
    def test_str_to_signal_type(self):
        self.assertEqual(SignalEngine._str_to_signal_type("STRONG_BULLISH"), SignalType.STRONG_BULLISH)
        self.assertEqual(SignalEngine._str_to_signal_type("buy"), SignalType.BULLISH)
        self.assertEqual(SignalEngine._str_to_signal_type("sell"), SignalType.BEARISH)
        self.assertEqual(SignalEngine._str_to_signal_type("unknown"), SignalType.NEUTRAL)

    def test_signal_str_has_emoji(self):
        s = Signal(type=SignalType.BULLISH, confidence=0.5, reason="r", source="trend")
        self.assertIn("🟢", str(s))

    def test_aggregated_to_dict(self):
        sig = AggregatedSignal(
            type=SignalType.BULLISH,
            confidence=0.5,
            components=[Signal(type=SignalType.BULLISH, confidence=0.5, reason="r", source="trend")],
            summary="s",
        )
        d = sig.to_dict()
        self.assertEqual(d["type"], "bullish")
        self.assertEqual(len(d["components"]), 1)


if __name__ == "__main__":
    unittest.main()
