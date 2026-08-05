"""
Scenarios Agent — Generates multiple market scenarios with probability estimates.
Adapted from MarketLens BTC's ScenariosAgent.

Generates THREE scenarios:
1. BULL CASE — Optimistic but realistic
2. BEAR CASE — Pessimistic but realistic
3. BASE CASE — Most likely outcome

Each scenario includes probability, catalysts, and impact assessment.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from analysis.prompts import SCENARIOS_SYSTEM, SCENARIOS_TEMPLATE
from data.cache import clean_json_response

logger = logging.getLogger(__name__)


@dataclass
class Scenario:
    """A possible market scenario."""
    name: str
    description: str
    probability: int  # 0-100
    key_catalysts: List[str] = field(default_factory=list)
    impact_level: str = "medium"  # high, medium, low

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "description": self.description,
            "probability": self.probability,
            "key_catalysts": self.key_catalysts,
            "impact_level": self.impact_level,
        }


class ScenariosAgent:
    """
    Generates multiple market scenarios to help users understand
    the range of possible outcomes.
    """

    def __init__(self, ai_engine: Any):
        self.ai = ai_engine

    async def generate(
        self,
        question: str,
        market_data: str,
        thesis_output: str,
        contradiction_output: str,
    ) -> List[Scenario]:
        """
        Generate bull, bear, and base market scenarios.

        Args:
            question: Original user question
            market_data: Market context data
            thesis_output: Thesis summary
            contradiction_output: Contradiction analysis

        Returns:
            List of 3 Scenario objects with probabilities summing to ~100%
        """
        logger.info("Generating market scenarios...")

        if not thesis_output:
            return [
                Scenario(name="Base Case", description="Data tidak mencukupi untuk analisis skenario", probability=100),
            ]

        prompt = SCENARIOS_TEMPLATE.format(
            question=question,
            market_data=market_data[:1500] if market_data else "No market data",
            thesis_output=thesis_output[:1000] if thesis_output else "No thesis",
            contradiction_output=contradiction_output[:800] if contradiction_output else "No contradictions found",
        )

        # generate() sinkron (requests) → jalankan di thread agar tidak
        # memblokir event loop dan bisa paralel dengan agent lain.
        response = await asyncio.to_thread(
            self.ai.generate,
            prompt,
            use_cache=True,
            system_override=SCENARIOS_SYSTEM,
        )

        return self._parse_response(response)

    def _parse_response(self, response: str) -> List[Scenario]:
        """Parse LLM response into Scenario list."""
        scenarios = []

        try:
            text = clean_json_response(response)
            data = json.loads(text)
            items = data.get("scenarios", data.get("results", []))

            for item in items:
                if isinstance(item, dict):
                    scenarios.append(Scenario(
                        name=item.get("name", "Scenario"),
                        description=item.get("description", ""),
                        probability=min(100, max(0, int(item.get("probability", 33)))),
                        key_catalysts=item.get("key_catalysts", []),
                        impact_level=item.get("impact_level", "medium"),
                    ))
        except (json.JSONDecodeError, IndexError, ValueError) as e:
            logger.warning(f"Failed to parse scenarios: {e}")

        # Ensure we have exactly 3 scenarios
        if not scenarios:
            scenarios = [
                Scenario(name="Bull Case", description="Skenario optimis", probability=33),
                Scenario(name="Bear Case", description="Skenario pesimis", probability=33),
                Scenario(name="Base Case", description="Skenario paling mungkin", probability=34),
            ]

        return scenarios[:3]  # Max 3 scenarios
