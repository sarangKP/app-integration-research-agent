"""Environment-driven configuration. No hardcoded model names or credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    composio_api_key: str
    tavily_api_key: str
    firecrawl_api_key: str | None
    google_api_key: str
    gemini_model: str
    gemini_judge_model: str
    llm_provider: str
    groq_api_key: str | None
    groq_model: str | None
    groq_fallback_model: str | None
    aws_bucket: str | None
    cf_distribution_id: str | None
    concurrency: int
    tavily_credit_limit: int
    firecrawl_credit_limit: int


def load_config() -> Config:
    llm_provider = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
    if llm_provider not in ("gemini", "groq"):
        raise RuntimeError(f"Unsupported LLM_PROVIDER: {llm_provider!r} (expected 'gemini' or 'groq')")

    groq_api_key = os.environ.get("GROQ_API_KEY")
    groq_model = os.environ.get("GROQ_MODEL")
    groq_fallback_model = os.environ.get("GROQ_FALLBACK_MODEL")
    if llm_provider == "groq":
        if not groq_api_key:
            raise RuntimeError("Missing required environment variable: GROQ_API_KEY")
        if not groq_model:
            raise RuntimeError("Missing required environment variable: GROQ_MODEL")
        if not groq_fallback_model:
            raise RuntimeError("Missing required environment variable: GROQ_FALLBACK_MODEL")

    return Config(
        composio_api_key=_require("COMPOSIO_API_KEY"),
        tavily_api_key=_require("TAVILY_API_KEY"),
        firecrawl_api_key=os.environ.get("FIRECRAWL_API_KEY"),
        google_api_key=_require("GOOGLE_API_KEY"),
        gemini_model=_require("GEMINI_MODEL"),
        gemini_judge_model=_require("GEMINI_JUDGE_MODEL"),
        llm_provider=llm_provider,
        groq_api_key=groq_api_key,
        groq_model=groq_model,
        groq_fallback_model=groq_fallback_model,
        aws_bucket=os.environ.get("AWS_BUCKET"),
        cf_distribution_id=os.environ.get("CF_DISTRIBUTION_ID"),
        concurrency=int(os.environ.get("RESEARCH_CONCURRENCY", "2")),
        tavily_credit_limit=int(os.environ.get("TAVILY_CREDIT_LIMIT", "900")),
        firecrawl_credit_limit=int(os.environ.get("FIRECRAWL_CREDIT_LIMIT", "900")),
    )
