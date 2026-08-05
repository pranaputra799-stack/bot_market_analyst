"""
Monitoring — Metrics tracking for the multi-agent analysis system.
Adapted from MarketLens BTC's monitoring module.

Tracks:
- Analysis counts by type
- Agent execution times
- Error rates
- Cache hit ratios
"""

import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AnalysisMetrics:
    """Metrics for a single analysis run."""
    question_type: str
    agents_executed: List[str]
    start_time: float
    end_time: Optional[float] = None
    duration_ms: Optional[float] = None
    confidence_score: Optional[float] = None
    cache_hits: int = 0
    error: Optional[str] = None


class MetricsTracker:
    """
    Tracks and reports metrics for the multi-agent analysis system.

    Provides:
    - Per-analysis timing
    - Agent usage statistics
    - Error tracking
    - Performance reports
    """

    def __init__(self):
        self._analyses: List[AnalysisMetrics] = []
        self._agent_times: Dict[str, List[float]] = {}
        self._question_types: Dict[str, int] = {}
        self._total_cache_hits = 0
        self._total_errors = 0
        self._start_time = time.time()

    def start_analysis(self, question_type: str, agents: List[str]) -> AnalysisMetrics:
        """Start tracking a new analysis run."""
        metrics = AnalysisMetrics(
            question_type=question_type,
            agents_executed=agents,
            start_time=time.time(),
        )
        self._analyses.append(metrics)
        self._question_types[question_type] = self._question_types.get(question_type, 0) + 1
        return metrics

    def complete_analysis(
        self,
        metrics: AnalysisMetrics,
        confidence_score: Optional[float] = None,
        error: Optional[str] = None,
    ):
        """Mark an analysis as complete."""
        metrics.end_time = time.time()
        metrics.duration_ms = (metrics.end_time - metrics.start_time) * 1000
        metrics.confidence_score = confidence_score
        metrics.error = error

        if error:
            self._total_errors += 1

    def record_agent_time(self, agent_name: str, duration_ms: float):
        """Record execution time for an agent."""
        if agent_name not in self._agent_times:
            self._agent_times[agent_name] = []
        self._agent_times[agent_name].append(duration_ms)

    def record_cache_hit(self, count: int = 1):
        """Record a cache hit."""
        self._total_cache_hits += count
        if self._analyses:
            self._analyses[-1].cache_hits += count

    def get_report(self) -> Dict:
        """Generate a comprehensive metrics report."""
        total_analyses = len(self._analyses)
        completed = [a for a in self._analyses if a.end_time is not None]
        failed = [a for a in completed if a.error is not None]

        avg_duration = (
            sum(a.duration_ms for a in completed if a.duration_ms) / len(completed)
            if completed
            else 0
        )

        # Agent performance
        agent_stats = {}
        for agent_name, times in self._agent_times.items():
            agent_stats[agent_name] = {
                "total_calls": len(times),
                "avg_duration_ms": sum(times) / len(times) if times else 0,
                "max_duration_ms": max(times) if times else 0,
                "min_duration_ms": min(times) if times else 0,
            }

        uptime_seconds = time.time() - self._start_time
        uptime_str = f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m"

        return {
            "uptime": uptime_str,
            "total_analyses": total_analyses,
            "completed": len(completed),
            "failed": len(failed),
            "error_rate": f"{len(failed) / total_analyses * 100:.1f}%" if total_analyses > 0 else "0%",
            "avg_duration_ms": round(avg_duration, 1),
            "total_cache_hits": self._total_cache_hits,
            "question_types": dict(self._question_types),
            "agent_performance": agent_stats,
        }

    def get_question_type_distribution(self) -> Dict[str, int]:
        """Get distribution of question types analyzed."""
        return dict(self._question_types)

    def summary_for_status(self) -> str:
        """Get a formatted summary for /status command."""
        report = self.get_report()
        lines = [
            f"📊 *ANALYSIS ENGINE*",
            f"• Total analisis: {report['total_analyses']}",
            f"• Rata-rata durasi: {report['avg_duration_ms']:.0f}ms",
            f"• Cache hits: {report['total_cache_hits']}",
            f"• Error rate: {report['error_rate']}",
            "",
            "*Tipe Pertanyaan:*",
        ]

        for qtype, count in report.get("question_types", {}).items():
            lines.append(f"  • {qtype}: {count}x")

        if report.get("agent_performance"):
            lines.append("")
            lines.append("*Kinerja Agent:*")
            for agent, stats in report["agent_performance"].items():
                lines.append(f"  • {agent}: {stats['total_calls']}x ({stats['avg_duration_ms']:.0f}ms avg)")

        return "\n".join(lines)


# Global metrics tracker instance
metrics = MetricsTracker()
