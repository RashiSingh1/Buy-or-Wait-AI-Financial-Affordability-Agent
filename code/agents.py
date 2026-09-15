"""Deterministic Buy or Wait decision engine.

The module intentionally has no third-party dependencies and does not read any
files at import time.  ``code.main`` is the only executable entry point.
"""

from __future__ import annotations

import csv
import itertools
import re
import statistics
import calendar
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

try:
    from .evidence import EvidenceService, ExtractedEvidence
except ImportError:
    from evidence import EvidenceService, ExtractedEvidence

OUTPUT_COLUMNS = [
    "request_id", "amount_safe_to_pay", "affordability_status",
    "recommended_payment_method", "payment_plan",
    "earliest_date_for_full_payment", "spending_changes_needed",
    "decision_explanation",
]
STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
_NUMBER = re.compile(r"(?<![\w.])(?:[$€£₹]|IDR|INR|ZAR|USD|EUR)?\s*[-+]?\d[\d.,]*(?:\s*[kKmM])?")
_ISO_DATE = re.compile(r"\b(20\d{2}-\d{1,2}-\d{1,2})\b")
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7,
    "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def parse_amount(value: object) -> Optional[float]:
    """Parse English and Indonesian-style numbers without guessing blank values."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.\-+kKmM]", "", text)
    if not text:
        return None
    multiplier = 1.0
    if text[-1:].lower() == "k":
        multiplier, text = 1000.0, text[:-1]
    elif text[-1:].lower() == "m":
        multiplier, text = 1_000_000.0, text[:-1]
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    if "," in text and "." in text:
        # The final separator is the decimal separator.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        text = "".join(parts) if len(parts[-1]) == 3 and all(p.isdigit() for p in parts) else text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        return sign * float(text) * multiplier
    except ValueError:
        return None


def parse_date(value: object) -> Optional[date]:
    if value is None:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    m = re.search(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\b", text)
    if m and m.group(2).lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),?\s+(20\d{2})\b", text)
    if m and m.group(1).lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


@dataclass(frozen=True)
class Evidence:
    amounts: tuple[float, ...] = ()
    dates: tuple[date, ...] = ()
    currencies: tuple[str, ...] = ()
    facts: frozenset[str] = frozenset()


def parse_evidence(text: str) -> Evidence:
    """Extract only explicit numeric/date/status facts from a message."""
    source = text or ""
    # Do not mistake the year/month/day parts of an ISO date for money.
    source_for_numbers = _ISO_DATE.sub(" ", source)
    amounts = tuple(x for x in (parse_amount(m.group(0)) for m in _NUMBER.finditer(source_for_numbers)) if x is not None)
    dates = [parse_date(m.group(1)) for m in _ISO_DATE.finditer(text or "")]
    dates = [d for d in dates if d]
    for m in re.finditer(r"\b(\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\b", text or ""):
        d = parse_date(m.group(0))
        if d:
            dates.append(d)
    upper = (text or "").upper()
    currencies = tuple(c for c in ("IDR", "INR", "ZAR", "USD", "EUR") if c in upper)
    lower = (text or "").lower()
    facts = {
        token for token in (
            "confirmed", "scheduled", "pending", "settled", "cancelled", "canceled",
            "reversed", "unrealized", "not withdrawable", "tidak disetujui",
            "belum disetujui", "sudah dikonfirmasi", "dikonfirmasi", "menunggu",
        ) if token in lower
    }
    if "dikonfirmasi" in facts or "sudah dikonfirmasi" in facts:
        facts.add("confirmed")
    if "menunggu" in facts or "belum disetujui" in facts or "tidak disetujui" in facts:
        facts.add("pending")
    return Evidence(amounts, tuple(dict.fromkeys(dates)), currencies, frozenset(facts))


def _float(value: object, default: float = 0.0) -> float:
    result = parse_amount(value)
    return default if result is None else result


@dataclass(frozen=True)
class Profile:
    user_id: str
    home_currency: str
    balance: float
    minimum: float
    priorities: frozenset[str]
    protected: frozenset[str]
    reducible: frozenset[str]
    stoppable: frozenset[str]
    methods: frozenset[str]
    max_installment_months: Optional[int]


@dataclass
class Event:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Optional[float]
    currency: str
    event_date: Optional[date]
    settlement_date: Optional[date]
    status: str
    linked_event_id: str = ""
    flexibility: str = "fixed"
    minimum_allowed_amount: Optional[float] = None


@dataclass(frozen=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: float
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: int
    financing_fee: float
    total_payable_amount: float

    def schedule(self) -> list[tuple[date, float]]:
        return [
            (self.first_payment_date + timedelta(days=self.payment_frequency_days * i), self.payment_amount)
            for i in range(self.number_of_payments)
        ]


@dataclass(frozen=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: float
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


@dataclass(frozen=True)
class Message:
    message_id: str
    user_id: str
    request_id: str
    related_event_id: str
    sent_at: str
    source_type: str
    message_text: str


@dataclass(frozen=True)
class ImageLink:
    image_id: str
    user_id: str
    request_id: str
    related_event_id: str


@dataclass
class DataSet:
    profiles: dict[str, Profile] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    rates: "RateTable | None" = None
    options: dict[str, list[PaymentOption]] = field(default_factory=lambda: defaultdict(list))
    messages: list[Message] = field(default_factory=list)
    images: list[ImageLink] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    dataset_dir: Optional[Path] = None
    evidence_service: Optional[EvidenceService] = None


def _rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def load_profiles(path: Path) -> dict[str, Profile]:
    result = {}
    for row in _rows(path):
        split = lambda name: frozenset(x.strip() for x in row.get(name, "").split("|") if x.strip())
        raw_months = row.get("max_installment_months", "").strip()
        result[row["user_id"]] = Profile(
            row["user_id"], row["home_currency"], _float(row["current_available_balance"]),
            _float(row["minimum_balance_to_keep"]), split("financial_priorities"),
            split("expense_categories_to_protect"), split("expense_categories_user_is_willing_to_reduce"),
            split("expense_categories_user_is_willing_to_stop"), split("payment_methods_user_will_consider"),
            int(raw_months) if raw_months else None,
        )
    return result


def load_events(path: Path) -> list[Event]:
    result = []
    for row in _rows(path):
        result.append(Event(
            row["event_id"], row["user_id"], row["event_type"], row["description"], row["category"],
            row["direction"], parse_amount(row.get("amount")), row.get("currency", ""),
            parse_date(row.get("event_date")), parse_date(row.get("settlement_date")),
            row.get("status", "").lower(), row.get("linked_event_id", ""), row.get("flexibility", "fixed"),
            parse_amount(row.get("minimum_allowed_amount")),
        ))
    return result


class RateTable:
    def __init__(self, rows: Iterable[Mapping[str, str]] = ()):
        self._rates: dict[tuple[date, str, str], float] = {}
        self._dates: set[date] = set()
        for row in rows:
            d, rate = parse_date(row.get("rate_date")), parse_amount(row.get("rate"))
            if d and rate and rate > 0:
                self._rates[(d, row["from_currency"], row["to_currency"])] = rate
                self._dates.add(d)

    def _direct(self, when: date, source: str, target: str) -> Optional[float]:
        if source == target:
            return 1.0
        candidates = [d for d in self._dates if d <= when]
        if not candidates:
            candidates = list(self._dates)
        for d in sorted(candidates, reverse=True):
            if (d, source, target) in self._rates:
                return self._rates[(d, source, target)]
        return None

    def convert(self, amount: float, source: str, target: str, when: date) -> float:
        if source == target:
            return amount
        direct = self._direct(when, source, target)
        if direct is not None:
            return amount * direct
        # Breadth-first path supports inverse and multi-hop conversions.
        currencies = {c for _, a, b in self._rates for c in (a, b)}
        queue = deque([(source, 1.0)])
        visited = {source}
        while queue:
            current, factor = queue.popleft()
            for nxt in sorted(currencies - visited):
                edge = self._direct(when, current, nxt)
                if edge is None:
                    reverse = self._direct(when, nxt, current)
                    edge = 1.0 / reverse if reverse else None
                if edge is not None:
                    if nxt == target:
                        return amount * factor * edge
                    visited.add(nxt)
                    queue.append((nxt, factor * edge))
        raise ValueError(f"No exchange-rate path from {source} to {target} on {when}")


def load_rates(path: Path) -> RateTable:
    return RateTable(_rows(path))


def load_options(path: Path) -> dict[str, list[PaymentOption]]:
    result: dict[str, list[PaymentOption]] = defaultdict(list)
    for row in _rows(path):
        first = parse_date(row.get("first_payment_date"))
        if not first:
            continue
        option = PaymentOption(
            row["payment_option_id"], row["request_id"], row["payment_method"],
            _float(row["payment_amount"]), int(_float(row["number_of_payments"], 1)), first,
            int(_float(row.get("payment_frequency_days"), 0)), _float(row.get("financing_fee")),
            _float(row.get("total_payable_amount")),
        )
        result[option.request_id].append(option)
    return result


def load_messages(path: Path) -> list[Message]:
    return [Message(row["message_id"], row["user_id"], row.get("request_id", ""),
                    row.get("related_event_id", ""), row.get("sent_at", ""),
                    row.get("source_type", ""), row.get("message_text", "")) for row in _rows(path)]


def load_images(path: Path) -> list[ImageLink]:
    return [ImageLink(row["image_id"], row["user_id"], row.get("request_id", ""),
                      row.get("related_event_id", "")) for row in _rows(path)]


def load_requests(path: Path) -> list[Request]:
    result = []
    for row in _rows(path):
        result.append(Request(
            row["request_id"], row["user_id"], parse_date(row["request_date"]),
            row["request_type"], _float(row["requested_amount"]),
            parse_date(row["desired_completion_date"]), row.get("allows_partial_payment", "").lower() == "true",
            row.get("request_text", ""),
        ))
    return result


def load_dataset(dataset_dir: Path, request_file: Optional[Path] = None) -> DataSet:
    """Load supporting files; requests are read only when ``request_file`` is supplied."""
    data = DataSet(
        profiles=load_profiles(dataset_dir / "financial_profiles.csv"),
        events=load_events(dataset_dir / "financial_events.csv"),
        rates=load_rates(dataset_dir / "exchange_rates.csv"),
        options=load_options(dataset_dir / "request_payment_options.csv"),
        messages=load_messages(dataset_dir / "messages.csv"),
        images=load_images(dataset_dir / "images.csv"),
        dataset_dir=dataset_dir,
        evidence_service=EvidenceService(dataset_dir),
    )
    if request_file is not None:
        data.requests = load_requests(request_file)
    return data


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _add_months(value: date, months: int = 1) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


class Forecast:
    """Daily cash-flow forecast with deterministic recurring-event projection."""

    def __init__(self, data: DataSet, request: Request, profile: Profile, changes: Sequence[str] = ()):
        self.data, self.request, self.profile = data, request, profile
        self.start = request.request_date
        self.end = self.start + timedelta(days=90)
        self.cash: dict[date, float] = defaultdict(float)
        self.labels: dict[date, list[Event]] = defaultdict(list)
        self._build(changes)

    def _amount(self, event: Event) -> Optional[float]:
        if event.amount is None:
            # A linked textual message can amend/clarify a blank structured amount.
            for message in self.data.messages:
                if message.related_event_id == event.event_id:
                    evidence = self._message_evidence(message)
                    if evidence.amounts:
                        return self.data.rates.convert(evidence.amounts[0], evidence.currencies[0],
                                                       self.profile.home_currency, event.settlement_date or self.start) if evidence.currencies and evidence.currencies[0] != self.profile.home_currency else evidence.amounts[0]
            # Images are considered only when the structured event amount is
            # blank and the image explicitly links to this event.
            service = self.data.evidence_service
            if service:
                for image in self.data.images:
                    if image.user_id == event.user_id and image.related_event_id == event.event_id:
                        extracted = service.image_for_event(image.image_id, event.event_id)
                        if extracted and extracted.amount is not None:
                            amount = extracted.amount
                            if extracted.currency and extracted.currency != self.profile.home_currency:
                                amount = self.data.rates.convert(
                                    amount, extracted.currency, self.profile.home_currency,
                                    event.settlement_date or self.start,
                                )
                            return amount
            return None
        when = event.settlement_date or event.event_date or self.start
        return self.data.rates.convert(event.amount, event.currency, self.profile.home_currency, when)

    def _message_evidence(self, message: Message) -> Evidence:
        deterministic = parse_evidence(message.message_text)
        service = self.data.evidence_service
        extracted = service.message(message.message_text, deterministic) if service else None
        if not extracted:
            return deterministic
        amounts = deterministic.amounts
        if not amounts and extracted.amount is not None:
            amounts = (extracted.amount,)
        dates = deterministic.dates
        if not dates and extracted.evidence_date:
            parsed = parse_date(extracted.evidence_date)
            dates = (parsed,) if parsed else ()
        currencies = deterministic.currencies
        if not currencies and extracted.currency:
            currencies = (extracted.currency,)
        facts = deterministic.facts | frozenset(extracted.facts)
        return Evidence(amounts, dates, currencies, facts)

    def _build(self, changes: Sequence[str]) -> None:
        by_id = {e.event_id: e for e in self.data.events if e.user_id == self.request.user_id}
        changed = set()
        reductions: dict[str, float] = {}
        for action in changes:
            parts = action.split(":")
            if len(parts) == 2 and parts[0] == "stop":
                changed.add(parts[1])
            elif len(parts) == 3 and parts[0] == "reduce_to":
                reductions[parts[1]] = max(0.0, _float(parts[2]))
        relevant = [e for e in by_id.values() if e.status not in {"cancelled", "canceled", "failed"}]
        structured_income: set[tuple[date, int]] = set()
        for event in relevant:
            amount = self._amount(event)
            when = event.settlement_date or event.event_date
            # The current balance already reflects settled history. Future
            # settled rows are historical leakage, not confirmed future cash;
            # only pending/scheduled rows are direct future commitments.
            if (amount is None or when is None or not (self.start <= when <= self.end)
                    or event.status == "settled"):
                continue
            if event.event_id in changed:
                continue
            if event.event_id in reductions:
                amount = min(amount, reductions[event.event_id])
            if event.direction == "debit":
                # Pending and scheduled debits are reserved; settled future debits are
                # also real commitments.  Pending credits are deliberately excluded.
                self.cash[when] -= amount
                self.labels[when].append(event)
            elif event.direction == "credit" and event.status == "scheduled":
                if event.category in {"salary", "income"} or event.event_type == "income":
                    self.cash[when] += amount
                    structured_income.add((when, round(amount)))
        # An employer message can explicitly amend a future confirmed salary
        # even when the corresponding structured row is absent. Unconfirmed
        # bonuses, commissions, and pending credits are intentionally ignored.
        for message in self.data.messages:
            if message.user_id != self.request.user_id or message.source_type not in {"employer", "payroll"}:
                continue
            lower = message.message_text.lower()
            if any(word in lower for word in ("bonus", "commission", "komisi", "bonusnya")):
                continue
            evidence = self._message_evidence(message)
            if not evidence.amounts or not evidence.dates:
                continue
            salary_language = any(word in lower for word in (
                "salary", "payroll", "pay", "gaji", "penggajian", "slip gaji",
                "salaris",
            ))
            if not (({"confirmed", "scheduled"} & evidence.facts) or salary_language):
                continue
            when = evidence.dates[-1]
            if not (self.start <= when <= self.end):
                continue
            amount = evidence.amounts[0]
            currency = evidence.currencies[0] if evidence.currencies else self.profile.home_currency
            if currency != self.profile.home_currency:
                amount = self.data.rates.convert(amount, currency, self.profile.home_currency, when)
            if (when, round(amount)) in structured_income:
                continue
            self.cash[when] += amount
        self._forecast_recurrences(relevant, changed, reductions)

    def _forecast_recurrences(self, events: list[Event], changed: set[str], reductions: Mapping[str, float]) -> None:
        groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
        for event in events:
            event_when = event.settlement_date or event.event_date
            if (event.direction == "debit" and event.status == "settled"
                    and event.amount is not None and event_when and event_when < self.start):
                groups[(event.category, _norm(event.description))].append(event)
            elif (event.direction == "credit" and event.category in {"salary", "income"}
                  and event.status == "settled" and event.amount is not None
                  and event_when and event_when < self.start):
                groups[("credit:" + event.category, _norm(event.description))].append(event)
        occupied = {(e.event_id, e.settlement_date or e.event_date) for e in events}
        for group in groups.values():
            dates = sorted(e.settlement_date or e.event_date for e in group if e.settlement_date or e.event_date)
            if len(dates) < 2:
                continue
            gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if 5 <= (b - a).days <= 45]
            # A single interval is not enough evidence for a recurring debit;
            # irregular grocery/transport histories otherwise become repeated
            # every few days. Salary is allowed with one interval because a
            # confirmed upcoming credit plus one prior salary is explicit.
            is_credit = group[0].direction == "credit"
            if len(gaps) < 1:
                continue
            interval = int(round(statistics.median(gaps)))
            if interval < 5:
                continue
            if not is_credit and (len(gaps) < 3 or max(gaps) > interval * 1.5 or min(gaps) < interval * 0.5):
                continue
            latest = dates[-1]
            template = group[-1]
            amount = self._amount(template)
            if amount is None:
                continue
            monthly = (
                26 <= interval <= 35
                and max(abs(item.day - latest.day) for item in dates) <= 2
            )
            next_date = _add_months(latest) if monthly else latest + timedelta(days=interval)
            while next_date <= self.end:
                if next_date >= self.start and not any(d == next_date for _, d in occupied):
                    if template.event_id not in changed:
                        adjusted = min(amount, reductions.get(template.event_id, amount))
                        if template.direction == "credit":
                            self.cash[next_date] += adjusted
                        else:
                            self.cash[next_date] -= adjusted
                            self.labels[next_date].append(template)
                next_date = _add_months(next_date) if monthly else next_date + timedelta(days=interval)

    def balance(self, at: date, payments: Sequence[tuple[date, float]] = ()) -> float:
        balance = self.profile.balance
        payments_by_date: dict[date, float] = defaultdict(float)
        for d, amount in payments:
            payments_by_date[d] += amount
        for d in sorted(set(self.cash) | set(payments_by_date)):
            if d > at:
                break
            balance += self.cash[d] - payments_by_date[d]
        return balance

    def safe(self, payments: Sequence[tuple[date, float]]) -> bool:
        if any(d < self.start or d > self.end for d, _ in payments):
            return False
        balance = self.profile.balance
        by_payment: dict[date, float] = defaultdict(float)
        for d, amount in payments:
            by_payment[d] += amount
        for d in sorted(set(self.cash) | set(by_payment)):
            balance += self.cash[d] - by_payment[d]
            if balance < self.profile.minimum - 1e-6:
                return False
        return True

    def safe_single_dates(self, amount: float, latest: date) -> list[date]:
        dates = []
        d = self.start
        while d <= min(latest, self.end):
            if self.safe([(d, amount)]):
                dates.append(d)
            d += timedelta(days=1)
        return dates


def allowed_changes(data: DataSet, request: Request, profile: Profile) -> list[str]:
    """Return only profile-authorized, flexible future events."""
    candidates = []
    for event in data.events:
        if event.user_id != request.user_id or event.direction != "debit":
            continue
        if event.category in profile.protected or event.category not in profile.reducible | profile.stoppable:
            continue
        if event.status in {"cancelled", "canceled", "failed"} or (event.settlement_date or event.event_date or request.request_date) < request.request_date:
            continue
        if event.flexibility in {"stoppable", "stop"} and event.category in profile.stoppable:
            candidates.append((0, f"stop:{event.event_id}"))
        elif event.flexibility in {"reducible", "stoppable", "reduce"} and event.category in profile.reducible:
            current = event.amount or 0.0
            minimum = event.minimum_allowed_amount if event.minimum_allowed_amount is not None else current / 2
            candidates.append((1, f"reduce_to:{event.event_id}:{minimum:g}"))
    return [value for _, value in sorted(candidates)[:3]]


def _fmt_amount(value: float) -> str:
    rounded = round(value, 2)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:.2f}"


def format_plan(plan: Sequence[tuple[date, float]]) -> str:
    return "|".join(f"{d.isoformat()}:{_fmt_amount(amount)}" for d, amount in plan) if plan else "none"


@dataclass
class Decision:
    request_id: str
    amount_safe_to_pay: float
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: str
    spending_changes_needed: str
    decision_explanation: str

    def row(self) -> dict[str, str]:
        row = {name: str(getattr(self, name)) for name in OUTPUT_COLUMNS}
        row["amount_safe_to_pay"] = _fmt_amount(self.amount_safe_to_pay)
        return row


def _option_feasible(option: PaymentOption, request: Request, profile: Profile) -> bool:
    if option.payment_method != "installments" or "installments" not in profile.methods:
        return False
    if profile.max_installment_months is not None and option.number_of_payments > profile.max_installment_months:
        return False
    schedule = option.schedule()
    return bool(schedule) and schedule[-1][0] <= request.desired_completion_date


def decide(data: DataSet, request: Request) -> Decision:
    profile = data.profiles[request.user_id]
    no_change = Forecast(data, request, profile)

    # Find the largest request-date payment that keeps every projected balance
    # above the user's minimum. Safety is monotone in the payment amount.
    low, high = 0.0, request.requested_amount
    for _ in range(60):
        mid = (low + high) / 2
        if no_change.safe([(request.request_date, mid)]):
            low = mid
        else:
            high = mid
    safe_today = min(request.requested_amount, low)
    options = data.options.get(request.request_id, [])
    candidates: list[tuple[tuple, str, list[tuple[date, float]], Sequence[str]]] = []

    def add(method: str, plan: list[tuple[date, float]], changes: Sequence[str], status: str, cost: float = 0.0) -> None:
        if not plan or not Forecast(data, request, profile, changes).safe(plan):
            return
        first = plan[0][0]
        rank = {"affordable_now": 0, "affordable_with_plan": 1, "affordable_later": 2}[status]
        # Completing by the deadline, no changes, lower cost, earlier start, fewer payments.
        candidates.append(((rank, 0 if first <= request.desired_completion_date else 1,
                            len(changes), cost, first, len(plan)), method, plan, changes))

    full_plan = [(request.request_date, request.requested_amount)]
    if "full_payment" in profile.methods or not profile.methods:
        if no_change.safe(full_plan):
            add("full_payment", full_plan, (), "affordable_now")
        changes = allowed_changes(data, request, profile)
        for size in range(1, len(changes) + 1):
            # Combinations are small (at most three) and allow multiple
            # independent flexible commitments to be adjusted together.
            for chosen in itertools.combinations(changes, size):
                if Forecast(data, request, profile, chosen).safe(full_plan):
                    add("full_payment", full_plan, chosen, "affordable_with_plan")

    for option in options:
        if _option_feasible(option, request, profile):
            plan = option.schedule()
            if sum(a for _, a in plan) + 1e-6 >= request.requested_amount and Forecast(data, request, profile).safe(plan):
                add("installments", plan, (), "affordable_with_plan", option.total_payable_amount)

    if request.allows_partial_payment and "partial_payment" in profile.methods and 0 < safe_today < request.requested_amount:
        remaining = request.requested_amount - safe_today
        for d in no_change.safe_single_dates(remaining, request.desired_completion_date):
            if d >= request.request_date:
                add("partial_payment", [(request.request_date, safe_today), (d, remaining)], (), "affordable_with_plan")
                break

    earliest = no_change.safe_single_dates(request.requested_amount, request.request_date + timedelta(days=90))
    if earliest:
        d = earliest[0]
        if d > request.request_date:
            add("wait", [(d, request.requested_amount)], (), "affordable_later")

    if candidates:
        _, method, plan, changes = min(candidates, key=lambda item: item[0])
        status = "affordable_now" if method == "full_payment" and plan[0][0] == request.request_date and not changes else "affordable_with_plan" if method in {"full_payment", "partial_payment", "installments"} else "affordable_later"
        first_full = next((d for d, amount in plan if abs(amount - request.requested_amount) < 1e-6), plan[-1][0])
        explanation = f"{method.replace('_', ' ')} keeps at least {profile.home_currency} {_fmt_amount(profile.minimum)} available."
        return Decision(request.request_id, round(min(safe_today, request.requested_amount), 2), status, method,
                        format_plan(plan), first_full.isoformat(), "|".join(changes) or "none", explanation)
    return Decision(
        request.request_id, round(min(safe_today, request.requested_amount), 2), "not_affordable", "not_recommended",
        "none", "", "none",
        f"Do not proceed: no available plan keeps the {profile.home_currency} { _fmt_amount(profile.minimum) } minimum protected.",
    )


def validate_decision(
    decision: Decision,
    request: Request,
    options: Sequence[PaymentOption] = (),
    data: Optional[DataSet] = None,
) -> Decision:
    """Final approval gate; invalid candidate decisions become a safe refusal."""
    valid = (
        decision.affordability_status in STATUSES and decision.recommended_payment_method in METHODS
        and 0 - 1e-6 <= decision.amount_safe_to_pay <= request.requested_amount + 1e-6
    )
    if valid and decision.payment_plan != "none":
        parsed = []
        for part in decision.payment_plan.split("|"):
            try:
                d, amount = part.split(":", 1)
                parsed.append((date.fromisoformat(d), float(amount)))
            except (ValueError, TypeError):
                valid = False
                break
        if parsed and any(parsed[i][0] > parsed[i + 1][0] for i in range(len(parsed) - 1)):
            valid = False
        if parsed and any(d < request.request_date or d > request.desired_completion_date
                          for d, _ in parsed):
            valid = False
        if parsed and any(amount < 0 for _, amount in parsed):
            valid = False
        if decision.recommended_payment_method == "partial_payment":
            valid = (
                valid and len(parsed) == 2 and parsed[0][0] == request.request_date
                and 0 < parsed[0][1] < request.requested_amount
                and abs(sum(a for _, a in parsed) - request.requested_amount) < 0.01
                and parsed[1][0] <= request.desired_completion_date
            )
        if decision.recommended_payment_method == "installments":
            valid = valid and any(
                option.schedule() == parsed for option in options if option.payment_method == "installments"
            )
        if decision.recommended_payment_method == "full_payment":
            valid = valid and len(parsed) == 1 and abs(parsed[0][1] - request.requested_amount) < 0.01
        if decision.recommended_payment_method == "wait":
            valid = valid and len(parsed) == 1 and abs(parsed[0][1] - request.requested_amount) < 0.01
    elif decision.recommended_payment_method in {"full_payment", "partial_payment", "installments", "wait"}:
        valid = False
    if decision.recommended_payment_method == "not_recommended":
        valid = valid and decision.payment_plan == "none"
    if valid and data is not None and decision.payment_plan != "none":
        parsed_plan = []
        for part in decision.payment_plan.split("|"):
            plan_date, plan_amount = part.split(":", 1)
            parsed_plan.append((date.fromisoformat(plan_date), float(plan_amount)))
        valid = Forecast(data, request, data.profiles[request.user_id]).safe(parsed_plan)
    if valid and decision.spending_changes_needed != "none" and data is not None:
        profile = data.profiles.get(request.user_id)
        events = {event.event_id: event for event in data.events if event.user_id == request.user_id}
        actions = decision.spending_changes_needed.split("|")
        valid = bool(profile) and len(actions) <= 3
        seen_events: set[str] = set()
        for action in actions:
            parts = action.split(":")
            event = events.get(parts[1]) if len(parts) > 1 else None
            if event is None or event.category in profile.protected:
                valid = False
                break
            if event.event_id in seen_events:
                valid = False
                break
            seen_events.add(event.event_id)
            if parts[0] == "stop":
                valid = valid and event.category in profile.stoppable and event.flexibility in {"stoppable", "stop"}
            elif parts[0] == "reduce_to" and len(parts) == 3:
                new_amount = parse_amount(parts[2])
                minimum = event.minimum_allowed_amount if event.minimum_allowed_amount is not None else (event.amount or 0) / 2
                valid = valid and event.category in profile.reducible and event.flexibility in {"reducible", "reduce", "stoppable"} and new_amount is not None and minimum - 1e-6 <= new_amount <= (event.amount or 0) + 1e-6
            else:
                valid = False
    if valid:
        return decision
    return Decision(request.request_id, max(0.0, min(request.requested_amount, decision.amount_safe_to_pay)),
                    "not_affordable", "not_recommended", "none", "", "none",
                    "Do not proceed: the proposed plan failed deterministic safety validation.")


def write_output(path: Path, decisions: Sequence[Decision]) -> None:
    if any(set(decision.row()) != set(OUTPUT_COLUMNS) for decision in decisions):
        raise ValueError("Decision schema does not match required output columns")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(d.row() for d in decisions)


def run(data: DataSet) -> list[Decision]:
    return [validate_decision(decide(data, request), request, data.options.get(request.request_id, ()), data) for request in data.requests]
