"""
Unit tests: parsing respons LLM yang ROBUST di semua agent analisis.

Regresi bug: 'list' object has no attribute 'get' — model free kadang
mengembalikan JSON ARRAY padahal diminta OBJECT (mis. `[{...}]` bukan
`{"contradictions": [...]}`). Semua parser harus toleran terhadap kedua
bentuk dan TIDAK pernah melempar exception yang mematikan pipeline.

Logika murni (tanpa network) — AI engine di-stub.
"""

import asyncio
import unittest

from data.cache import parse_json_payload, extract_list_items
from analysis.contradiction_agent import ContradictionAgent
from analysis.scenarios_agent import ScenariosAgent
from analysis.thesis_agent import ThesisAgent
from analysis.confidence_agent import ConfidenceAgent
from analysis.risk_gates import RiskGates
from analysis.research_agent import ResearchAgent, ResearchContext
from analysis.sentiment import SentimentAnalyzer


class _StubAI:
    """AI engine stub — mengembalikan respons yang sudah ditentukan."""

    def __init__(self, response: str):
        self._response = response

    def generate(self, *args, **kwargs):
        return self._response


class TestParseJsonPayload(unittest.TestCase):
    def test_valid_object(self):
        self.assertEqual(parse_json_payload('{"a": 1}'), {"a": 1})

    def test_valid_array(self):
        self.assertEqual(parse_json_payload('[{"a": 1}]'), [{"a": 1}])

    def test_markdown_fenced(self):
        self.assertEqual(
            parse_json_payload('```json\n{"a": 1}\n```'), {"a": 1}
        )

    def test_text_around_json(self):
        self.assertEqual(
            parse_json_payload('Berikut analisis: {"a": 1} Semoga membantu'),
            {"a": 1},
        )

    def test_prose_bracket_prefix_does_not_shadow_object(self):
        """Respons diawali bracket prosa ([update] ...) tapi berisi object valid.
        Array-first extraction dulu mengambil '[update]' (balanced tapi bukan
        JSON) → json.loads gagal → object di belakangnya ikut hilang. Sekarang
        array divalidasi dulu; gagal → jatuh ke object extraction."""
        self.assertEqual(
            parse_json_payload('[update] gold naik: {"a": 1}'),
            {"a": 1},
        )

    def test_valid_array_still_extracted_first(self):
        """Array JSON asli di awal tetap diekstrak utuh (validasi lolos)."""
        self.assertEqual(
            parse_json_payload('[{"a": 1}, {"b": 2}] catatan akhir'),
            [{"a": 1}, {"b": 2}],
        )

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_json_payload("tidak ada json sama sekali"))
        self.assertIsNone(parse_json_payload(""))
        self.assertIsNone(parse_json_payload(None))


class TestExtractListItems(unittest.TestCase):
    def test_dict_with_key(self):
        self.assertEqual(
            extract_list_items({"contradictions": [1, 2]}, "contradictions"), [1, 2]
        )

    def test_list_direct(self):
        self.assertEqual(extract_list_items([1, 2, 3], "contradictions"), [1, 2, 3])

    def test_dict_with_second_key(self):
        self.assertEqual(
            extract_list_items({"x": 1, "results": [9]}, "contradictions", "results"), [9]
        )

    def test_non_list_value_returns_empty(self):
        self.assertEqual(extract_list_items({"contradictions": "not-a-list"}, "contradictions"), [])
        self.assertEqual(extract_list_items(None, "contradictions"), [])
        self.assertEqual(extract_list_items("string", "contradictions"), [])


class TestContradictionAgent(unittest.TestCase):
    def _parse(self, response: str):
        return ContradictionAgent(None)._parse_response(response)

    def test_array_payload_regression(self):
        """Bug asli: array langsung → dulu AttributeError, sekarang diparse."""
        items = self._parse('[{"description": "A vs B", "severity": "high"}]')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].description, "A vs B")
        self.assertEqual(items[0].severity, "high")

    def test_object_payload(self):
        items = self._parse('{"contradictions": [{"description": "X", "severity": "medium"}]}')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].description, "X")

    def test_garbage_returns_empty(self):
        self.assertEqual(self._parse("maaf saya tidak mengerti"), [])

    def test_null_items_ignored(self):
        items = self._parse('[{"description": "A"}, null, "teks"]')
        self.assertEqual(len(items), 1)


class TestScenariosAgent(unittest.TestCase):
    def _parse(self, response: str):
        return ScenariosAgent(None)._parse_response(response)

    def test_array_payload(self):
        scenarios = self._parse(
            '[{"name": "Bull", "probability": 40}, '
            '{"name": "Bear", "probability": 30}, '
            '{"name": "Base", "probability": 30}]'
        )
        self.assertEqual(len(scenarios), 3)
        self.assertEqual(scenarios[0].name, "Bull")
        self.assertEqual(scenarios[0].probability, 40)

    def test_object_payload(self):
        scenarios = self._parse(
            '{"scenarios": [{"name": "Bull", "probability": 50}]}'
        )
        self.assertEqual(len(scenarios), 1)
        self.assertEqual(scenarios[0].probability, 50)

    def test_garbage_uses_default_three(self):
        scenarios = self._parse("tidak valid")
        self.assertEqual(len(scenarios), 3)
        names = {s.name for s in scenarios}
        self.assertEqual(names, {"Bull Case", "Bear Case", "Base Case"})

    def test_array_limited_to_three(self):
        scenarios = self._parse(
            '[{"name": "A"}, {"name": "B"}, {"name": "C"}, {"name": "D"}]'
        )
        self.assertEqual(len(scenarios), 3)


class TestThesisAgent(unittest.TestCase):
    def _parse(self, response: str):
        return ThesisAgent(None)._parse_response(response)

    def test_array_payload_returns_none(self):
        self.assertIsNone(self._parse('[{"direction": "bullish"}]'))

    def test_object_payload(self):
        thesis = self._parse(
            '{"direction": "bearish", "confidence": 0.7, "thesis_summary": "s"}'
        )
        self.assertIsNotNone(thesis)
        self.assertEqual(thesis.direction, "bearish")
        self.assertAlmostEqual(thesis.confidence, 0.7)

    def test_garbage_returns_none(self):
        self.assertIsNone(self._parse("bukan json"))


class TestConfidenceAgent(unittest.TestCase):
    def _parse(self, response: str):
        return ConfidenceAgent(None)._parse_response(response)

    def test_array_payload_returns_none(self):
        self.assertIsNone(self._parse('[{"overall_score": 0.9}]'))

    def test_object_payload(self):
        data = self._parse('{"overall_score": 0.8, "assessment": "bagus"}')
        self.assertEqual(data["overall"], 0.8)
        self.assertEqual(data["assessment"], "bagus")

    def test_garbage_returns_none(self):
        self.assertIsNone(self._parse("tidak valid"))


class TestRiskGates(unittest.TestCase):
    def _parse(self, response: str):
        return RiskGates(None)._parse_response(response)

    def test_array_payload_returns_none(self):
        self.assertIsNone(self._parse('[{"overall_risk_level": "high"}]'))

    def test_object_payload(self):
        data = self._parse('{"overall_risk_level": "high", "summary": "hati-hati"}')
        self.assertEqual(data["overall_risk_level"], "high")
        self.assertEqual(data["summary"], "hati-hati")


class TestResearchAgent(unittest.TestCase):
    def test_array_payload_does_not_crash(self):
        stub = _StubAI('[{"key_drivers": ["X"]}]')
        agent = ResearchAgent(stub, None, None, None)
        ctx = ResearchContext()
        ctx.raw_context = "data pasar"

        asyncio.run(agent._llm_analyze_context(ctx, "berapa harga gold?"))

        # Parsing gagal → llm_analysis berisi teks mentah, field lain default
        self.assertEqual(ctx.llm_analysis, '[{"key_drivers": ["X"]}]')
        self.assertEqual(ctx.key_drivers, [])
        self.assertEqual(ctx.market_regime, "unknown")

    def test_object_payload_parsed(self):
        stub = _StubAI('{"key_drivers": ["CPI tinggi"], "market_regime": "volatile"}')
        agent = ResearchAgent(stub, None, None, None)
        ctx = ResearchContext()
        ctx.raw_context = "data pasar"
        # Pertanyaan BEDA dari test array di atas — cache key menyertakan
        # question, jadi hasil array tidak boleh bocor ke test ini.
        asyncio.run(agent._llm_analyze_context(ctx, "analisis emas hari ini?"))

        self.assertEqual(ctx.key_drivers, ["CPI tinggi"])
        self.assertEqual(ctx.market_regime, "volatile")


class TestSentimentRefine(unittest.TestCase):
    def test_array_payload_falls_back_to_data_score(self):
        stub = _StubAI('[{"score": 0.9}]')
        analyzer = SentimentAnalyzer(ai_engine=stub)
        result = asyncio.run(
            analyzer._llm_refine("FOREX", [{"headline": "h", "score": 0.4}], 0.4)
        )
        self.assertEqual(result["score"], 0.4)  # fallback skor data, bukan crash
        self.assertEqual(result["assessment"], "")


if __name__ == "__main__":
    unittest.main()
