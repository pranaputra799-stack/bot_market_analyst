"""
Contradiction Agent — Detects conflicting signals and inconsistencies in analysis.
Adapted from MarketLens BTC's ContradictionAgent.

Cross-checks:
- Technical vs fundamental analysis conflicts
- Short-term vs long-term trend conflicts
- News sentiment vs price action divergence
- Cross-asset inconsistencies
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from analysis.prompts import CONTRADICTION_SYSTEM, CONTRADICTION_TEMPLATE
from data.cache import clean_json_response

logger = logging.getLogger(__name__)


@dataclass
class Contradiction:
    """A contradiction or conflict found in the analysis."""
    description: str
    severity: str = "low"  # low, medium, high
    sources: List[str] = field(default_factory=list)
    impact: str = ""

    def to_dict(self) -> Dict:
        return {
            "description": self.description,
            "severity": self.severity,
            "sources": self.sources,
            "impact": self.impact,
        }


class ContradictionAgent:
    """
    Detects conflicting signals across analysis dimensions.

    Uses LLM to identify inconsistencies between:
    - Market data vs thesis direction
    - Technical signals vs fundamental context
    - News sentiment vs price action
    - Short-term vs longer-term outlooks
    """

    def __init__(self, ai_engine: Any):
        self.ai = ai_engine

    async def analyze(
        self,
        question: str,
        market_data: str,
        thesis_output: str,
        signal_output: str,
    ) -> List[Contradiction]:
        """
        Analyze for contradictions in the current analysis.

        Args:
            question: Original user question
            market_data: Raw market data context
            thesis_output: Thesis summary/direction
            signal_output: Technical signal summary

        Returns:
            List of Contradiction objects with severity
        """
        logger.info("Analyzing for contradictions...")

        if not thesis_output and not signal_output:
            return []

        prompt = CONTRADICTION_TEMPLATE.format(
            question=question,
            market_data=market_data[:1500] if market_data else "No market data",
            thesis_output=thesis_output[:1000] if thesis_output else "No thesis",
            signal_output=signal_output[:1000] if signal_output else "No signals",
        )

        # generate() sinkron (requests) → jalankan di thread agar tidak
        # memblokir event loop dan bisa paralel dengan agent lain.
        response = await asyncio.to_thread(
            self.ai.generate,
            prompt,
            use_cache=True,
            system_override=CONTRADICTION_SYSTEM,
        )

        return self._parse_response(response)

    def _parse_response(self, response: str) -> List[Contradiction]:
        """Parse LLM response into Contradiction list."""
        contradictions = []

        try:
            text = clean_json_response(response)
            data = json.loads(text)
            # `or default` — LLM boleh mengembalikan null eksplisit (sesuai
            # aturan anti-halusinasi "isi null jika tidak ada").
            items = data.get("contradictions") or []

            for item in items:
                if isinstance(item, dict):
                    contradictions.append(Contradiction(
                        description=item.get("description") or item.get("contradiction") or "",
                        severity=item.get("severity") or "low",
                        sources=item.get("sources") or ["llm_analysis"],
                        impact=item.get("impact") or "",
                    ))
        except (json.JSONDecodeError, IndexError) as e:
            logger.warning(f"Failed to parse contradictions: {e}")

        return contradictions

    @staticmethod
    def has_critical_contradictions(contradictions: List[Contradiction]) -> bool:
        """Check if there are any high-severity contradictions."""
        return any(c.severity == "high" for c in contradictions)
