"""
Risk Gates — Educational risk assessment for market conditions.
Adapted from MarketLens BTC's RiskGates, but adapted for educational analysis
instead of trade execution.

Identifies:
1. VOLATILITY RISK — Unusual price swings or low liquidity
2. EVENT RISK — Upcoming economic data or events
3. CORRELATION RISK — Cross-asset dislocations
4. TECHNICAL RISK — Broken patterns or failed levels
5. SENTIMENT RISK — Extreme positioning

⚠️ This is EDUCATIONAL analysis, not trading advice.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from analysis.prompts import RISK_SYSTEM, RISK_TEMPLATE
from data.cache import parse_json_payload

logger = logging.getLogger(__name__)


@dataclass
class RiskFactor:
    """A risk factor identified in market analysis."""
    risk: str
    severity: str  # high, medium, low
    explanation: str = ""
    what_to_watch: str = ""

    def to_dict(self) -> Dict:
        return {
            "risk": self.risk,
            "severity": self.severity,
            "explanation": self.explanation,
            "what_to_watch": self.what_to_watch,
        }


@dataclass
class RiskAssessment:
    """Complete risk assessment for educational analysis."""
    overall_level: str  # low, moderate, high, extreme
    risk_factors: List[RiskFactor] = field(default_factory=list)
    catalyst_calendar: List[Dict] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> Dict:
        return {
            "overall_level": self.overall_level,
            "risk_count": len(self.risk_factors),
            "high_severity_count": sum(1 for r in self.risk_factors if r.severity == "high"),
            "summary": self.summary,
        }


class RiskGates:
    """
    Educational risk assessment for market analysis.

    Unlike MarketLens's RiskGates which blocks trades, this version
    EDUCATES users about market risks so they can make informed decisions.
    """

    def __init__(self, ai_engine: Any):
        self.ai = ai_engine

    async def assess(
        self,
        market_data: str,
        thesis_output: str,
        contradiction_output: str,
        scenarios_output: str,
    ) -> RiskAssessment:
        """
        Assess market risks for educational purposes.

        Args:
            market_data: Market context data
            thesis_output: Thesis summary with direction
            contradiction_output: Contradiction analysis
            scenarios_output: Generated scenarios

        Returns:
            RiskAssessment with identified risks and severity
        """
        logger.info("Assessing market risks...")

        # Basic algorithmic assessment based on available data
        algorithmic_level = self._algorithmic_assessment(
            market_data, contradiction_output
        )

        # LLM-enhanced assessment for nuanced risk identification
        try:
            prompt = RISK_TEMPLATE.format(
                market_data=market_data[:1500] if market_data else "No market data",
                thesis_output=thesis_output[:1000] if thesis_output else "No thesis",
                contradiction_output=contradiction_output[:800] if contradiction_output else "No contradictions",
                scenarios_output=scenarios_output[:800] if scenarios_output else "No scenarios",
            )

            # generate() sinkron (requests) → jalankan di thread agar tidak
            # memblokir event loop dan bisa paralel dengan agent lain.
            response = await asyncio.to_thread(
                self.ai.generate,
                prompt,
                use_cache=True,
                system_override=RISK_SYSTEM,
                # Output JSON faktor risiko + summary — 800 token cukup;
                # default 4096 over-provision untuk output terstruktur.
                max_tokens=800,
            )

            llm_assessment = self._parse_response(response)

            if llm_assessment:
                # Safely parse risk factors without breaking on unexpected extra fields from LLM
                risk_factors = []
                for rf in llm_assessment.get("risk_factors", []):
                    if isinstance(rf, dict):
                        risk_factors.append(RiskFactor(
                            risk=rf.get("risk", rf.get("name", "Unknown Risk")),
                            severity=rf.get("severity", "low"),
                            explanation=rf.get("explanation", rf.get("description", "")),
                            what_to_watch=rf.get("what_to_watch", ""),
                        ))

                # Blend algorithmic and LLM assessments
                return RiskAssessment(
                    overall_level=llm_assessment.get("overall_risk_level", algorithmic_level),
                    risk_factors=risk_factors,
                    catalyst_calendar=llm_assessment.get("catalyst_calendar", []),
                    summary=llm_assessment.get("summary", ""),
                )
        except Exception as e:
            logger.warning(f"LLM risk assessment failed: {e}")

        return RiskAssessment(
            overall_level=algorithmic_level,
            summary=self._generate_summary(algorithmic_level),
        )

    def _algorithmic_assessment(self, market_data: str, contradiction_output: str) -> str:
        """Quick algorithmic risk assessment from data."""
        risk_score = 0

        # Higher risk if there are market data mentions of volatility
        volatility_keywords = ["volatil", "tinggi", "lonjak", "turun drastis", "crash", "panic"]
        if market_data:
            risk_score += sum(2 for kw in volatility_keywords if kw in market_data.lower())

        # Higher risk if contradictions exist
        if contradiction_output and "high" in contradiction_output.lower():
            risk_score += 3
        if contradiction_output and "medium" in contradiction_output.lower():
            risk_score += 1

        if risk_score >= 5:
            return "high"
        elif risk_score >= 3:
            return "moderate"
        elif risk_score >= 1:
            return "low"
        return "low"

    def _generate_summary(self, level: str) -> str:
        """Generate a summary text for the risk level."""
        summaries = {
            "extreme": "⚠️⚠️ Risiko sangat tinggi. Kondisi pasar tidak normal. Harap sangat berhati-hati.",
            "high": "⚠️ Risiko tinggi. Ada beberapa faktor risiko signifikan yang perlu diwaspadai.",
            "moderate": "📊 Risiko moderat. Kondisi pasar normal dengan beberapa faktor perlu diperhatikan.",
            "low": "✅ Risiko rendah. Kondisi pasar relatif stabil.",
        }
        return summaries.get(level, "📊 Risiko tidak dapat ditentukan.")

    def _parse_response(self, response: str) -> Optional[Dict]:
        """Parse LLM response into risk assessment dict."""
        # json.loads yang TIDAK pernah raise; payload list/non-dict → None
        # (pemanggil memakai penilaian algoritmik — pipeline tidak crash).
        data = parse_json_payload(response)
        if not isinstance(data, dict):
            return None
        # `or default` — LLM boleh mengembalikan null eksplisit (sesuai
        # aturan anti-halusinasi "isi null jika tidak ada").
        return {
            "overall_risk_level": data.get("overall_risk_level") or "moderate",
            "risk_factors": data.get("risk_factors") or [],
            "catalyst_calendar": data.get("catalyst_calendar") or [],
            "summary": data.get("summary") or "",
        }
