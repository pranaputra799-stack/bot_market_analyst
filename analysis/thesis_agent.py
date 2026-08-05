"""
Thesis Agent — Formulates structured market analysis with directional bias.
Adapted from MarketLens BTC's ThesisAgent.

Takes research context and technical signals, then produces:
- Clear directional bias (bullish/bearish/neutral)
- Supporting evidence
- Key catalysts
- Time horizon
- Risk factors
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from analysis.prompts import THESIS_SYSTEM, THESIS_FORMULATION_TEMPLATE
from analysis.signals import AggregatedSignal
from data.cache import cache, safe_hash, clean_json_response

logger = logging.getLogger(__name__)


@dataclass
class Thesis:
    """A structured market analysis thesis."""
    direction: str  # bullish, bearish, neutral
    confidence: float  # 0.0 to 1.0
    summary: str = ""
    key_evidence: List[str] = field(default_factory=list)
    time_horizon: str = "short_term"  # short_term, medium_term, structural
    risk_factors: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict:
        return {
            "direction": self.direction,
            "confidence": self.confidence,
            "summary": self.summary,
            "key_evidence": self.key_evidence,
            "time_horizon": self.time_horizon,
            "risk_factors": self.risk_factors,
        }


class ThesisAgent:
    """
    Formulates structured market thesis from research context and signals.

    The thesis provides a clear, data-driven view of market direction
    with supporting evidence and identified risks.
    """

    def __init__(self, ai_engine: Any):
        self.ai = ai_engine

    async def formulate(
        self,
        question: str,
        research_context: Any,
        signal: Optional[AggregatedSignal] = None,
        intent: str = "general",
    ) -> Thesis:
        """
        Formulate a market thesis from research and signals.

        Args:
            question: Original user question
            research_context: ResearchContext from ResearchAgent
            signal: AggregatedSignal from SignalEngine (optional)
            intent: Detected intent from classifier

        Returns:
            Thesis with direction, confidence, evidence, and risks
        """
        logger.info(f"Formulating thesis for: {question[:80]}... (intent={intent})")

        # Use signal to guide thesis if available
        signal_output = ""
        if signal:
            signal_output = (
                f"Signal: {signal.type.value} (confidence: {signal.confidence:.0%})\n"
                f"Details: {signal.summary}"
            )

        # Pastikan research_output adalah string agar tidak error saat di-slice
        raw = research_context.llm_analysis or research_context.raw_context
        research_output = raw if isinstance(raw, str) else str(raw) if raw else ""

        # Try cache first — gunakan safe_hash() bukan hash() agar deterministik
        cache_key = f"thesis:{safe_hash(question + research_output[:300] + signal_output[:100])}"
        cached = cache.get(cache_key)
        if cached:
            try:
                data = json.loads(cached) if isinstance(cached, str) else cached
                return Thesis(
                    direction=data.get("direction", "neutral"),
                    confidence=data.get("confidence", 0.5),
                    summary=data.get("thesis_summary", ""),
                    key_evidence=data.get("key_evidence", []),
                    time_horizon=data.get("time_horizon", "short_term"),
                    risk_factors=data.get("risk_factors", []),
                )
            except (json.JSONDecodeError, TypeError):
                pass

        prompt = THESIS_FORMULATION_TEMPLATE.format(
            question=question,
            research_output=research_output[:2000] if research_output else "No research data",
            signal_output=signal_output or "No signal data",
        )

        # Pass intent to system for context-aware thesis generation
        system_prompt = THESIS_SYSTEM + (
            f"\n\nUser question type: {intent}"
            f"\nAdjust your thesis focus based on this question type."
        )

        response = self.ai.generate(
            prompt,
            use_cache=False,
            system_override=system_prompt,
        )

        thesis = self._parse_response(response)
        if not thesis:
            thesis = Thesis(direction="neutral", confidence=0.3, summary=response[:300])

        # Cache the result
        try:
            cache.set(cache_key, thesis.to_dict(), ttl=300)
        except Exception:
            pass

        return thesis

    def _parse_response(self, response: str) -> Optional[Thesis]:
        """Parse LLM response into Thesis dataclass."""
        try:
            text = clean_json_response(response)
            data = json.loads(text)
            return Thesis(
                direction=data.get("direction", "neutral"),
                confidence=min(1.0, max(0.0, float(data.get("confidence", 0.5)))),
                summary=data.get("thesis_summary", ""),
                key_evidence=data.get("key_evidence", []),
                time_horizon=data.get("time_horizon", "short_term"),
                risk_factors=data.get("risk_factors", []),
            )
        except (json.JSONDecodeError, IndexError, ValueError) as e:
            logger.warning(f"Failed to parse thesis response: {e}")
            return None
