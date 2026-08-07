"""
Research Agent — Gathers market intelligence from multiple data sources.
Adapted from MarketLens BTC's ResearchAgent.

The agent collects:
- Current market prices and movements
- Macroeconomic data
- News and sentiment
- Economic calendar events

Then uses LLM to analyze and structure this data into actionable context.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from analysis.prompts import (
    RESEARCH_SYSTEM,
    RESEARCH_ANALYSIS_TEMPLATE,
    format_context_for_prompt,
)
from data.cache import cache, safe_hash, clean_json_response

logger = logging.getLogger(__name__)


@dataclass
class ResearchContext:
    """Structured research context for analysis."""
    market_summary: str = ""
    macro_summary: str = ""
    news_summary: str = ""
    calendar_summary: str = ""
    key_drivers: List[str] = field(default_factory=list)
    key_levels: Dict[str, List[str]] = field(default_factory=lambda: {"support": [], "resistance": []})
    market_regime: str = "unknown"  # trending, ranging, volatile
    llm_analysis: str = ""
    raw_context: str = ""

    def to_dict(self) -> Dict:
        return {
            "market_regime": self.market_regime,
            "key_drivers": self.key_drivers,
            "key_levels": self.key_levels,
            "news_sentiment": self.news_summary[:200] if self.news_summary else "",
        }

    def to_prompt_context(self) -> str:
        """Format all context data for LLM prompt."""
        parts = []
        if self.market_summary:
            parts.append(f"📊 MARKET DATA:\n{self.market_summary}")
        if self.macro_summary:
            parts.append(f"🏛️ MACRO DATA:\n{self.macro_summary}")
        if self.news_summary:
            parts.append(f"📰 NEWS:\n{self.news_summary}")
        if self.calendar_summary:
            parts.append(f"📅 CALENDAR:\n{self.calendar_summary}")
        if self.llm_analysis:
            parts.append(f"🤖 AI ANALYSIS:\n{self.llm_analysis}")
        return "\n\n".join(parts) if parts else ""


class ResearchAgent:
    """
    Gathers and analyzes market context from multiple data sources.

    Uses the data layer (market_data, macro_data, news_data) and optionally
    enhances with LLM analysis for richer context.
    """

    def __init__(
        self,
        ai_engine: Any,
        market_data: Any,
        macro_data: Any,
        news_fetcher: Any,
    ):
        self.ai = ai_engine
        self.market = market_data
        self.macro = macro_data
        self.news = news_fetcher

    async def gather(
        self,
        question: str,
        intent_result: Optional[Any] = None,
        conversation_history: str = "",
    ) -> ResearchContext:
        """
        Gather market context relevant to the user's question.
        Uses intent classification for smarter data fetching.

        Args:
            question: User's question to determine what data is needed
            intent_result: Optional IntentResult for smarter data detection
            conversation_history: Riwayat percakapan user (untuk follow-up)

        Returns:
            ResearchContext with structured market data and analysis
        """
        logger.info(f"Research agent gathering context for: {question[:80]}...")

        context = ResearchContext()

        # Use intent result if available, otherwise fallback to keyword detection
        if intent_result:
            needs_market = intent_result.needs_market_data
            needs_macro = intent_result.needs_macro_data
            needs_news = intent_result.needs_news_data
            needs_calendar = intent_result.needs_calendar
        else:
            needs_market = self._detect_market_data_needed(question)
            needs_macro = self._detect_macro_needed(question)
            needs_news = self._detect_news_needed(question)
            needs_calendar = self._detect_calendar_needed(question)

        # Gather data in parallel
        fetch_tasks = []

        if needs_market:
            fetch_tasks.append(self._fetch_market_summary())

        if needs_macro:
            fetch_tasks.append(self._fetch_macro_summary())

        if needs_news:
            fetch_tasks.append(self._fetch_news_summary())

        if needs_calendar:
            fetch_tasks.append(self._fetch_calendar_summary())

        if fetch_tasks:
            results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            idx = 0
            if needs_market and idx < len(results):
                context.market_summary = results[idx] if not isinstance(results[idx], Exception) else ""
                idx += 1
            if needs_macro and idx < len(results):
                context.macro_summary = results[idx] if not isinstance(results[idx], Exception) else ""
                idx += 1
            if needs_news and idx < len(results):
                context.news_summary = results[idx] if not isinstance(results[idx], Exception) else ""
                idx += 1
            if needs_calendar and idx < len(results):
                context.calendar_summary = results[idx] if not isinstance(results[idx], Exception) else ""
                idx += 1

        # Build raw context for LLM
        context.raw_context = context.to_prompt_context()

        # Use LLM to analyze context and extract structure
        if context.raw_context:
            try:
                await self._llm_analyze_context(context, question, conversation_history)
            except Exception as e:
                logger.warning(f"LLM context analysis failed: {e}")

        return context

    async def _fetch_market_summary(self) -> str:
        """Fetch market summary synchronously (Yahoo Finance is sync)."""
        return await asyncio.to_thread(self.market.get_market_summary)

    async def _fetch_macro_summary(self) -> str:
        """Fetch macro summary synchronously."""
        return await asyncio.to_thread(self.macro.get_macro_summary)

    async def _fetch_news_summary(self) -> str:
        """Fetch news summary."""
        try:
            result = await self.news.get_news_summary("FOREX")
            if isinstance(result, str):
                return result
            return str(result.get("summary", "")) if isinstance(result, dict) else ""
        except Exception as e:
            logger.warning(f"News fetch failed: {e}")
            return ""

    async def _fetch_calendar_summary(self) -> str:
        """Fetch economic calendar."""
        try:
            events = await self.macro.get_economic_calendar()
            return self.macro.format_calendar_text(events, max_events=5)
        except Exception as e:
            logger.warning(f"Calendar fetch failed: {e}")
            return ""

    async def _llm_analyze_context(
        self,
        context: ResearchContext,
        question: str,
        conversation_history: str = "",
    ):
        """Use LLM to extract structured insights from raw context data."""
        # Sertakan history dalam cache key agar analisis follow-up tidak
        # mengambil cache yang dibuat tanpa konteks percakapan.
        cache_key = (
            f"research_analysis:{safe_hash(question + context.raw_context[:500] + conversation_history[:200])}"
        )
        cached = cache.get(cache_key)
        if cached:
            try:
                data = json.loads(cached) if isinstance(cached, str) else cached
                # `or default` — LLM boleh mengembalikan null eksplisit (sesuai
                # aturan anti-halusinasi "isi null jika tidak ada"); jangan biarkan
                # null mengalir ke bawah dan merusak konsumen field ini.
                context.key_drivers = data.get("key_drivers") or []
                context.key_levels = data.get("key_levels") or {"support": [], "resistance": []}
                context.market_regime = data.get("market_regime") or "unknown"
                context.llm_analysis = data.get("llm_analysis") or ""
                return
            except (json.JSONDecodeError, TypeError):
                pass

        prompt = RESEARCH_ANALYSIS_TEMPLATE.format(
            question=question,
            context_data=format_context_for_prompt(context.raw_context),
            conversation_history=conversation_history or "Tidak ada percakapan sebelumnya.",
        )

        # generate() sinkron (requests) → jalankan di thread agar tidak
        # memblokir event loop.
        response = await asyncio.to_thread(
            self.ai.generate,
            prompt,
            use_cache=False,
            system_override=RESEARCH_SYSTEM,
        )

        # Try to parse JSON from response
        try:
            # Extract JSON menggunakan clean_json_response untuk menangani variasi format LLM
            text = clean_json_response(response)

            data = json.loads(text)
            # `or default` — LLM boleh mengembalikan null eksplisit (sesuai aturan
            # anti-halusinasi "isi null jika tidak ada"); guard agar tidak None.
            context.key_drivers = data.get("key_drivers") or []
            context.key_levels = data.get("key_levels") or {"support": [], "resistance": []}
            context.market_regime = data.get("market_regime") or "unknown"
            context.llm_analysis = data.get("price_context") or response[:500]
        except (json.JSONDecodeError, IndexError):
            context.llm_analysis = response[:500]

        # Cache the analysis
        try:
            cache.set(
                cache_key,
                {
                    "key_drivers": context.key_drivers,
                    "key_levels": context.key_levels,
                    "market_regime": context.market_regime,
                    "llm_analysis": context.llm_analysis,
                },
                ttl=300,
            )
        except Exception:
            pass

    @staticmethod
    def _detect_market_data_needed(question: str) -> bool:
        """Detect if question needs market price data."""
        keywords = [
            "harga", "price", "eur", "usd", "gbp", "jpy", "gold", "emas",
            "forex", "pasar", "market", "naik", "turun", "chart", "grafik",
            "dxy", "s&p", "index", "saham", "crypto", "bitcoin",
        ]
        q = question.lower()
        return any(kw in q for kw in keywords)

    @staticmethod
    def _detect_macro_needed(question: str) -> bool:
        """Detect if question needs macro data."""
        keywords = [
            "inflasi", "cpi", "nfp", "gdp", "fed", "fomc", "suku bunga",
            "tenaga kerja", "pengangguran", "makro", "ekonomi", "ekonomi",
            "interest rate", "unemployment", "inflation",
        ]
        q = question.lower()
        return any(kw in q for kw in keywords)

    @staticmethod
    def _detect_news_needed(question: str) -> bool:
        """Detect if question needs news/sentiment data."""
        keywords = [
            "berita", "news", "sentimen", "sentiment", "hari ini",
            "headline", "terkini", "update", "baru",
        ]
        q = question.lower()
        return any(kw in q for kw in keywords)

    @staticmethod
    def _detect_calendar_needed(question: str) -> bool:
        """Detect if question needs calendar data."""
        keywords = [
            "kalender", "calendar", "jadwal", "rilis", "event",
            "data ekonomi", "hari ini",
        ]
        q = question.lower()
        return any(kw in q for kw in keywords)
