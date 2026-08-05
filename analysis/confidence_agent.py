"""
Confidence Agent — Scores overall confidence of market analysis.
Adapted from MarketLens BTC's ConfidenceAgent.

Scoring factors:
1. EVIDENCE QUALITY (30%) — Availability and relevance of data
2. SIGNAL ALIGNMENT (25%) — How well technical signals agree
3. CONTRADICTION IMPACT (25%) — Severity of contradictions found
4. SCENARIO CLARITY (20%) — How clear the scenario picture is
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from analysis.prompts import CONFIDENCE_SYSTEM, CONFIDENCE_TEMPLATE
from analysis.signals import AggregatedSignal, SignalType
from analysis.contradiction_agent import Contradiction
from data.cache import clean_json_response

logger = logging.getLogger(__name__)


@dataclass
class ConfidenceScore:
    """Calibrated confidence score for the analysis."""
    overall: float  # 0.0 to 1.0
    level: str = "moderate"  # high, moderate, low, very_low
    evidence_quality: float = 0.0
    signal_alignment: float = 0.0
    contradiction_impact: float = 0.0
    scenario_clarity: float = 0.0
    assessment: str = ""
    limitations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "overall": self.overall,
            "level": self.level,
            "assessment": self.assessment,
            "limitations": self.limitations,
        }


class ConfidenceAgent:
    """
    Calibrates confidence by scoring evidence quality, signal alignment,
    contradiction impact, and scenario clarity.

    Combines algorithmic scoring with LLM analysis for robust calibration.
    """

    def __init__(self, ai_engine: Any):
        self.ai = ai_engine

    async def calibrate(
        self,
        question: str,
        signal: Optional[AggregatedSignal] = None,
        contradictions: Optional[List[Contradiction]] = None,
        has_research_data: bool = False,
        research_output: str = "",
        signal_output: str = "",
        contradiction_output: str = "",
        scenarios_output: str = "",
        thesis_output: str = "",
    ) -> ConfidenceScore:
        """
        Calculate calibrated confidence score for the analysis.

        Uses a combination of algorithmic scoring and LLM analysis.

        Args:
            question: Original user question
            signal: Aggregated signal (optional, for algorithmic scoring)
            contradictions: List of contradictions (optional)
            has_research_data: Whether research data was available
            research_output: Research agent output text
            signal_output: Signal agent output text
            contradiction_output: Contradiction agent output text
            scenarios_output: Scenarios agent output text
            thesis_output: Thesis agent output text

        Returns:
            ConfidenceScore with overall and component scores
        """
        logger.info("Calibrating confidence score...")

        # Algorithmic scoring
        evidence_quality = self._score_evidence_quality(has_research_data, research_output)
        signal_alignment = self._score_signal_alignment(signal)
        contradiction_impact = self._score_contradictions(contradictions)

        # Combine algorithmic scores for initial assessment
        raw_score = (
            0.30 * evidence_quality +
            0.25 * signal_alignment +
            0.25 * (1.0 - contradiction_impact)
        )
        base_level = (
            "high" if raw_score > 0.75
            else "moderate" if raw_score > 0.50
            else "low" if raw_score > 0.25
            else "very_low"
        )

        # LLM refinement for nuance
        try:
            prompt = CONFIDENCE_TEMPLATE.format(
                question=question,
                research_output=research_output[:500] if research_output else "No research data",
                signal_output=signal_output[:500] if signal_output else "No signal data",
                contradiction_output=contradiction_output[:500] if contradiction_output else "No contradictions",
                scenarios_output=scenarios_output[:500] if scenarios_output else "No scenarios",
                thesis_output=thesis_output[:500] if thesis_output else "No thesis",
            )

            response = self.ai.generate(
                prompt,
                use_cache=True,
                system_override=CONFIDENCE_SYSTEM,
            )

            llm_data = self._parse_response(response)
            if llm_data:
                # Blend algorithmic and LLM scores
                overall = (raw_score * 0.4) + (llm_data.get("overall", raw_score) * 0.6)
                scenario_clarity = llm_data.get("scenario_clarity", 0.5)
                assessment = llm_data.get("assessment", self._generate_assessment(overall))
                limitations = llm_data.get("limitations", [])
            else:
                overall = raw_score
                scenario_clarity = 0.5
                assessment = self._generate_assessment(overall)
                limitations = []
        except Exception as e:
            logger.warning(f"LLM confidence refinement failed: {e}")
            overall = raw_score
            scenario_clarity = 0.5
            assessment = self._generate_assessment(overall)
            limitations = []

        overall = max(0.0, min(1.0, overall))
        level = (
            "high" if overall > 0.75
            else "moderate" if overall > 0.50
            else "low" if overall > 0.25
            else "very_low"
        )

        return ConfidenceScore(
            overall=overall,
            level=level,
            evidence_quality=evidence_quality,
            signal_alignment=signal_alignment,
            contradiction_impact=contradiction_impact,
            scenario_clarity=scenario_clarity,
            assessment=assessment,
            limitations=limitations,
        )

    def _score_evidence_quality(self, has_data: bool, research_output: str) -> float:
        """Score the quality of available research data."""
        if not has_data or not research_output:
            return 0.2

        # More data = higher confidence (up to 1.0)
        length_score = min(1.0, len(research_output) / 1000)
        return max(0.2, length_score)

    def _score_signal_alignment(self, signal: Optional[AggregatedSignal]) -> float:
        """Score how well technical signals align."""
        if not signal:
            return 0.5  # Neutral if no signals

        # Higher confidence in signal = better alignment
        return signal.confidence

    def _score_contradictions(self, contradictions: Optional[List[Contradiction]]) -> float:
        """Calculate penalty from contradictions (0.0 = no penalty, 1.0 = max penalty)."""
        if not contradictions:
            return 0.0

        total_penalty = 0.0
        for c in contradictions:
            if c.severity == "high":
                total_penalty += 0.3
            elif c.severity == "medium":
                total_penalty += 0.15
            else:
                total_penalty += 0.05

        return min(1.0, total_penalty)

    def _generate_assessment(self, score: float) -> str:
        """Generate a textual assessment of the confidence score."""
        if score > 0.75:
            return "Keyakinan tinggi: Data dan sinyal saling mendukung dengan baik"
        elif score > 0.50:
            return "Keyakinan moderat: Ada dukungan data tetapi tetap perlu kewaspadaan"
        elif score > 0.25:
            return "Keyakinan rendah: Data terbatas atau ada sinyal yang bertentangan"
        else:
            return "Keyakinan sangat rendah: Data tidak mencukupi untuk analisis yang berarti"

    def _parse_response(self, response: str) -> Optional[Dict]:
        """Parse LLM response into confidence dict."""
        try:
            text = clean_json_response(response)
            data = json.loads(text)
            return {
                "overall": float(data.get("overall_score", 0.5)),
                "level": data.get("level", "moderate"),
                "evidence_quality": float(data.get("evidence_quality", 0.5)),
                "signal_alignment": float(data.get("signal_alignment", 0.5)),
                "scenario_clarity": float(data.get("scenario_clarity", 0.5)),
                "assessment": data.get("assessment", ""),
                "limitations": data.get("limitations", []),
            }
        except (json.JSONDecodeError, IndexError, ValueError) as e:
            logger.warning(f"Failed to parse confidence response: {e}")
            return None
