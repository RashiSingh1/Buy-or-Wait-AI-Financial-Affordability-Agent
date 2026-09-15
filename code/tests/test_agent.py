import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import (DataSet, Event, Profile, RateTable, Request, decide,
                   load_dataset, parse_amount, parse_evidence, validate_decision)


class AgentTests(unittest.TestCase):
    def test_public_sample_run(self):
        dataset = Path(__file__).resolve().parents[2] / "dataset"
        data = load_dataset(dataset, dataset / "sample_requests.csv")
        self.assertEqual(len(data.requests), 25)
        self.assertEqual(len({r.request_id for r in data.requests}), 25)
        for decision in [validate_decision(decide(data, r), r, data.options.get(r.request_id, ()))
                         for r in data.requests]:
            self.assertIn(decision.affordability_status, {
                "affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"
            })
            self.assertGreaterEqual(decision.amount_safe_to_pay, 0)

    def test_numeric_and_evidence_parsing(self):
        self.assertEqual(parse_amount("IDR 46,018,000"), 46018000)
        self.assertEqual(parse_amount("1.234,56"), 1234.56)
        evidence = parse_evidence("Gaji dikonfirmasi IDR 42.500.000 pada 2025-08-15")
        self.assertEqual(evidence.amounts, (42500000.0,))
        self.assertEqual(evidence.dates, (date(2025, 8, 15),))
        self.assertIn("confirmed", evidence.facts)

    def test_rate_inverse_and_path(self):
        rates = RateTable([
            {"rate_date": "2025-01-01", "from_currency": "USD", "to_currency": "EUR", "rate": "0.8"},
            {"rate_date": "2025-01-01", "from_currency": "EUR", "to_currency": "INR", "rate": "100"},
        ])
        self.assertAlmostEqual(rates.convert(10, "EUR", "USD", date(2025, 1, 2)), 12.5)
        self.assertAlmostEqual(rates.convert(1, "USD", "INR", date(2025, 1, 2)), 80)

    def test_safe_full_payment_and_gate(self):
        profile = Profile("u", "USD", 1000, 300, frozenset(), frozenset({"rent"}),
                          frozenset(), frozenset(), frozenset({"full_payment"}), None)
        request = Request("r", "u", date(2025, 1, 1), "purchase", 500, date(2025, 1, 10), False, "")
        data = DataSet(profiles={"u": profile}, rates=RateTable())
        decision = validate_decision(decide(data, request), request)
        self.assertEqual(decision.recommended_payment_method, "full_payment")
        self.assertEqual(decision.affordability_status, "affordable_now")
        self.assertEqual(decision.payment_plan, "2025-01-01:500")

    def test_pending_debit_is_reserved(self):
        profile = Profile("u", "USD", 1000, 300, frozenset(), frozenset({"rent"}),
                          frozenset(), frozenset(), frozenset({"full_payment"}), None)
        request = Request("r", "u", date(2025, 1, 1), "purchase", 750, date(2025, 1, 10), False, "")
        event = Event("e", "u", "expense", "bill", "rent", "debit", 500, "USD",
                      date(2025, 1, 2), date(2025, 1, 2), "pending")
        data = DataSet(profiles={"u": profile}, events=[event], rates=RateTable())
        decision = decide(data, request)
        self.assertEqual(decision.recommended_payment_method, "not_recommended")
        self.assertEqual(decision.amount_safe_to_pay, 200)


if __name__ == "__main__":
    unittest.main()
