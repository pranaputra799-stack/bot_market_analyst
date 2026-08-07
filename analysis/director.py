"""
Director Agent — Orchestrates the multi-agent analysis pipeline.
Adapted from MarketLens BTC's DirectorAgent and AutoHedge's handoff patterns.

The Director:
1. Analyzes user intent to determine which agents to invoke
2. Executes agents in dependency order (research → signals → thesis → ...)
3. Synthesizes all outputs into a comprehensive, educational response
4. Tracks metrics and caches results for efficiency

Agent Pipeline:
  research → signals → thesis → contradiction → scenarios → confidence → risk_gates
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from analysis.prompts import (
    DIRECTOR_SYSTEM,
    build_analysis_prompt,
    format_context_for_prompt,
)
from analysis.research_agent import ResearchAgent, ResearchContext
from analysis.thesis_agent import ThesisAgent, Thesis
from analysis.contradiction_agent import ContradictionAgent, Contradiction
from analysis.scenarios_agent import ScenariosAgent, Scenario
from analysis.confidence_agent import ConfidenceAgent, ConfidenceScore
from analysis.risk_gates import RiskGates, RiskAssessment
from analysis.signals import SignalEngine, AggregatedSignal, SignalType
from analysis.intent_classifier import IntentClassifier, IntentResult
from analysis.indicators import compute_indicators, format_indicators_for_prompt
from analysis.fact_check import build_fact_check_note
from analysis.monitoring import metrics
from data.cache import cache, safe_hash

logger = logging.getLogger(__name__)


# Intent yang cukup dengan jalur RINGAN (1-2 panggilan LLM): research + signals +
# synthesis saja. Thesis/contradiction/confidence/risk hanya menambah latensi
# tanpa banyak nilai untuk tipe pertanyaan ini (mis. "berapa harga EUR/USD?").
LIGHT_INTENTS = {"education", "price_check", "news_sentiment", "calendar"}


@dataclass
class AnalysisResult:
    """Complete result from the multi-agent analysis pipeline."""
    question: str
    intent: str = "general"
    research_context: Optional[ResearchContext] = None
    thesis: Optional[Thesis] = None
    signal: Optional[AggregatedSignal] = None
    contradictions: List[Contradiction] = field(default_factory=list)
    scenarios: List[Scenario] = field(default_factory=list)
    confidence: Optional[ConfidenceScore] = None
    risk_assessment: Optional[RiskAssessment] = None
    final_response: str = ""
    agents_executed: List[str] = field(default_factory=list)
    duration_ms: float = 0.0
    error: Optional[str] = None
    conversation_history: str = ""
    indicators_summary: str = ""  # teks indikator teknikal untuk prompt synthesis
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class AnalysisDirector:
    """
    Orchestrates the multi-agent analysis pipeline.

    Usage:
        director = AnalysisDirector(ai_engine, market_data, macro_data, news_fetcher)
        result = await director.analyze("Kenapa gold naik hari ini?")

    The Director automatically:
    - Detects question intent using intelligent classifier
    - Selects relevant agents based on intent
    - Executes them in optimal order
    - Caches intermediate results
    - Synthesizes final response
    """

    def __init__(
        self,
        ai_engine: Any,
        market_data: Any,
        macro_data: Any,
        news_fetcher: Any,
        enable_llm_planning: bool = True,
        enable_cache: bool = True,
    ):
        self.ai = ai_engine
        self.enable_llm_planning = enable_llm_planning
        self.enable_cache = enable_cache

        # Initialize intent classifier (with LLM fallback)
        self.intent_classifier = IntentClassifier(ai_engine)

        # Initialize agents
        self.research = ResearchAgent(ai_engine, market_data, macro_data, news_fetcher)
        self.signals = SignalEngine()
        self.thesis = ThesisAgent(ai_engine)
        self.contradiction = ContradictionAgent(ai_engine)
        self.scenarios = ScenariosAgent(ai_engine)
        self.confidence = ConfidenceAgent(ai_engine)
        self.risk = RiskGates(ai_engine)

    async def analyze(
        self,
        question: str,
        market_data_ohlcv: Optional[List[Dict]] = None,
        technical_indicators: Optional[Dict] = None,
        conversation_history: str = "",
    ) -> AnalysisResult:
        """
        Run the full multi-agent analysis pipeline.

        Args:
            question: User's question to analyze
            market_data_ohlcv: Optional OHLCV data for signal analysis
            technical_indicators: Optional technical indicators
            conversation_history: Riwayat percakapan user (format_history) untuk
                konteks follow-up — disuntikkan ke research & synthesis prompt

        Returns:
            AnalysisResult with all agent outputs and final response
        """
        start_time = time.time()

        # ===== INTENT CLASSIFICATION =====
        intent_result = await self.intent_classifier.classify(question)
        intent = intent_result.intent
        logger.info(f"Director: Detected intent={intent} (conf={intent_result.confidence:.0%}, entities={intent_result.detected_entities})")

        # Initialize result
        result = AnalysisResult(
            question=question,
            intent=intent,
            conversation_history=conversation_history,
        )
        metrics_handle = metrics.start_analysis(intent, [])

        try:
            # Check cache for identical question
            # Cache key menyertakan history agar jawaban yang dikontekstualisasi
            # percakapan user A tidak tersaji ke user lain dengan pertanyaan sama.
            if self.enable_cache:
                cached_result = self._check_cache(question, conversation_history)
                if cached_result:
                    metrics.record_cache_hit()
                    metrics.complete_analysis(metrics_handle)
                    logger.info(f"Cache hit for question: {question[:60]}...")
                    return cached_result

            # ===== STAGE 1: Research (intent-aware) =====
            logger.info(f"Director: Running Research Agent (intent={intent})...")
            result.agents_executed.append("research")
            research_start = time.time()
            result.research_context = await self.research.gather(
                question,
                intent_result=intent_result,  # Pass intent for smarter data gathering
                conversation_history=conversation_history,  # konteks follow-up
            )
            metrics.record_agent_time("research", (time.time() - research_start) * 1000)

            # ===== STAGE 2: Signals (only if market data is relevant) =====
            if intent_result.needs_market_data or market_data_ohlcv:
                logger.info("Director: Running Signal Engine...")
                result.agents_executed.append("signals")
                signal_start = time.time()

                if market_data_ohlcv:
                    # Hitung indikator teknikal LOKAL dari OHLCV (RSI, MACD,
                    # Bollinger, pivot, fib, dll) agar signal engine memakai
                    # angka nyata — bukan None seperti sebelumnya.
                    if not technical_indicators:
                        technical_indicators = compute_indicators(market_data_ohlcv)
                        result.indicators_summary = format_indicators_for_prompt(
                            technical_indicators
                        )
                    result.signal = self.signals.evaluate_from_data(
                        market_data_ohlcv, technical_indicators
                    )
                elif result.research_context and result.research_context.raw_context:
                    result.signal = self.signals.evaluate_from_context(
                        result.research_context.raw_context
                    )
                metrics.record_agent_time("signals", (time.time() - signal_start) * 1000)

            # ===== STAGE 3: Thesis (skip untuk light intents) =====
            if intent not in LIGHT_INTENTS:
                logger.info("Director: Running Thesis Agent...")
                result.agents_executed.append("thesis")
                thesis_start = time.time()
                result.thesis = await self.thesis.formulate(
                    question=question,
                    research_context=result.research_context,
                    signal=result.signal,
                    intent=intent,  # Pass intent for better thesis
                )
                metrics.record_agent_time("thesis", (time.time() - thesis_start) * 1000)

            # Prepare common variables
            thesis_output = result.thesis.summary if result.thesis else ""
            signal_output = result.signal.summary if result.signal else ""
            market_str = result.research_context.raw_context if result.research_context else ""

            # ===== STAGE 4: Contradictions (skip untuk light intents) =====
            if intent not in LIGHT_INTENTS:
                logger.info("Director: Running Contradiction Agent...")
                result.agents_executed.append("contradiction")
                contra_start = time.time()

                result.contradictions = await self.contradiction.analyze(
                    question=question,
                    market_data=market_str,
                    thesis_output=thesis_output,
                    signal_output=signal_output,
                )
                metrics.record_agent_time("contradiction", (time.time() - contra_start) * 1000)

            contra_output = "\n".join(
                f"[{c.severity}] {c.description}" for c in result.contradictions[:3]
            ) if result.contradictions else "No contradictions"

            # ===== STAGE 5: Scenarios (skip for simple intents) =====
            if intent in ("prediction", "technical", "market_analysis", "comparison", "risk"):
                logger.info("Director: Running Scenarios Agent...")
                result.agents_executed.append("scenarios")
                scenario_start = time.time()

                result.scenarios = await self.scenarios.generate(
                    question=question,
                    market_data=market_str,
                    thesis_output=thesis_output,
                    contradiction_output=contra_output,
                )
                metrics.record_agent_time("scenarios", (time.time() - scenario_start) * 1000)

            scenarios_output = "\n".join(
                f"{s.name}: {s.probability}%" for s in result.scenarios
            ) if result.scenarios else "No scenarios"

            # ===== STAGE 6+7: Confidence & Risk (jalan PARALEL) =====
            # Keduanya bergantung pada kontradiksi & skenario yang sudah selesai,
            # sehingga bisa dieksekusi bersamaan. LLM call di dalam tiap agent
            # sudah di-thread (asyncio.to_thread), jadi gather benar-benar paralel.
            conf_task = None
            risk_task = None
            if (result.thesis or result.signal) and intent not in LIGHT_INTENTS:
                conf_task = self.confidence.calibrate(
                    question=question,
                    signal=result.signal,
                    contradictions=result.contradictions,
                    has_research_data=bool(result.research_context and result.research_context.raw_context),
                    research_output=market_str,
                    signal_output=signal_output,
                    contradiction_output=contra_output,
                    scenarios_output=scenarios_output,
                    thesis_output=thesis_output,
                )
            if intent not in LIGHT_INTENTS:
                risk_task = self.risk.assess(
                    market_data=market_str,
                    thesis_output=thesis_output,
                    contradiction_output=contra_output,
                    scenarios_output=scenarios_output,
                )

            parallel = [
                (name, task)
                for name, task in (("confidence", conf_task), ("risk_gates", risk_task))
                if task is not None
            ]
            if parallel:
                logger.info(f"Director: Running {[n for n, _ in parallel]} in parallel...")
                result.agents_executed.extend(n for n, _ in parallel)
                stage_start = time.time()
                outcomes = await asyncio.gather(
                    *(task for _, task in parallel),
                    return_exceptions=True,
                )
                stage_ms = (time.time() - stage_start) * 1000
                for (name, _), outcome in zip(parallel, outcomes):
                    if isinstance(outcome, Exception):
                        logger.warning(f"Director: {name} agent failed: {outcome}")
                        continue
                    if name == "confidence":
                        result.confidence = outcome
                    else:
                        result.risk_assessment = outcome
                    metrics.record_agent_time(name, stage_ms)

            # ===== FINAL SYNTHESIS (intent-aware) =====
            logger.info(f"Director: Synthesizing final response for intent={intent}...")
            result.final_response = await self._synthesize_response(result)
            result.duration_ms = (time.time() - start_time) * 1000

            # ===== FACT CHECK: verifikasi angka jawaban vs data terhitung =====
            # Anti-halusinasi lapis terakhir (deterministik): angka harga/level di
            # jawaban dicek terhadap indikator lokal + data pasar + pertanyaan &
            # riwayat user. Angka yang tidak cocok ditambahkan catatan peringatan.
            self._apply_fact_check(result)

            # Cache the result
            if self.enable_cache:
                self._cache_result(question, result)

            metrics_handle.agents_executed = result.agents_executed
            metrics.complete_analysis(
                metrics_handle,
                confidence_score=result.confidence.overall if result.confidence else None,
            )

            logger.info(
                f"Director: Analysis complete in {result.duration_ms:.0f}ms "
                f"({len(result.agents_executed)} agents, intent={intent})"
            )

        except Exception as e:
            logger.error(f"Director: Analysis failed: {e}", exc_info=True)
            result.error = str(e)
            result.duration_ms = (time.time() - start_time) * 1000
            result.final_response = self._generate_fallback_response(question, e)
            metrics.complete_analysis(metrics_handle, error=str(e))

        return result

    async def _synthesize_response(self, result: AnalysisResult) -> str:
        """
        Synthesize all agent outputs into a cohesive response.

        Uses LLM to combine outputs if available, otherwise uses
        template-based synthesis.
        """
        # Extract outputs
        research_str = ""
        if result.research_context:
            research_str = result.research_context.raw_context

        signal_str = result.signal.summary if result.signal else ""
        thesis_str = result.thesis.summary if result.thesis else ""
        thesis_direction = result.thesis.direction if result.thesis else "neutral"
        thesis_conf = f"{result.thesis.confidence:.0%}" if result.thesis else "N/A"

        contra_str = "\n".join(
            f"• [{c.severity.upper()}] {c.description}" for c in result.contradictions[:5]
        ) if result.contradictions else "Tidak ada kontradiksi signifikan"

        scenario_str = ""
        if result.scenarios:
            for s in result.scenarios:
                scenario_str += f"\n• {s.name}: {s.probability}% — {s.description[:150]}"

        conf_str = ""
        if result.confidence:
            conf_str = f"Level: {result.confidence.level.upper()} ({result.confidence.overall:.0%})"
            if result.confidence.assessment:
                conf_str += f"\n{result.confidence.assessment}"

        risk_str = ""
        if result.risk_assessment:
            risk_str = f"Level: {result.risk_assessment.overall_level.upper()}"
            if result.risk_assessment.risk_factors:
                for rf in result.risk_assessment.risk_factors[:3]:
                    risk_str += f"\n• [{rf.severity.upper()}] {rf.risk}: {rf.explanation[:100]}"
            if result.risk_assessment.summary:
                risk_str += f"\n{result.risk_assessment.summary}"

        # Try LLM synthesis
        try:
            synthesis_prompt = build_analysis_prompt(
                question=result.question,
                context_data=format_context_for_prompt(research_str),
                research_output=research_str[:800] if research_str else "No research data",
                signal_output=signal_str[:500] if signal_str else "No signal data",
                indicators_output=result.indicators_summary,
                thesis_output=thesis_str[:500] if thesis_str else "No thesis",
                contradiction_output=contra_str[:500],
                scenarios_output=scenario_str[:500] if scenario_str else "No scenarios",
                confidence_output=conf_str,
                risk_output=risk_str,
                conversation_history=result.conversation_history,
            )

            response = await asyncio.to_thread(self.ai.generate, synthesis_prompt, use_cache=False)
            if response and len(response) > 50:
                return response
        except Exception as e:
            logger.warning(f"LLM synthesis failed: {e}")

        # Fallback: Template-based synthesis
        return self._template_synthesis(
            result.question, thesis_str, thesis_direction, thesis_conf,
            scenario_str, contra_str, conf_str, risk_str,
        )

    def _template_synthesis(
        self, question: str, thesis_str: str, direction: str, confidence: str,
        scenario_str: str, contra_str: str, conf_str: str, risk_str: str,
    ) -> str:
        """Generate response from template when LLM synthesis unavailable."""
        emoji_map = {
            "bullish": "🟢",
            "bearish": "🔴",
            "neutral": "⚪",
        }
        arrow = emoji_map.get(direction, "⚪")

        parts = [
            f"📊 *Analisis Multi-Agent*\n",
            f"*Pertanyaan:* {question}\n",
            f"{arrow} Bias: {direction.upper()} (keyakinan: {confidence})\n",
        ]

        if thesis_str:
            parts.append(f"\n💡 *Tesis:*\n{thesis_str[:300]}")

        if scenario_str:
            parts.append(f"\n🔮 *Skenario:*{scenario_str}")

        if contra_str and "tidak ada kontradiksi" not in contra_str.lower():
            parts.append(f"\n⚠️ *Kontradiksi Terdeteksi:*\n{contra_str[:300]}")

        if conf_str:
            parts.append(f"\n📈 *Keyakinan Analisis:*\n{conf_str[:200]}")

        if risk_str:
            parts.append(f"\n🛡️ *Risiko:*\n{risk_str[:300]}")

        parts.append(
            "\n\n---\n"
            "⚠️ *Disclaimer:* Analisis ini bersifat edukasi berdasarkan data historis "
            "dan sentimen pasar. Bukan saran investasi atau trading. "
            "Keputusan trading sepenuhnya tanggung jawab Anda."
        )

        return "\n".join(parts)

    def _generate_fallback_response(self, question: str, error: Exception) -> str:
        """Generate a fallback response when analysis fails."""
        return (
            f"🤖 *Market Analysis*\n\n"
            f"Maaf, terjadi kendala saat menganalisis pertanyaan:\n"
            f"*{question}*\n\n"
            f"❌ {str(error)[:150]}\n\n"
            f"Silakan coba lagi dengan pertanyaan yang lebih spesifik, "
            f"atau gunakan /status untuk memeriksa kondisi sistem.\n\n"
            f"---\n"
            f"⚠️ *Disclaimer:* Analisis edukasi. Bukan saran trading."
        )

    def _apply_fact_check(self, result: AnalysisResult):
        """
        Verifikasi deterministik angka di jawaban akhir terhadap data terhitung.

        Membandingkan angka mirip harga/level pada `final_response` dengan angka
        yang benar-benar ada di indikator teknikal lokal + data pasar + pertanyaan
        & riwayat user. Bila ada yang tidak cocok, catatan peringatan ditambahkan
        ke akhir jawaban (aman — tidak pernah raise).
        """
        if not result.final_response:
            return
        data_texts = [result.indicators_summary]
        if result.research_context and result.research_context.raw_context:
            data_texts.append(result.research_context.raw_context)
        # Angka di pertanyaan & jawaban sebelumnya bukan halusinasi — sertakan
        # sebagai data pembanding agar follow-up yang mengulang level tetap lolos.
        if result.question:
            data_texts.append(result.question)
        if result.conversation_history:
            data_texts.append(result.conversation_history)
        try:
            note = build_fact_check_note(result.final_response, data_texts)
            if note:
                result.final_response += note
        except Exception as e:
            logger.debug(f"Fact check skipped: {e}")

    def _check_cache(self, question: str, conversation_history: str = "") -> Optional[AnalysisResult]:
        """Check if we have a cached result for similar question."""
        cache_key = f"analysis:{safe_hash(question + conversation_history[:200])}"
        cached = cache.get(cache_key)
        if cached:
            try:
                data = json.loads(cached) if isinstance(cached, str) else cached
                if isinstance(data, dict) and "final_response" in data:
                    result = AnalysisResult(
                        question=question,
                        intent=data.get("intent", "general"),
                        final_response=data["final_response"],
                        agents_executed=data.get("agents_executed", []),
                        duration_ms=0,
                    )
                    return result
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    def _cache_result(self, question: str, result: AnalysisResult):
        """Cache the analysis result."""
        cache_key = f"analysis:{safe_hash(question + result.conversation_history[:200])}"
        try:
            cache.set(cache_key, {
                "intent": result.intent,
                "final_response": result.final_response,
                "agents_executed": result.agents_executed,
                "confidence": result.confidence.to_dict() if result.confidence else None,
            }, ttl=600)
        except Exception as e:
            logger.debug(f"Failed to cache analysis: {e}")
