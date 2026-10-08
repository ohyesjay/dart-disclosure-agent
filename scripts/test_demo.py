#!/usr/bin/env python3
"""Smoke checks using fictional source files, with no model or private corpus."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.evidence import annual_facts, deterministic_financial_answer
from app.retriever import Retriever
from scripts.build_demo_index import build


class SyntheticPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.directory.name) / 'demo.db'
        build(cls.db_path)
        cls.retriever = Retriever(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def answer(self, question):
        result = self.retriever.search(question, limit=7)
        facts = []
        for index, hit in enumerate(result['hits'], 1):
            for fact in annual_facts(hit, question):
                fact['evidence_index'] = index
                facts.append(fact)
        return result, deterministic_financial_answer(question, facts, result['target_companies'])

    def test_two_company_revenue_and_difference(self):
        result, answer = self.answer('가상알파와 가상베타의 2025년 사업보고서 연결 매출액을 비교하고 차이를 계산해줘')
        self.assertEqual(result['target_companies'], ['가상알파', '가상베타'])
        self.assertIn('1,000,000,000원', answer)
        self.assertIn('2,000,000,000원', answer)
        self.assertIn('더 작습니다', answer)
        self.assertEqual({hit['rcept_no'] for hit in result['hits']},
                         {'DEMO-ALPHA-2025', 'DEMO-BETA-2025'})

    def test_margin_uses_same_period_and_units(self):
        _, answer = self.answer('가상알파와 가상베타의 2025년 사업보고서 연결 매출액과 영업이익으로 영업이익률을 계산해줘')
        self.assertIn('15%', answer)
        self.assertIn('10%', answer)
        self.assertLess(answer.index('가상베타'), answer.index('가상알파'))

    def test_missing_period_does_not_borrow_annual_report(self):
        result = self.retriever.search('가상알파의 2023년 사업보고서 연결 매출액은?', limit=7)
        self.assertEqual(result['hits'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
