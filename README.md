# Buy or Wait — AI Financial Affordability Agent

Buy or Wait evaluates whether a requested expense can be completed safely from a user's reconstructed financial position. It combines structured CSV data with optional extraction of factual evidence from ambiguous messages or linked images, then uses deterministic Python logic to forecast cash flow, validate payment plans, and produce a structured affordability decision.

The system is designed so that an AI model may clarify evidence, but never decides affordability or performs the financial arithmetic.

## Background

This project was developed for **HackerRank Orchestrate — September 2026 Edition**.

## Problem Statement

A purchase cannot be judged safely from the current balance alone. A user may have pending debits, recurring obligations, essential expenses, confirmed future income, payment preferences, installment offers, or a minimum balance they want to preserve.

Buy or Wait reconstructs those constraints from the supplied financial profiles, events, exchange rates, payment options, messages, and images. A recommendation is considered safe only when every payment in the proposed plan can be made, the request is completed by its deadline where applicable, and the projected balance never falls below the user's minimum throughout the forecast window.

## Engineering Highlights

### Deterministic financial decision engine

`code/agent.py` owns the financial decision. It parses the supplied data, reconstructs user state, converts amounts into the user's home currency, projects cash flow, evaluates candidate plans, and validates the final decision. This keeps monetary calculations reproducible and inspectable.

### Daily 90-day safety forecast

`Forecast` builds a daily cash-flow timeline from the request date through 90 days. It accounts for the current balance, future scheduled debits, eligible income, recurring events, request payments, and permitted spending changes. A plan is safe only if every projected balance remains at least the profile's configured minimum.

### Calendar-aware recurrence detection

Historical settled events are grouped by category and normalized description. Recurrence is projected only when the history provides enough evidence: recurring debits require at least three historical gaps between occurrences with consistent intervals, while recurring salary/income can use a confirmed interval. Monthly patterns preserve the calendar day rather than drifting through fixed 30-day increments.

### Maximum safe amount by binary search

The engine searches for the largest amount payable on the request date without optional spending changes. Safety is monotone with respect to the payment amount, so `decide` performs 60 binary-search iterations over `[0, requested_amount]`, using the complete forecast safety check as the predicate.

### Dated currency conversion

`RateTable` uses the fixed dated exchange-rate data supplied by the challenge. It supports direct conversions, inverse rates, and breadth-first multi-hop conversion paths, selecting the latest available rate on or before the event date when possible.

### Payment-plan validation and selection

Installment schedules are created only from supplied payment options and must finish by the requested completion date. Partial-payment plans contain exactly two payments and must add up to the requested amount. The final validation layer rejects invalid dates, negative amounts, mismatched totals, unsafe schedules, invalid installment schedules, and unauthorized spending changes.

Candidate plans are ordered by the implementation's ranking tuple:

1. Affordability status priority: `affordable_now`, then `affordable_with_plan`, then `affordable_later`
2. Completion by the requested deadline
3. Fewer spending changes
4. Lower total payable cost
5. Earlier start date
6. Fewer payments

### Conservative evidence processing

Deterministic parsing handles explicit amounts, dates, currencies, and financial status words in messages. The optional evidence service asks Gemini for structured factual extraction only when deterministic evidence is insufficient, or when a linked image is needed to clarify a blank event amount. Extracted facts are converted into the deterministic data model before financial evaluation.

### Automated verification

The repository includes tests for parsing, currency conversion, pending and cancelled events, minimum-balance safety, deadlines, installment matching, spending-change rules, recurrence, output validation, structured evidence parsing, cache behavior, image extraction, ambiguous-message gating, and API-failure fallback.

## Features

- CSV loading for profiles, events, requests, payment options, messages, images, and exchange rates
- Explicit handling of settled, pending, scheduled, failed, cancelled, and unrealized event states
- Protection of essential and user-protected spending categories
- Forecasting of recurring income and expenses with calendar-month support
- Dated direct, inverse, and multi-hop currency conversion
- Maximum safe request-date payment calculation
- Full-payment, partial-payment, installment, wait, and refusal decisions
- Deadline-aware payment-plan construction
- Validation of plan totals, dates, option schedules, safety, and spending-change permissions
- Optional structured Gemini evidence extraction for ambiguous messages and linked images
- Persistent evidence caching and usage/error tracking
- Development and final CLI modes with an explicit final-evaluation guard
- CSV output with the exact challenge schema

## Decision Model

The engine emits one of four affordability statuses:

| Status | Meaning |
|---|---|
| `affordable_now` | The full request is safe on the request date using an accepted full-payment method, with no required spending changes. |
| `affordable_with_plan` | The full request can be completed safely through an accepted partial-payment schedule, a supplied installment option, or permitted spending changes. |
| `affordable_later` | The full request is not safe immediately but becomes safe as a single payment later within the forecast window. |
| `not_affordable` | No eligible plan completes the request safely within the forecast horizon and constraints. |

The recommended method is one of `full_payment`, `partial_payment`, `installments`, `wait`, or `not_recommended`.

## Financial Decision Logic

For each request, the engine:

1. Loads the user's home currency, current available balance, minimum balance, protected categories, flexible categories, accepted payment methods, and installment limit.
2. Converts event amounts into the home currency using the applicable dated exchange rate.
3. Excludes cancelled and failed events, does not treat unrealized investment value as cash, and reserves eligible future debits.
4. Counts scheduled salary/income and explicitly confirmed future salary evidence while excluding unsupported or unconfirmed future credits.
5. Detects supported recurring income and expense patterns and projects them through the 90-day window.
6. Applies the proposed request payments to the timeline.
7. Checks every relevant date against `minimum_balance_to_keep`.
8. Evaluates full payment, permitted spending changes, supplied installment offers, partial payment, and waiting.
9. Selects the best safe eligible plan and validates it before writing the output row.

Affordability is therefore evaluated across the financial timeline rather than from the current balance alone.

## Maximum Safe Amount

The engine first evaluates whether a candidate payment schedule is safe using `Forecast.safe`. It then searches for the maximum safe amount payable on the request date without optional spending changes:

```python
low, high = 0.0, request.requested_amount
for _ in range(60):
    mid = (low + high) / 2
    if forecast.safe([(request.request_date, mid)]):
        low = mid
    else:
        high = mid
```

The result is capped at the requested amount and rounded for output formatting.

## Payment Plan Selection

The implementation considers only payment methods accepted by the user's profile. Installment schedules must come from `request_payment_options.csv`, respect the user's maximum installment limit, cover the requested amount, and finish by the desired completion date.

Valid candidates are ranked using the actual decision-engine ordering:

1. Status priority, favoring `affordable_now` over `affordable_with_plan` over `affordable_later`
2. Completion by the desired deadline
3. No spending changes
4. Lower total payable cost
5. Earlier start
6. Fewer payments

The final validation pass independently rechecks the selected schedule and converts invalid decisions into a safe `not_recommended` result.

## AI / Evidence Processing

Gemini is optional and disabled unless explicitly enabled with an environment configuration and an available key. It is used only for factual extraction from:

- ambiguous messages where deterministic parsing lacks required evidence
- linked images associated with events whose structured amount is blank

Responses are accepted only through the structured evidence parser. The model is instructed not to provide affordability or payment recommendations. Deterministic Python remains responsible for:

- arithmetic and currency conversion
- cash-flow forecasting
- minimum-balance checks
- maximum safe amount calculation
- payment-plan construction and validation
- payment-option matching and ranking
- final affordability status and output generation

## Architecture

```text
CSV inputs
  |
  +--> Profiles, events, requests, options, messages, images, rates
  |
  v
Data loading and relationship joins
  |
  +--> Deterministic message parsing
  |        |
  |        +--> Optional Gemini evidence extraction when evidence is insufficient
  |
  +--> Linked image evidence for blank event amounts
  |
  v
Structured financial facts
  |
  v
Deterministic financial engine (code/agent.py)
  |
  +--> Dated currency conversion
  +--> Recurring income and expense projection
  +--> Daily 90-day cash-flow forecast
  +--> Minimum-balance safety checks
  +--> Maximum safe amount search
  +--> Payment-plan generation and ranking
  +--> Final decision validation
  |
  v
CSV writer
  |
  v
output.csv
```

## Workflow

1. `code/main.py` resolves the repository and dataset paths.
2. `load_dataset` loads supporting CSV files and constructs the typed in-memory data model.
3. The selected request file is loaded only by the active CLI mode.
4. Each request is evaluated by `decide`.
5. The forecast reconstructs future cash flow and checks candidate payment schedules.
6. The selected decision is passed through `validate_decision`.
7. `write_output` writes rows using the exact required column order.

Development mode reads `dataset/sample_requests.csv` and writes `code/development_output.csv`. Final mode is explicitly enabled with `RUN_FINAL_EVALUATION=1` and writes the root-level `output.csv`.

## Output Schema

The generated CSV contains exactly these eight columns:

| Field | Description |
|---|---|
| `request_id` | Identifier copied from the input request. |
| `amount_safe_to_pay` | Largest amount safe on the request date before optional spending changes, capped at the requested amount. |
| `affordability_status` | One of the four statuses defined above. |
| `recommended_payment_method` | `full_payment`, `partial_payment`, `installments`, `wait`, or `not_recommended`. |
| `payment_plan` | Chronological `YYYY-MM-DD:amount` entries separated by `|`, or `none`. |
| `earliest_date_for_full_payment` | First forecast date when a single full payment is safe, or empty when none is found within the forecast window. |
| `spending_changes_needed` | `none`, or up to three authorized `stop:<event_id>` / `reduce_to:<event_id>:<amount>` actions. |
| `decision_explanation` | Concise explanation generated from the selected deterministic decision. |

## Evaluation

The repository contains a completed final evaluation artifact with 250 request rows in the root-level `output.csv`. The implementation does not record a model call or token/cost usage for that run; the corresponding information is documented in `code/evaluation/usage_report.md` without inferred values.

## Testing

The test suite is part of the public project under `code/tests/`:

- `test_agent.py` covers sample-request loading, numeric and message-evidence
  parsing, inverse and multi-hop currency conversion, safe full-payment
  decisions, and reservation of pending debits.
- `test_comprehensive.py` covers sample workflow shape, cancelled events,
  minimum-balance and deadline rejection, installment matching, flexible
  spending-change validation, linked blank image amounts, calendar-month
  recurrence, and invalid output/payment-plan cases.
- `test_evidence.py` covers structured and fenced Gemini response parsing,
  malformed responses, persistent cache hits, linked-image extraction,
  ambiguous-message gating, and API-failure fallback.

Together, these tests demonstrate coverage of:

- numeric, date, and message evidence parsing
- inverse and multi-hop exchange-rate conversion
- pending and cancelled event behavior
- calendar-month recurrence
- minimum-balance and deadline safety
- installment schedule matching
- flexible spending-change validation
- output schema and payment-plan validation
- structured and fenced Gemini response parsing
- malformed-response handling
- evidence caching
- linked-image extraction
- ambiguous-message gating
- API failure fallback

Only test source files are shown in the project tree. Generated Python
bytecode, cache directories, and temporary test output are local artifacts and
are not project files.

Run the development test suite from the repository root:

```powershell
python -m unittest discover -s code/tests -v
```

## AI Usage / Cost Control

`code/evidence.py` includes:

- SHA-256 cache keys derived from the evidence prompt and optional image bytes
- persistent cache files under the ignored `runtime/` directory
- cache-hit and cache-miss counters
- model-call counters
- input and output token counters when returned by the API
- estimated-cost and error-type fields
- limited retry behavior for transient failures
- explicit rate-limit error tracking

The evidence layer avoids model calls when deterministic evidence is sufficient. API failures return no extracted evidence so the deterministic engine can continue conservatively.

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3 |
| Data processing | Python standard library `csv`, `dataclasses`, `datetime`, and `pathlib` |
| AI evidence extraction | Google Gemini REST API via Python standard-library HTTP utilities |
| Decision engine | Deterministic Python |
| Output | Python `csv.DictWriter` |
| Testing | Python `unittest` |

No third-party Python dependency or package manifest is required by the current implementation.

## Project Structure

```text
.
├── README.md
├── .gitignore
├── code/
│   ├── __init__.py
│   ├── agent.py
│   ├── evidence.py
│   ├── main.py
│   ├── evaluation/
│   │   ├── main.py
│   │   └── usage_report.md
│   └── tests/
│       ├── test_agent.py
│       ├── test_comprehensive.py
│       └── test_evidence.py
├── dataset/
│   ├── financial_events.csv
│   ├── financial_profiles.csv
│   ├── exchange_rates.csv
│   ├── images.csv
│   ├── messages.csv
│   ├── request_payment_options.csv
│   ├── requests.csv
│   ├── sample_requests.csv
│   └── media/images/
```

`runtime/`, `log.txt`, Python bytecode, and the development output are local/generated artifacts and are excluded by `.gitignore` or the submission packaging process.

## Local Setup

The implementation uses only the Python standard library.

From the repository root on Windows:

```powershell
python --version
python -m unittest discover -s code/tests -v
python code\main.py
```

The development command reads only the public sample requests and writes `code/development_output.csv`.

The guarded final command is:

```powershell
$env:RUN_FINAL_EVALUATION = "1"
python code\main.py
```

It reads the evaluation request file and writes the root-level `output.csv`. The final command should be run only when the evaluation dataset is intentionally being processed.

### Optional Gemini configuration

Gemini evidence extraction is opt-in. Configure `GEMINI_ENABLE=1` and provide `GEMINI_API_KEY` through the process environment or a local ignored `.env` file. Never commit the file or include it in an archive. The application does not print the key.

## Security

- Secrets are read from environment variables or a local ignored `.env` file.
- `.env` and `.env.*` are ignored by `.gitignore`, except for an optional `.env.example`.
- Runtime evidence caches and usage data are ignored.
- The optional AI layer receives only evidence selected by the evidence service and returns factual structured fields.
- API keys, credentials, and sensitive configuration are not included in the submission archive.

## Design Principles

- **Deterministic financial reasoning:** calculations and final classifications remain reproducible Python behavior.
- **Safety before affordability:** every proposed schedule is checked against the minimum balance across the forecast timeline.
- **AI only where it adds value:** model extraction is limited to ambiguous, unstructured evidence.
- **Evidence over instructions:** messages and images are treated as untrusted financial evidence, not executable commands.
- **Explainable decisions:** output fields expose the selected method, schedule, spending changes, and minimum-balance rationale.
- **Reproducible execution:** fixed input rates, explicit CLI modes, typed data structures, and standard-library tooling keep runs portable.

## Author

**Rashi Kumari**

