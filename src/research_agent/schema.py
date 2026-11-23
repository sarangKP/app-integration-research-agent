"""Pydantic data model for one researched app (PRD §4).

Enum lists add "unknown" alongside the PRD's literal values. The PRD requires
the extractor to say "unknown" rather than guess when evidence isn't found,
but its own enum tables (§4) don't list "unknown" as an allowed value for
every field. Without it, a not-found field would have to be force-fit into
a wrong enum value. Flagged to the user; kept here so pass1/pass2 can store
an honest "not found" state instead of guessing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

AuthMethod = Literal[
    "oauth2",
    "api_key",
    "basic",
    "bearer_token",
    "jwt",
    "hmac_signature",
    "session/cookie",
    "none",
    "other",
    "unknown",
]

AccessType = Literal[
    "self_serve_free",
    "self_serve_trial",
    "paid_plan_required",
    "admin_approval",
    "app_review_required",
    "partner_gated",
    "contact_sales",
    "no_public_api",
    "unknown",
]

ApiType = Literal[
    "rest",
    "graphql",
    "soap",
    "grpc",
    "websocket",
    "sdk_only",
    "cli_only",
    "none",
    "unknown",
]

ApiBreadth = Literal["narrow", "medium", "broad", "unknown"]

McpStatus = Literal["official", "community", "none", "unknown"]

Buildability = Literal["ready", "ready_with_caveats", "blocked", "unknown"]

MainBlocker = Literal[
    "none",
    "partner_approval",
    "paid_plan",
    "app_review",
    "enterprise_only",
    "admin_install",
    "no_public_api",
    "self_host_only",
    "cli_only_not_api",
    "unclear_docs",
    "unknown",
]

ConfidenceLevel = Literal["high", "medium", "low"]

VerificationStatus = Literal["passed", "failed", "flagged_for_human"]


class Evidence(BaseModel):
    field: str
    url: str
    quote: str
    fetched_at: datetime


class ExistingMcp(BaseModel):
    status: McpStatus
    mcp_url: str | None = None


class FieldVerification(BaseModel):
    status: VerificationStatus
    reason: str | None = None


class AppRecord(BaseModel):
    # from input (data/apps.csv)
    id: int
    name: str
    category: str
    hint: str

    one_liner: str

    auth_methods: list[AuthMethod]
    auth_notes: str = ""

    access: AccessType

    api_type: list[ApiType]
    api_breadth: ApiBreadth

    existing_mcp: ExistingMcp

    in_composio_catalog: bool = False

    buildability: Buildability
    main_blocker: MainBlocker

    evidence: list[Evidence] = Field(default_factory=list)
    confidence: dict[str, ConfidenceLevel] = Field(default_factory=dict)
    verification: dict[str, FieldVerification] = Field(default_factory=dict)

    human_edited: bool = False
    human_note: str | None = None

    pass_number: int
    model_provider: str
    model_name: str

    @field_validator("one_liner")
    @classmethod
    def _one_liner_max_20_words(cls, value: str) -> str:
        word_count = len(value.split())
        if word_count > 20:
            raise ValueError(f"one_liner must be at most 20 words, got {word_count}")
        return value
