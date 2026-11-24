"""Provider-agnostic structured-output LLM: Gemini or Groq.

Groq's free tier has much tighter limits than Gemini's (confirmed by the
user: 30 RPM, 8K TPM, 200K TPD, 1K RPD *per model*), so the Groq path adds
request pacing and a same-day fallback to a second model. Both facts
(real model IDs, `reasoning_effort` field, real per-call token usage via
`include_raw=True`) were confirmed live against the actual Groq API/SDK on
2026-11-23, not assumed from docs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

import tiktoken
from pydantic import BaseModel

from research_agent.retry import call_with_backoff

logger = logging.getLogger(__name__)

GROQ_USAGE_PATH = Path("output/groq_usage.json")
GEMINI_USAGE_PATH = Path("output/gemini_usage.json")

GROQ_LIMITS = {"rpm": 30, "tpm": 8000, "tpd": 200000, "rpd": 1000}

# Gemini's real free-tier limits are 15 RPM / 250K TPM / 500 RPD for
# gemini-3.5-flash-lite. We pace ourselves to a safety margin below the
# real RPM/TPM ceilings instead of running right up against them, since a
# live 429 mid-run still costs the 60s wait-and-retry below.
GEMINI_RPM_LIMIT = 15
GEMINI_SAFE_RPM = 13
GEMINI_TPM_LIMIT = 250_000
GEMINI_SAFE_TPM = 220_000
GEMINI_DAILY_DISPLAY_LIMIT = 500
GEMINI_429_MAX_RETRIES = 3
GEMINI_429_WAIT_SECONDS = 60

# Proactively switch to the fallback model before actually hitting the real
# 200K/day wall (user-specified safety margin), rather than waiting for a
# live 429 from Groq every time.
GROQ_DAILY_SOFT_LIMIT = 190_000

_ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


def _messages_to_text(messages: Any) -> str:
    if isinstance(messages, str):
        return messages
    parts = []
    for item in messages:
        if isinstance(item, tuple):
            parts.append(item[1])
        else:
            parts.append(str(item))
    return "\n".join(parts)


class AllProvidersExhausted(Exception):
    pass


class StructuredOutputEmpty(Exception):
    """Raised when the model call succeeded but parsing produced None."""


class GeminiQuotaExhausted(Exception):
    """Raised after 3 failed wait-and-retry attempts on a Gemini 429. The
    run should stop cleanly here, not fall back to a different model -
    Gemini has no second model to fall back to, and retrying harder just
    burns more of the daily quota."""


def _is_gemini_quota_error(exc: Exception) -> bool:
    text = str(exc)
    return "429" in text and ("RESOURCE_EXHAUSTED" in text or "quota" in text.lower())


# Gemini's free-tier daily quota resets on Google's clock, which is
# Pacific time, not whatever timezone this machine happens to be in - using
# local time would reset the counter at the wrong moment (e.g. hours early
# or late) relative to when the real quota actually refills.
GEMINI_QUOTA_RESET_TZ = ZoneInfo("America/Los_Angeles")


def _gemini_quota_date() -> str:
    return datetime.now(GEMINI_QUOTA_RESET_TZ).date().isoformat()


@dataclass
class _GeminiDailyUsage:
    date: str = field(default_factory=_gemini_quota_date)
    requests: int = 0


class GeminiUsageTracker:
    """Persists a daily request count for Gemini so '<N> / 500' survives
    process restarts, same resumability reasoning as GroqUsageTracker."""

    def __init__(self, path: Path = GEMINI_USAGE_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._usage = _GeminiDailyUsage()
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self._usage = _GeminiDailyUsage(**raw)
        self._reset_if_new_day()

    def _reset_if_new_day(self) -> None:
        today = _gemini_quota_date()
        if self._usage.date != today:
            self._usage = _GeminiDailyUsage(date=today)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self._usage), indent=2))
        os.replace(tmp, self.path)

    def record(self) -> int:
        with self._lock:
            self._reset_if_new_day()
            self._usage.requests += 1
            self._save()
            return self._usage.requests

    def requests_today(self) -> int:
        with self._lock:
            self._reset_if_new_day()
            return self._usage.requests


class GeminiExtractionLLM:
    provider = "gemini"

    def __init__(self, config) -> None:
        from langchain_google_genai import ChatGoogleGenerativeAI

        self._model = ChatGoogleGenerativeAI(model=config.gemini_model, google_api_key=config.google_api_key)
        self.model_name = config.gemini_model
        self._rate_limiter = MinuteRateLimiter(GEMINI_SAFE_RPM, GEMINI_SAFE_TPM, label="Gemini")
        self._usage = GeminiUsageTracker()

    @property
    def requests_today(self) -> int:
        return self._usage.requests_today()

    def invoke_structured(self, schema: type[BaseModel], messages: Any) -> tuple[BaseModel, str, str]:
        text = _messages_to_text(messages)
        estimated_tokens = len(text) // 4

        structured = self._model.with_structured_output(schema, method="json_schema")

        retries_used = 0
        while True:
            self._rate_limiter.acquire(estimated_tokens)
            try:
                # Not call_with_backoff: that retries any 429-shaped error 5x
                # with its own exponential backoff *before* this loop ever
                # sees the exception, which would turn "retry up to 3
                # times, 60s apart" into dozens of extra calls against an
                # already-exhausted quota. We want the flat 60s wait
                # specified here, nothing stacked on top of it.
                result = structured.invoke(messages)
            except Exception as exc:
                if not _is_gemini_quota_error(exc):
                    raise
                if retries_used >= GEMINI_429_MAX_RETRIES:
                    raise GeminiQuotaExhausted(str(exc)) from exc
                retries_used += 1
                logger.warning(
                    "Gemini 429 (retry %d/%d), waiting %ds before retrying",
                    retries_used, GEMINI_429_MAX_RETRIES, GEMINI_429_WAIT_SECONDS,
                )
                time.sleep(GEMINI_429_WAIT_SECONDS)
                continue
            else:
                self._usage.record()
                return result, self.provider, self.model_name


@dataclass
class _ModelUsage:
    date: str = field(default_factory=lambda: date.today().isoformat())
    tokens: int = 0
    requests: int = 0


class GroqUsageTracker:
    """Persists daily token/request counts per Groq model so the daily cap
    survives process restarts (PRD §6's resumability rule applies here too)."""

    def __init__(self, path: Path = GROQ_USAGE_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, _ModelUsage] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            for model, d in raw.items():
                self._data[model] = _ModelUsage(**d)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({m: asdict(u) for m, u in self._data.items()}, indent=2))
        os.replace(tmp, self.path)

    def _get(self, model: str) -> _ModelUsage:
        today = date.today().isoformat()
        usage = self._data.get(model)
        if usage is None or usage.date != today:
            usage = _ModelUsage(date=today)
            self._data[model] = usage
        return usage

    def can_use(self, model: str, estimated_tokens: int) -> bool:
        with self._lock:
            usage = self._get(model)
            if usage.requests + 1 > GROQ_LIMITS["rpd"]:
                return False
            if usage.tokens + estimated_tokens > GROQ_DAILY_SOFT_LIMIT:
                return False
            return True

    def tokens_used(self, model: str) -> int:
        with self._lock:
            return self._get(model).tokens

    def record(self, model: str, tokens_used: int) -> None:
        with self._lock:
            usage = self._get(model)
            usage.tokens += tokens_used
            usage.requests += 1
            self._save()


class MinuteRateLimiter:
    """Paces calls to stay under a model's RPM and TPM (rolling 60s window)."""

    def __init__(self, rpm: int, tpm: int, label: str = "pacing") -> None:
        self.rpm = rpm
        self.tpm = tpm
        self.label = label
        self._lock = threading.Lock()
        self._requests: deque[float] = deque()
        self._tokens: deque[tuple[float, int]] = deque()

    def acquire(self, estimated_tokens: int) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._requests and now - self._requests[0] > 60:
                    self._requests.popleft()
                while self._tokens and now - self._tokens[0][0] > 60:
                    self._tokens.popleft()
                tokens_in_window = sum(t for _, t in self._tokens)
                if len(self._requests) < self.rpm and tokens_in_window + estimated_tokens <= self.tpm:
                    self._requests.append(now)
                    self._tokens.append((now, estimated_tokens))
                    return
                waits = []
                if len(self._requests) >= self.rpm and self._requests:
                    waits.append(60 - (now - self._requests[0]))
                if tokens_in_window + estimated_tokens > self.tpm and self._tokens:
                    waits.append(60 - (now - self._tokens[0][0]))
                wait_s = max(0.2, min(waits) if waits else 1.0)
            logger.info("%s pacing: sleeping %.1fs to stay under RPM/TPM", self.label, wait_s)
            time.sleep(wait_s)


_REQUESTED_TOKENS_RE = re.compile(r"requested[\s:]+(\d+)", re.IGNORECASE)

# Groq doesn't let us set max_tokens client-side for json_schema structured
# output (confirmed live: the actual HTTP request body has no max_tokens/
# max_completion_tokens field) - it computes an output-token budget
# server-side per schema, which only surfaces in a 429's "Requested N" text.
# Bootstrap with the worst value observed live (2026-11-23) until we learn
# a real one from an actual error.
_DEFAULT_OTPM_ESTIMATE = 2048


class OutputTokenPacer:
    """Paces calls to average under a tight OUTPUT-tokens-per-minute target.

    Separate from MinuteRateLimiter's TPM (input+output) check: found live
    that qwen/qwen3.8-27b enforces its own much smaller OTPM dimension
    (observed limit 1000/min) on top of the general 8K TPM. A single
    structured-output call's server-computed output budget (~2048 tokens,
    observed live) already exceeds that 1000/min limit on its own, so a
    sliding-window "wait until there's room" admission check can never be
    satisfied - there both never is enough room. Model this as a minimum
    fixed interval between calls instead (spend the token budget over time,
    e.g. ~2048 tokens at a 900/min target -> ~1 call every ~137s), matching
    "one call about every 2 minutes" from the target rate.
    """

    def __init__(self, target_tokens_per_min: int) -> None:
        self.target = target_tokens_per_min
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self, estimated_output_tokens: int) -> None:
        interval = max(1.0, (estimated_output_tokens / self.target) * 60.0)
        with self._lock:
            now = time.monotonic()
            wait_s = max(0.0, self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + interval
        if wait_s > 0:
            logger.info(
                "Groq OTPM pacing: sleeping %.1fs before next qwen call (targeting ~%d output tokens/min)",
                wait_s, self.target,
            )
            time.sleep(wait_s)


def _looks_like_daily_quota(exc: Exception) -> bool:
    """Distinguish a real daily-quota exhaustion from a per-minute throttle.

    Found live (2026-11-23): Groq's per-minute OTPM rate-limit error text
    contains "Upgrade to Dev Tier today", and a bare "day" in text substring
    check matches that "today", misclassifying a transient per-minute
    throttle (which retries fine) as a daily exhaustion and stopping the
    whole run. Require an actual day/RPD/TPD marker, not just "day".
    """
    text = str(exc).lower()
    if not ("429" in text or "rate_limit" in text or "rate limit" in text):
        return False
    daily_markers = ("per day", "requests per day", "tokens per day", "daily limit", " rpd", " tpd", "'rpd'", "'tpd'")
    return any(marker in text for marker in daily_markers)


class GroqExtractionLLM:
    provider = "groq"

    def __init__(self, config) -> None:
        self._config = config
        self._primary_name = config.groq_model
        self._fallback_name = config.groq_fallback_model
        self.model_name = self._primary_name
        self._usage = GroqUsageTracker()
        # Each model has its own independent RPM/TPM bucket on Groq's side,
        # so each needs its own limiter instance - a shared one would
        # throttle the fallback model based on the primary's usage.
        self._limiters: dict[str, MinuteRateLimiter] = {}
        self._models: dict[str, Any] = {}
        # Output-token pacing is scoped to the fallback model specifically
        # (per user instruction) - the primary hasn't shown this OTPM issue.
        self._otpm_pacer = OutputTokenPacer(target_tokens_per_min=900)
        self._otpm_estimate = _DEFAULT_OTPM_ESTIMATE

    def _get_limiter(self, name: str) -> "MinuteRateLimiter":
        if name not in self._limiters:
            self._limiters[name] = MinuteRateLimiter(GROQ_LIMITS["rpm"], GROQ_LIMITS["tpm"], label="Groq")
        return self._limiters[name]

    def _get_model(self, name: str):
        if name not in self._models:
            from langchain_groq import ChatGroq

            kwargs: dict[str, Any] = {"model": name, "api_key": self._config.groq_api_key, "temperature": 0}
            if "gpt-oss" in name:
                kwargs["reasoning_effort"] = "low"
            self._models[name] = ChatGroq(**kwargs)
        return self._models[name]

    def invoke_structured(self, schema: type[BaseModel], messages: Any) -> tuple[BaseModel, str, str]:
        text = _messages_to_text(messages)
        estimated_tokens = count_tokens(text) + 800  # headroom for output/reasoning tokens

        last_exc: Exception | None = None
        for name in (self._primary_name, self._fallback_name):
            if not self._usage.can_use(name, estimated_tokens):
                logger.warning(
                    "Groq daily soft limit for %s reached (%d/%d tokens used today), switching to next model",
                    name, self._usage.tokens_used(name), GROQ_DAILY_SOFT_LIMIT,
                )
                continue

            self._get_limiter(name).acquire(estimated_tokens)
            if name == self._fallback_name:
                self._otpm_pacer.acquire(self._otpm_estimate)

            model = self._get_model(name)
            structured = model.with_structured_output(schema, method="json_schema", include_raw=True)
            try:
                raw_result = call_with_backoff(structured.invoke, messages)
            except Exception as exc:
                last_exc = exc
                if name == self._fallback_name:
                    match = _REQUESTED_TOKENS_RE.search(str(exc))
                    if match:
                        observed = int(match.group(1))
                        if observed > self._otpm_estimate:
                            logger.info(
                                "Learned a larger real OTPM request size for %s: %d (was %d)",
                                name, observed, self._otpm_estimate,
                            )
                            self._otpm_estimate = observed
                if _looks_like_daily_quota(exc):
                    logger.warning("Groq daily quota hit for %s mid-call, trying next model", name)
                    continue
                raise

            raw_msg = raw_result["raw"]
            usage = getattr(raw_msg, "usage_metadata", None) or {}
            actual_tokens = usage.get("total_tokens", estimated_tokens)
            self._usage.record(name, actual_tokens)
            self.model_name = name

            parsed = raw_result["parsed"]
            if parsed is None:
                logger.warning(
                    "Groq structured output parsed as None for model=%s (parsing_error=%s). Raw content: %.800s",
                    name, raw_result.get("parsing_error"), getattr(raw_msg, "content", ""),
                )
                raise StructuredOutputEmpty(f"Structured output parsed as None for model {name}")

            return parsed, self.provider, name

        raise AllProvidersExhausted(
            f"Both Groq models ({self._primary_name}, {self._fallback_name}) are at their daily limit."
            + (f" Last error: {last_exc}" if last_exc else "")
        )


ExtractionLLM = GeminiExtractionLLM | GroqExtractionLLM


def build_extraction_llm(config) -> ExtractionLLM:
    if config.llm_provider == "groq":
        return GroqExtractionLLM(config)
    return GeminiExtractionLLM(config)
