"""
Signal Engine — Aggregates technical indicators and market data into unified signals
with confidence scoring. Adapted from MarketLens BTC's SignalEngine.

Provides:
- Trend signal evaluation (SMA/EMA crossovers)
- Momentum signal evaluation (RSI, Stochastic)
- Volatility signal evaluation (Bollinger Bands)
- Multi-source signal aggregation with confidence scoring
"""

import logging
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class SignalType(Enum):
    """Trading signal types with directional strength."""
    STRONG_BULLISH = "strong_bullish"
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"
    STRONG_BEARISH = "strong_bearish"


@dataclass
class Signal:
    """Represents a single analysis signal with confidence."""
    type: SignalType
    confidence: float  # 0.0 to 1.0
    reason: str
    source: str  # e.g., "trend", "momentum", "volatility", "volume"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        emoji = {
            SignalType.STRONG_BULLISH: "🟢🟢",
            SignalType.BULLISH: "🟢",
            SignalType.NEUTRAL: "⚪",
            SignalType.BEARISH: "🔴",
            SignalType.STRONG_BEARISH: "🔴🔴",
        }
        return f"{emoji.get(self.type, '⚪')} {self.source}: {self.reason} (conf={self.confidence:.0%})"


@dataclass
class AggregatedSignal:
    """Aggregated signal combining multiple signal sources."""
    type: SignalType
    confidence: float  # 0.0 to 1.0
    components: List[Signal] = field(default_factory=list)
    summary: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict:
        """Convert to dict for prompt context."""
        return {
            "type": self.type.value,
            "confidence": self.confidence,
            "summary": self.summary,
            "components": [
                {
                    "source": s.source,
                    "type": s.type.value,
                    "confidence": s.confidence,
                    "reason": s.reason,
                }
                for s in self.components
            ],
        }


class SignalEngine:
    """
    Aggregates multiple signal sources into unified analysis signals.

    Evaluates:
    - Trend signals (SMA/EMA, MACD)
    - Momentum signals (RSI, Stochastic)
    - Volatility signals (Bollinger Bands, ATR)
    - Volume signals (volume trend)

    Can work with both live technical data and LLM-analyzed market context.
    """

    def __init__(self):
        self.signals: List[Signal] = []

    def evaluate_from_data(
        self,
        ohlcv_data: List[Dict],
        technical_indicators: Optional[Dict] = None,
    ) -> AggregatedSignal:
        """
        Evaluate signals from OHLCV data and technical indicators.

        Args:
            ohlcv_data: List of OHLCV dicts with date, open, high, low, close, volume
            technical_indicators: Optional pre-computed indicators (RSI, MACD, etc.)

        Returns:
            AggregatedSignal with combined analysis
        """
        self.signals = []

        if not ohlcv_data or len(ohlcv_data) < 5:
            logger.warning("Not enough data for signal evaluation")
            return AggregatedSignal(
                type=SignalType.NEUTRAL,
                confidence=0.0,
                summary="Data tidak mencukupi untuk analisis sinyal",
            )

        # Extract closing prices
        closes = [d.get("close", 0) for d in ohlcv_data]
        volumes = [d.get("volume", 0) for d in ohlcv_data]
        highs = [d.get("high", 0) for d in ohlcv_data]
        lows = [d.get("low", 0) for d in ohlcv_data]

        # Trend signals
        trend_signal = self._evaluate_trend(closes)
        if trend_signal:
            self.signals.append(trend_signal)

        # Momentum signals from indicators or price action
        if technical_indicators:
            momentum_signal = self._evaluate_momentum_from_indicators(technical_indicators)
        else:
            momentum_signal = self._evaluate_momentum_price_action(closes)
        if momentum_signal:
            self.signals.append(momentum_signal)

        # Volatility signals
        vol_signal = self._evaluate_volatility(closes, highs, lows)
        if vol_signal:
            self.signals.append(vol_signal)

        # Volume signals
        vol_signal = self._evaluate_volume(volumes)
        if vol_signal:
            self.signals.append(vol_signal)

        # Aggregate
        return self._aggregate()

    def evaluate_from_context(
        self,
        market_context: str,
        llm_analysis_signals: Optional[List[Dict]] = None,
    ) -> AggregatedSignal:
        """
        Evaluate signals from LLM-analyzed market context.

        Args:
            market_context: Text description of market conditions
            llm_analysis_signals: Optional pre-parsed signal dicts from LLM

        Returns:
            AggregatedSignal
        """
        # If we have structured LLM signals, use them
        if llm_analysis_signals:
            for sig in llm_analysis_signals:
                signal_type = self._str_to_signal_type(sig.get("type", "neutral"))
                self.signals.append(Signal(
                    type=signal_type,
                    confidence=sig.get("confidence", 0.5),
                    reason=sig.get("reason", ""),
                    source=sig.get("source", "llm_analysis"),
                ))

        # If only text context, create a neutral signal based on text analysis
        if not self.signals:
            bullish_count = sum(1 for w in ["bullish", "naik", "positif", "menguat", "optimis"]
                              if w in market_context.lower())
            bearish_count = sum(1 for w in ["bearish", "turun", "negatif", "melemah", "pesimis"]
                              if w in market_context.lower())

            if bullish_count > bearish_count + 2:
                self.signals.append(Signal(
                    type=SignalType.BULLISH,
                    confidence=0.5,
                    reason="Market context menunjukkan sentimen positif",
                    source="context_analysis",
                ))
            elif bearish_count > bullish_count + 2:
                self.signals.append(Signal(
                    type=SignalType.BEARISH,
                    confidence=0.5,
                    reason="Market context menunjukkan sentimen negatif",
                    source="context_analysis",
                ))
            else:
                self.signals.append(Signal(
                    type=SignalType.NEUTRAL,
                    confidence=0.5,
                    reason="Market context tidak menunjukkan bias jelas",
                    source="context_analysis",
                ))

        return self._aggregate()

    def _evaluate_trend(self, closes: List[float]) -> Optional[Signal]:
        """Evaluate trend from closing prices using SMA crossover logic."""
        if len(closes) < 20:
            return None

        # Simple SMA calculations
        sma_5 = sum(closes[-5:]) / 5
        sma_10 = sum(closes[-10:]) / 10
        sma_20 = sum(closes[-20:]) / 20

        # Price vs SMAs
        current_price = closes[-1]
        price_above_sma20 = current_price > sma_20

        # SMA crossover detection
        prev_sma5 = sum(closes[-6:-1]) / 5
        prev_sma20 = sum(closes[-21:-1]) / 20

        # Trend direction
        trend_up = sma_5 > sma_10 > sma_20
        trend_down = sma_5 < sma_10 < sma_20

        # Momentum of trend
        momentum = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] > 0 else 0

        if trend_up and momentum > 0.01:
            return Signal(
                type=SignalType.BULLISH,
                confidence=min(0.8, 0.5 + abs(momentum) * 5),
                reason=f"Trend naik (SMA5={sma_5:.2f} > SMA20={sma_20:.2f})",
                source="trend",
            )
        elif trend_down and momentum < -0.01:
            return Signal(
                type=SignalType.BEARISH,
                confidence=min(0.8, 0.5 + abs(momentum) * 5),
                reason=f"Trend turun (SMA5={sma_5:.2f} < SMA20={sma_20:.2f})",
                source="trend",
            )
        elif price_above_sma20:
            return Signal(
                type=SignalType.BULLISH,
                confidence=0.3,
                reason="Harga di atas SMA20 (bias positif ringan)",
                source="trend",
            )
        else:
            return Signal(
                type=SignalType.BEARISH,
                confidence=0.3,
                reason="Harga di bawah SMA20 (bias negatif ringan)",
                source="trend",
            )

    def _evaluate_momentum_from_indicators(self, indicators: Dict) -> Optional[Signal]:
        """Evaluate momentum from pre-computed technical indicators."""
        rsi = indicators.get("rsi")
        macd = indicators.get("macd")
        macd_signal = indicators.get("macd_signal")

        if rsi is not None:
            if rsi > 70:
                return Signal(
                    type=SignalType.BEARISH,
                    confidence=min(0.8, (rsi - 70) / 30 * 0.5 + 0.5),
                    reason=f"RSI overbought ({rsi:.1f}) — potensi koreksi",
                    source="momentum",
                )
            elif rsi < 30:
                return Signal(
                    type=SignalType.BULLISH,
                    confidence=min(0.8, (30 - rsi) / 30 * 0.5 + 0.5),
                    reason=f"RSI oversold ({rsi:.1f}) — potensi rebound",
                    source="momentum",
                )
            elif rsi > 60:
                return Signal(
                    type=SignalType.BULLISH,
                    confidence=0.4,
                    reason=f"RSI menguat ({rsi:.1f})",
                    source="momentum",
                )
            elif rsi < 40:
                return Signal(
                    type=SignalType.BEARISH,
                    confidence=0.4,
                    reason=f"RSI melemah ({rsi:.1f})",
                    source="momentum",
                )

        # MACD check
        if macd is not None and macd_signal is not None:
            if macd > macd_signal and macd > 0:
                return Signal(
                    type=SignalType.BULLISH,
                    confidence=0.5,
                    reason="MACD positif dan di atas signal line",
                    source="momentum",
                )
            elif macd < macd_signal and macd < 0:
                return Signal(
                    type=SignalType.BEARISH,
                    confidence=0.5,
                    reason="MACD negatif dan di bawah signal line",
                    source="momentum",
                )

        return None

    def _evaluate_momentum_price_action(self, closes: List[float]) -> Optional[Signal]:
        """Evaluate momentum from raw price action (fallback when no indicators)."""
        if len(closes) < 10:
            return None

        # Rate of change
        roc_5 = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] > 0 else 0
        roc_10 = (closes[-1] - closes[-10]) / closes[-10] if closes[-10] > 0 else 0

        # Consecutive candles direction
        up_candles = sum(1 for i in range(-5, 0) if closes[i] > closes[i-1])
        down_candles = 5 - up_candles

        if roc_5 > 0.02 and up_candles >= 4:
            return Signal(
                type=SignalType.STRONG_BULLISH,
                confidence=0.7,
                reason=f"Momentum kuat ({roc_5:.1%} dalam 5 periode)",
                source="momentum",
            )
        elif roc_5 > 0.01 and up_candles >= 3:
            return Signal(
                type=SignalType.BULLISH,
                confidence=0.5,
                reason=f"Momentum positif ({roc_5:.1%} dalam 5 periode)",
                source="momentum",
            )
        elif roc_5 < -0.02 and down_candles >= 4:
            return Signal(
                type=SignalType.STRONG_BEARISH,
                confidence=0.7,
                reason=f"Momentum negatif kuat ({roc_5:.1%} dalam 5 periode)",
                source="momentum",
            )
        elif roc_5 < -0.01 and down_candles >= 3:
            return Signal(
                type=SignalType.BEARISH,
                confidence=0.5,
                reason=f"Momentum negatif ({roc_5:.1%} dalam 5 periode)",
                source="momentum",
            )

        return None

    def _evaluate_volatility(self, closes: List[float], highs: List[float], lows: List[float]) -> Optional[Signal]:
        """Evaluate volatility from price data."""
        if len(closes) < 10:
            return None

        # Calculate ATR-like measure
        true_ranges = []
        for i in range(1, min(len(closes), 14)):
            tr = max(
                highs[-i] - lows[-i],
                abs(highs[-i] - closes[-i-1]),
                abs(lows[-i] - closes[-i-1]),
            )
            true_ranges.append(tr)

        if not true_ranges:
            return None

        avg_true_range = sum(true_ranges) / len(true_ranges)
        current_price = closes[-1]
        volatility_pct = (avg_true_range / current_price * 100) if current_price > 0 else 0

        # Compare to historical volatility
        older_ranges = []
        for i in range(14, min(len(closes), 28)):
            if i < len(highs) - 1:
                tr = max(
                    highs[-i] - lows[-i],
                    abs(highs[-i] - closes[-i-1]),
                    abs(lows[-i] - closes[-i-1]),
                )
                older_ranges.append(tr)

        if older_ranges:
            older_avg = sum(older_ranges) / len(older_ranges)
            vol_ratio = avg_true_range / older_avg if older_avg > 0 else 1
        else:
            vol_ratio = 1

        if vol_ratio > 1.5:
            return Signal(
                type=SignalType.NEUTRAL,
                confidence=0.6,
                reason=f"Volatilitas tinggi ({volatility_pct:.1f}%) — waspada false breakout",
                source="volatility",
            )
        elif vol_ratio < 0.5:
            return Signal(
                type=SignalType.NEUTRAL,
                confidence=0.4,
                reason="Volatilitas rendah — market mungkin sedang menunggu katalis",
                source="volatility",
            )

        return None

    def _evaluate_volume(self, volumes: List[float]) -> Optional[Signal]:
        """Evaluate volume trend."""
        if len(volumes) < 10:
            return None

        recent_vol = sum(volumes[-5:]) / 5
        older_vol = sum(volumes[-10:-5]) / 5 if len(volumes) >= 10 else recent_vol

        vol_ratio = recent_vol / older_vol if older_vol > 0 else 1

        if vol_ratio > 2.0:
            return Signal(
                type=SignalType.NEUTRAL,
                confidence=0.5,
                reason=f"Volume tinggi ({vol_ratio:.1f}x rata-rata) — konfirmasi jika searah trend",
                source="volume",
            )
        elif vol_ratio > 1.5:
            return Signal(
                type=SignalType.NEUTRAL,
                confidence=0.3,
                reason="Volume di atas rata-rata — minat pasar meningkat",
                source="volume",
            )
        elif vol_ratio < 0.5:
            return Signal(
                type=SignalType.NEUTRAL,
                confidence=0.3,
                reason="Volume rendah — pergerakan mungkin tidak valid",
                source="volume",
            )

        return None

    def _aggregate(self) -> AggregatedSignal:
        """Combine all signals into a unified aggregated signal."""
        if not self.signals:
            return AggregatedSignal(
                type=SignalType.NEUTRAL,
                confidence=0.0,
                summary="Tidak ada sinyal yang tersedia",
            )

        # Score each signal numerically
        signal_values = {
            SignalType.STRONG_BULLISH: 2,
            SignalType.BULLISH: 1,
            SignalType.NEUTRAL: 0,
            SignalType.BEARISH: -1,
            SignalType.STRONG_BEARISH: -2,
        }

        total_score = 0
        total_weight = 0

        for signal in self.signals:
            score = signal_values.get(signal.type, 0)
            weight = signal.confidence
            total_score += score * weight
            total_weight += weight

        avg_score = total_score / total_weight if total_weight > 0 else 0
        avg_confidence = sum(s.confidence for s in self.signals) / len(self.signals)

        # Determine aggregate type
        if avg_score >= 1.5:
            agg_type = SignalType.STRONG_BULLISH
        elif avg_score >= 0.5:
            agg_type = SignalType.BULLISH
        elif avg_score <= -1.5:
            agg_type = SignalType.STRONG_BEARISH
        elif avg_score <= -0.5:
            agg_type = SignalType.BEARISH
        else:
            agg_type = SignalType.NEUTRAL

        # Summary
        non_neutral = [s for s in self.signals if s.type != SignalType.NEUTRAL]
        if non_neutral:
            summary = "; ".join(s.reason for s in non_neutral[:3])
        else:
            summary = "Tidak ada sinyal dominan — market mixed"

        return AggregatedSignal(
            type=agg_type,
            confidence=avg_confidence,
            components=self.signals,
            summary=summary,
        )

    @staticmethod
    def _str_to_signal_type(s: str) -> SignalType:
        """Convert string to SignalType."""
        mapping = {
            "strong_bullish": SignalType.STRONG_BULLISH,
            "bullish": SignalType.BULLISH,
            "neutral": SignalType.NEUTRAL,
            "bearish": SignalType.BEARISH,
            "strong_bearish": SignalType.STRONG_BEARISH,
            "strong_buy": SignalType.STRONG_BULLISH,
            "buy": SignalType.BULLISH,
            "sell": SignalType.BEARISH,
            "strong_sell": SignalType.STRONG_BEARISH,
        }
        return mapping.get(s.lower(), SignalType.NEUTRAL)

    def get_summary_for_prompt(self) -> str:
        """Get signal summary formatted for LLM prompts."""
        if not self.signals:
            return "No signals generated yet."

        lines = []
        for sig in self.signals:
            lines.append(str(sig))

        return "\n".join(lines)
