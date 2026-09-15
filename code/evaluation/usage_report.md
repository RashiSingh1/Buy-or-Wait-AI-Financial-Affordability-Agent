## Development validation run

- Provider/model: none; deterministic Python engine only
- AI/message model calls: 0 in the sample workflow; mocked transport calls only in tests
- AI/image model calls: 0 in the sample workflow; mocked transport calls only in tests
- Input tokens: 0
- Output tokens: 0
- Total tokens: 0
- Average tokens per sample request: 0
- Estimated total cost: 0
- Estimated cost per sample request: 0
- Evidence cache hits: 1 recorded by the local runtime tracker
- Evidence cache misses: 0 recorded by the sample workflow
- API/rate-limit errors: 0

Validation used only `dataset/sample_requests.csv`, supporting datasets, and
synthetic in-memory fixtures. The sample-only workflow generated 25 decisions
with the required eight output columns and unique request IDs. The test suite
contained 20 passing tests covering parsing, currency conversion, calendar-month
recurrence, pending and
cancelled events, minimum-balance safety, deadlines, installment matching,
spending changes, image-linked blank amounts, output validation, structured
Gemini response parsing, malformed responses, mocked API failure fallback,
mocked image extraction, ambiguous-message gating, and persistent cache hits.

The initial exact sample comparison was 0/25 complete rows. After correcting
calendar-month recurrence and numeric amount formatting, field parity was:
`request_id` 25/25, `amount_safe_to_pay` 4/25, `affordability_status` 17/25,
`recommended_payment_method` 19/25, `payment_plan` 16/25,
`earliest_date_for_full_payment` 8/25, `spending_changes_needed` 22/25, and
`decision_explanation` 0/25. Complete-row parity remained 0/25 because the
public sample explanations use request-specific prose and several remaining
financial differences require further investigation of sample evidence and
forecast policy.

The guarded final path was not executed and `dataset/requests.csv` was not
processed.
