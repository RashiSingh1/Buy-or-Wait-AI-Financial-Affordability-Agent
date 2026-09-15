"""Optional, conservative Gemini evidence extraction.

This module never makes a network request unless ``GEMINI_ENABLE=1`` and a
``GEMINI_API_KEY`` are present.  It extracts factual fields only; affordability
and payment recommendations remain the responsibility of :mod:`code.agent`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional


_TRANSIENT = {408, 500, 502, 503, 504}
_RATE_LIMIT = {429}
_FACT_KEYS = ("facts", "statuses", "status", "transaction_status")
_ALLOWED_CURRENCIES = {"IDR", "INR", "ZAR", "USD", "EUR", "GBP"}
_ALLOWED_FACTS = {
    "confirmed", "scheduled", "pending", "settled", "cancelled", "canceled",
    "reversed", "unrealized", "not withdrawable", "tidak disetujui",
    "belum disetujui", "sudah dikonfirmasi", "dikonfirmasi", "menunggu",
}


def load_dotenv(path: Optional[Path] = None) -> None:
    """Load simple ``KEY=VALUE`` entries without ever printing their values."""
    candidate = path or Path.cwd() / ".env"
    if not candidate.is_file():
        return
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[^\d,.\-+kKmM]", "", text)
    multiplier = 1.0
    if text[-1:].lower() == "k":
        multiplier, text = 1000.0, text[:-1]
    elif text[-1:].lower() == "m":
        multiplier, text = 1_000_000.0, text[:-1]
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        parts = text.split(",")
        text = "".join(parts) if len(parts[-1]) == 3 else text.replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    try:
        return float(text) * multiplier
    except ValueError:
        return None


def _integer(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _date(value: Any) -> Optional[str]:
    if value is None:
        return None
    match = re.search(r"\b20\d{2}-\d{1,2}-\d{1,2}\b", str(value))
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(0)).isoformat()
    except ValueError:
        return None


@dataclass(frozen=True)
class ExtractedEvidence:
    """Factual evidence fields accepted from a model response."""

    amount: Optional[float] = None
    evidence_date: Optional[str] = None
    currency: Optional[str] = None
    facts: tuple[str, ...] = ()
    confidence: Optional[float] = None
    source: str = "deterministic"

    @property
    def usable(self) -> bool:
        return self.amount is not None or self.evidence_date is not None or bool(self.facts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount": self.amount,
            "date": self.evidence_date,
            "currency": self.currency,
            "facts": list(self.facts),
            "confidence": self.confidence,
            "source": self.source,
        }


def _json_object(text: str) -> Optional[Mapping[str, Any]]:
    """Parse an object from plain JSON or a fenced/embedded JSON response."""
    if not isinstance(text, str):
        return None
    candidates = [text.strip()]
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    for candidate in candidates:
        try:
            value = json.loads(candidate)
            if isinstance(value, Mapping):
                return value
        except (TypeError, ValueError):
            pass
    # Be tolerant of a short explanation around a JSON object, but never
    # interpret arbitrary prose as evidence.
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, Mapping) else None
        except (TypeError, ValueError):
            return None
    return None


def parse_structured_evidence(payload: Any, source: str = "gemini") -> Optional[ExtractedEvidence]:
    """Parse a Gemini response and accept only the factual JSON schema."""
    if isinstance(payload, Mapping):
        if "candidates" in payload:
            parts = payload.get("candidates") or []
            text_parts = []
            if parts and isinstance(parts[0], Mapping):
                content = parts[0].get("content", {})
                for part in content.get("parts", []) if isinstance(content, Mapping) else []:
                    if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
            obj = _json_object("".join(text_parts))
        else:
            obj = payload
    elif isinstance(payload, str):
        obj = _json_object(payload)
    else:
        obj = None
    if not obj:
        return None
    amount = _number(obj.get("amount", obj.get("amount_value", obj.get("value"))))
    if amount is not None and amount < 0:
        amount = None
    evidence_date = _date(obj.get("date", obj.get("event_date", obj.get("settlement_date"))))
    currency = str(obj.get("currency", "")).upper().strip() or None
    if currency not in _ALLOWED_CURRENCIES:
        currency = None
    raw_facts: list[Any] = []
    for key in _FACT_KEYS:
        value = obj.get(key)
        if isinstance(value, (list, tuple)):
            raw_facts.extend(value)
        elif value:
            raw_facts.append(value)
    facts = tuple(dict.fromkeys(
        item for item in (str(value).strip().lower() for value in raw_facts)
        if item in _ALLOWED_FACTS
    ))
    confidence = _number(obj.get("confidence"))
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    result = ExtractedEvidence(amount, evidence_date, currency, facts, confidence, source)
    return result if result.usable else None


@dataclass
class UsageTracker:
    """Small persisted usage summary with no request text or secrets."""

    calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: int = 0
    estimated_cost_usd: float = 0.0
    error_types: dict[str, int] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def record_error(self, kind: str) -> None:
        self.errors += 1
        self.error_types[kind] = self.error_types.get(kind, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "errors": self.errors,
            "error_types": dict(self.error_types),
            "estimated_cost_usd": round(self.estimated_cost_usd, 8),
        }


class GeminiClient:
    """Minimal Gemini REST client with cache, retry, and usage accounting."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        enabled: Optional[bool] = None,
        cache_dir: Optional[Path] = None,
        model: str = "gemini-2.0-flash",
        transport: Optional[Callable[[str, bytes, Mapping[str, str]], tuple[int, bytes]]] = None,
    ):
        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
        load_dotenv()
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.enabled = (os.environ.get("GEMINI_ENABLE") == "1") if enabled is None else enabled
        self.enabled = bool(self.enabled and self.api_key)
        self.model = model
        self.cache_dir = (
            Path(cache_dir) if cache_dir is not None
            else Path(__file__).resolve().parents[1] / "runtime" / "evidence_cache"
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.usage = UsageTracker()
        self.usage_file = self.cache_dir.parent / "evidence_usage.json"
        self._transport = transport

    def _persist_usage(self) -> None:
        try:
            self.usage_file.write_text(json.dumps(self.usage.as_dict(), indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _key(self, prompt: str, image: Optional[bytes]) -> str:
        digest = hashlib.sha256()
        digest.update(prompt.encode("utf-8"))
        if image:
            digest.update(image)
        return digest.hexdigest()

    def _send(self, body: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        if self._transport:
            return self._transport(self.model, body, headers)
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read()

    def generate(self, prompt: str, image: Optional[bytes] = None,
                 mime_type: str = "image/png") -> Optional[ExtractedEvidence]:
        if not self.enabled:
            return None
        key = self._key(prompt, image)
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                result = parse_structured_evidence(cached, source="gemini-cache")
                if result:
                    self.usage.cache_hits += 1
                    self._persist_usage()
                    return result
            except (OSError, ValueError, TypeError):
                pass
        self.usage.cache_misses += 1
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if image:
            parts.append({"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image).decode("ascii")}})
        body = json.dumps({"contents": [{"parts": parts}]}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        attempts = 0
        while attempts < 2:
            attempts += 1
            self.usage.calls += 1
            try:
                status, raw = self._send(body, headers)
            except (OSError, urllib.error.URLError, ValueError):
                if attempts == 1:
                    time.sleep(0.05)
                    continue
                self.usage.record_error("transport")
                self._persist_usage()
                break
            if status == 200:
                try:
                    response = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self.usage.record_error("malformed_response")
                    self._persist_usage()
                    break
                metadata = response.get("usageMetadata", {}) if isinstance(response, Mapping) else {}
                if not isinstance(metadata, Mapping):
                    metadata = {}
                self.usage.input_tokens += _integer(metadata.get("promptTokenCount", 0))
                self.usage.output_tokens += _integer(metadata.get("candidatesTokenCount", 0))
                result = parse_structured_evidence(response)
                if not result:
                    self.usage.record_error("malformed_response")
                    self._persist_usage()
                    break
                try:
                    cache_file.write_text(json.dumps(result.as_dict()), encoding="utf-8")
                except OSError:
                    pass
                self._persist_usage()
                return result
            if status in _TRANSIENT and attempts == 1:
                time.sleep(0.05)
                continue
            self.usage.record_error("rate_limit" if status in _RATE_LIMIT else f"http_{status}")
            self._persist_usage()
            break
        self._persist_usage()
        return None


class EvidenceService:
    """Policy layer deciding when optional model extraction may be attempted."""

    def __init__(self, dataset_dir: Optional[Path] = None, client: Optional[GeminiClient] = None):
        self.dataset_dir = Path(dataset_dir) if dataset_dir else None
        self.client = client or GeminiClient()

    @staticmethod
    def deterministic_insufficient(evidence: Any) -> bool:
        return not getattr(evidence, "amounts", ()) or not getattr(evidence, "dates", ())

    def message(self, text: str, deterministic: Any) -> Optional[ExtractedEvidence]:
        if not self.deterministic_insufficient(deterministic):
            return None
        return self.client.generate(
            "Extract factual financial evidence from this ambiguous message. "
            "Return JSON only with amount, date (YYYY-MM-DD), currency, facts, confidence. "
            "Never return an affordability or payment recommendation.\n" + (text or "")
        )

    def image_for_event(self, image_id: str, related_event_id: str) -> Optional[ExtractedEvidence]:
        if not related_event_id or not self.dataset_dir:
            return None
        image_path = self.dataset_dir / "media" / "images" / f"{image_id}.png"
        try:
            image = image_path.read_bytes()
        except OSError:
            return None
        mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
        return self.client.generate(
            "Read this image only to extract factual amount, date, currency, and transaction "
            "status for the linked financial event. Return JSON only; do not recommend a decision.",
            image=image, mime_type=mime,
        )


GeminiEvidenceExtractor = EvidenceService
