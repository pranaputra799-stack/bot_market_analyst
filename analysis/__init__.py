"""
Market Analysis Multi-Agent System
===================================
Adapted from MarketLens BTC's research pipeline architecture.

Agent Pipeline:
  1. Director — Orchestrates which agents to call based on user intent
  2. Research — Gathers market context and relevant data
  3. Signals — Aggregates technical signals with confidence scoring
  4. Thesis — Forms structured analysis with directional bias
  5. Contradiction — Detects conflicting signals and risks
  6. Scenarios — Generates possible market scenarios
  7. Confidence — Scores overall confidence of the analysis
  8. Risk Gates — Educational risk assessment for market conditions

Usage:
    from analysis.director import AnalysisDirector
    director = AnalysisDirector(ai_engine, market_data, macro_data, news_fetcher)
    result = await director.analyze(question, user_context)
"""

from analysis.signals import SignalEngine, Signal, SignalType
from analysis.director import AnalysisDirector, AnalysisResult
from analysis.intent_classifier import IntentClassifier, IntentResult, get_classifier

__all__ = [
    "SignalEngine",
    "Signal",
    "SignalType",
    "AnalysisDirector",
    "AnalysisResult",
    "IntentClassifier",
    "IntentResult",
    "get_classifier",
]
