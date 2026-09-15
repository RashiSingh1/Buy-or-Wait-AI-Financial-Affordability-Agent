import unittest
from datetime import date
from pathlib import Path

from code.agent import (
    DataSet,
    Decision,
    Event,
    PaymentOption,
    Profile,
    RateTable,
    Request,
    decide,
    load_dataset,
    validate_decision,
)


def profile(methods=frozenset({"full_payment"}), reducible=frozenset(),
            stoppable=frozenset()):
    return Profile(
        "u", "USD", 1000, 300, frozenset(), frozenset({"rent"}),
        reducible, stoppable, methods, 3,
    )


def request(amount=500, deadline=date(2025, 1, 10), partial=False):
    return Request("r", "u", date(2025, 1, 1), "purchase", amount,
                   deadline, partial, "")


class ComprehensiveTests(unittest.TestCase):
    def test_sample_workflow_and_exact_shape(self):
        dataset = Path(__file__).resolve().parents[2] / "dataset"
        data = load_dataset(dataset, dataset / "sample_requests.csv")
        decisions = [validate_decision(decide(data, item), item,
                                       data.options.get(item.request_id, ()), data)
                     for item in data.requests]
        self.assertEqual(len(decisions), 25)
        self.assertEqual(set(decisions[0].row()), {
            "request_id", "amount_safe_to_pay", "affordability_status",
            "recommended_payment_method", "payment_plan",
            "earliest_date_for_full_payment", "spending_changes_needed",
            "decision_explanation",
        })

    def test_cancelled_event_does_not_reduce_capacity(self):
        data = DataSet(
            profiles={"u": profile()},
            rates=RateTable(),
            events=[Event("cancelled", "u", "expense", "x", "rent", "debit",
                          600, "USD", date(2025, 1, 2), date(2025, 1, 2),
                          "cancelled")],
        )
        self.assertEqual(decide(data, request(500)).recommended_payment_method,
                         "full_payment")

    def test_deadline_and_minimum_balance_reject_unsafe_plan(self):
        data = DataSet(profiles={"u": profile()}, rates=RateTable())
        item = request(800, date(2025, 1, 2), partial=True)
        decision = validate_decision(
            type("D", (), {
                "request_id": "r", "amount_safe_to_pay": 700,
                "affordability_status": "affordable_with_plan",
                "recommended_payment_method": "partial_payment",
                "payment_plan": "2025-01-01:700|2025-01-02:100",
                "earliest_date_for_full_payment": "2025-01-02",
                "spending_changes_needed": "none",
                "decision_explanation": "x",
            })(),
            item, data=data,
        )
        self.assertEqual(decision.recommended_payment_method, "not_recommended")

    def test_installment_option_must_match_and_finish_by_deadline(self):
        data = DataSet(
            profiles={"u": profile(frozenset({"installments"}))},
            rates=RateTable(),
            options={"r": [PaymentOption(
                "payment_option_1", "r", "installments", 250, 2,
                date(2025, 1, 2), 1, 0, 500,
            )]},
        )
        decision = decide(data, request(500, date(2025, 1, 3)))
        self.assertEqual(decision.recommended_payment_method, "installments")
        self.assertEqual(decision.payment_plan,
                         "2025-01-02:250|2025-01-03:250")

    def test_reducible_and_stoppable_events_are_validated(self):
        data = DataSet(
            profiles={"u": profile(reducible=frozenset({"dining"}),
                                   stoppable=frozenset({"streaming"}))},
            rates=RateTable(),
            events=[
                Event("flex", "u", "subscription", "x", "streaming", "debit",
                      100, "USD", date(2025, 1, 2), date(2025, 1, 2),
                      "scheduled", flexibility="stoppable"),
                Event("protected", "u", "expense", "x", "rent", "debit",
                      100, "USD", date(2025, 1, 2), date(2025, 1, 2),
                      "scheduled", flexibility="stoppable"),
            ],
        )
        self.assertEqual(decide(data, request(500)).recommended_payment_method,
                         "full_payment")
        self.assertTrue(validate_decision(
            type("D", (), {
                "request_id": "r", "amount_safe_to_pay": 500,
                "affordability_status": "affordable_with_plan",
                "recommended_payment_method": "full_payment",
                "payment_plan": "2025-01-01:500",
                "earliest_date_for_full_payment": "2025-01-01",
                "spending_changes_needed": "stop:flex",
                "decision_explanation": "x",
            })(), request(500), data=data).recommended_payment_method
            == "full_payment")

    def test_image_linked_blank_amount_is_not_zero(self):
        dataset = Path(__file__).resolve().parents[2] / "dataset"
        data = load_dataset(dataset, dataset / "sample_requests.csv")
        event = next(item for item in data.events if item.event_id == "event_253")
        self.assertIsNone(event.amount)
        self.assertTrue(any(link.related_event_id == event.event_id
                            for link in data.images))

    def test_monthly_recurrence_preserves_calendar_day(self):
        data = DataSet(
            profiles={"u": profile()},
            rates=RateTable(),
            events=[
                Event("salary_1", "u", "income", "salary", "salary", "credit",
                      1000, "USD", date(2024, 1, 15), date(2024, 1, 15),
                      "settled"),
                Event("salary_2", "u", "income", "salary", "salary", "credit",
                      1000, "USD", date(2024, 2, 15), date(2024, 2, 15),
                      "settled"),
                Event("salary_3", "u", "income", "salary", "salary", "credit",
                      1000, "USD", date(2024, 3, 15), date(2024, 3, 15),
                      "settled"),
            ],
        )
        item = Request("r", "u", date(2024, 4, 1), "purchase", 1,
                       date(2024, 4, 30), False, "")
        forecast = __import__("code.agent", fromlist=["Forecast"]).Forecast(
            data, item, data.profiles["u"]
        )
        self.assertEqual(forecast.cash[date(2024, 4, 15)], 1000)

    def test_output_validation_rejects_bad_status_amount_date_and_total(self):
        item = request(500)
        base = {
            "request_id": "r", "amount_safe_to_pay": 500,
            "affordability_status": "affordable_now",
            "recommended_payment_method": "full_payment",
            "payment_plan": "2025-01-01:400",
            "earliest_date_for_full_payment": "2025-01-01",
            "spending_changes_needed": "none",
            "decision_explanation": "x",
        }
        for field, value in (
            ("affordability_status", "unknown"),
            ("amount_safe_to_pay", -1),
            ("payment_plan", "not-a-date:500"),
            ("payment_plan", "2025-01-01:400"),
        ):
            candidate = dict(base)
            candidate[field] = value
            decision = validate_decision(Decision(**candidate), item)
            self.assertEqual(decision.recommended_payment_method,
                             "not_recommended")

    def test_output_validation_rejects_invalid_installment_option(self):
        item = request(500, date(2025, 1, 3))
        candidate = Decision(
            "r", 500, "affordable_with_plan", "installments",
            "2025-01-02:200|2025-01-03:300", "2025-01-03", "none", "x",
        )
        option = PaymentOption(
            "payment_option_1", "r", "installments", 250, 2,
            date(2025, 1, 2), 1, 0, 500,
        )
        checked = validate_decision(candidate, item, [option])
        self.assertEqual(checked.recommended_payment_method, "not_recommended")


if __name__ == "__main__":
    unittest.main()
