"""LangGraph pipeline (PRD §14 step 5): plan_queries -> search ->
select_sources -> fetch_pages -> extract -> verify -> [targeted_research ->
fetch_pages (retry loop) | finalize].

`verify` currently runs:
  D1 - every evidence quote actually appears in the text of the page it cites
  D2 - the evidence URL's host is one we trust for that field
  D3 - every evidence URL was fetched with status 200
  D4 - certain field values must agree with each other
D5-D6 land in a later step.

Setting the FORCE_FAIL_FIELD env var to a field name is a test-only hook:
it makes `verify` treat that field as failed on the first two passes
(retries 0 and 1) regardless of what the real checks say, so the retry
loop can be exercised without needing a real extraction failure.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import urlparse

from composio import Composio
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from research_agent import prompts
from research_agent.budget import select_passages
from research_agent.llm import ExtractionLLM, StructuredOutputEmpty
from research_agent.retry import call_with_backoff
from research_agent.schema import (
    AccessType,
    ApiBreadth,
    ApiType,
    AppRecord,
    AuthMethod,
    Buildability,
    Evidence,
    ExistingMcp,
    FieldVerification,
    MainBlocker,
    McpStatus,
)
from research_agent.tools import CreditCounter, fetch_page, tavily_search

logger = logging.getLogger(__name__)


class QueryPlan(BaseModel):
    queries: list[str]


class EvidenceDraft(BaseModel):
    field: str
    url: str
    quote: str


class ExtractionDraft(BaseModel):
    one_liner: str
    auth_methods: list[AuthMethod]
    auth_notes: str = ""
    access: AccessType
    api_type: list[ApiType]
    api_breadth: ApiBreadth
    existing_mcp_status: McpStatus
    existing_mcp_url: str | None = None
    buildability: Buildability
    main_blocker: MainBlocker
    evidence: list[EvidenceDraft] = Field(default_factory=list)


class AppState(TypedDict, total=False):
    id: int
    name: str
    category: str
    hint: str
    queries: list[str]
    search_results: list[dict]
    selected_urls: list[str]
    pages: dict[str, dict]
    draft: dict
    record: dict
    llm_provider: str
    llm_model: str
    retries: int
    failed_fields: list[str]
    check_results: dict


@dataclass
class Deps:
    composio_client: Composio | None
    tavily_api_key: str
    firecrawl_api_key: str | None
    credits: CreditCounter
    extraction_llm: ExtractionLLM
    use_cache: bool = True


def _extract_domain(hint: str) -> str:
    match = re.search(r"([a-zA-Z0-9-]+\.[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*)", hint)
    if not match:
        return ""
    return match.group(1).lower().rstrip(".,)")


def _domain_rank(url: str, hint_domain: str, app_name: str) -> int:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if hint_domain and (host == hint_domain or host.endswith("." + hint_domain)):
        return 0
    if any(tok in host for tok in ("docs.", "developer.", "developers.", "api.")):
        return 1
    name_token = app_name.lower().replace(" ", "")
    if host == "github.com" and name_token and name_token in url.lower():
        return 2
    return 3


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


D1_PAGE_TEXT_MIN_LEN = 200


def check_d1(quote: str, page_text: str) -> dict:
    """D1: the evidence quote must actually appear in the page it cites.

    Short quotes (under 8 words) skip the fuzzy match - a 3-word quote can
    coincidentally fuzzy-match almost any page, so it only passes if it's
    an exact substring of the page text. Quotes of 8+ words use rapidfuzz's
    partial_ratio (>= 90) since real sentence-length quotes can have minor
    whitespace/punctuation drift from the source page.

    Module-level (not nested in build_graph) so scripts/d1_audit.py can
    reuse the exact same logic the live verify node uses, instead of
    re-implementing it and risking drift.
    """
    if not page_text or len(page_text) < D1_PAGE_TEXT_MIN_LEN:
        return {"passed": False, "ratio": 0.0, "reason": "page_text_missing"}

    quote_norm = _normalize_for_match(quote)
    text_norm = _normalize_for_match(page_text)

    if len(quote.split()) < 8:
        passed = quote_norm in text_norm
        ratio = 100.0 if passed else 0.0
    else:
        ratio = fuzz.partial_ratio(quote_norm, text_norm)
        passed = ratio >= 90

    return {"passed": passed, "ratio": ratio, "reason": "ok" if passed else "quote_not_found"}


# Fields whose evidence must come from the app's own site/docs (or, loosely,
# its own GitHub org) - factual claims about auth/API/buildability shouldn't
# be sourced from third-party blogs.
D2_OFFICIAL_ONLY_FIELDS = {"auth_methods", "api_type", "access", "buildability", "main_blocker"}

# MCP existence is commonly documented by the community rather than the
# vendor, so these fields additionally trust GitHub (any repo), npm, and
# known MCP registries.
D2_MCP_FIELDS = {"existing_mcp_status", "existing_mcp_url"}
D2_MCP_REGISTRY_HOSTS = {"mcp.so", "smithery.ai", "glama.ai", "modelcontextprotocol.io"}

# Fields D2 intentionally does not check: "one_liner" (not a factual claim
# that needs a trusted source) and anything else (api_breadth, auth_notes -
# not in scope per the spec, so left unchecked rather than silently expanded).


def check_d2(field_name: str, url: str, hint_domain: str, app_name: str) -> dict | None:
    """D2: the evidence URL's host must be one we trust for that field.

    Returns None if this field isn't in scope for D2 at all (one_liner, or
    any field not explicitly listed above) - the caller should skip
    recording a D2 result in that case, same as having no check.
    """
    if field_name not in D2_OFFICIAL_ONLY_FIELDS and field_name not in D2_MCP_FIELDS:
        return None

    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    if hint_domain and (host == hint_domain or host.endswith("." + hint_domain)):
        return {"passed": True, "host": host, "reason": "official_domain"}

    name_token = app_name.lower().replace(" ", "")
    if host == "github.com" and name_token and name_token in url.lower():
        return {"passed": True, "host": host, "reason": "github_official_org"}

    if field_name in D2_MCP_FIELDS:
        if host == "github.com":
            return {"passed": True, "host": host, "reason": "github_registry"}
        if host == "npmjs.com" or host.endswith(".npmjs.com"):
            return {"passed": True, "host": host, "reason": "npmjs_registry"}
        if host in D2_MCP_REGISTRY_HOSTS:
            return {"passed": True, "host": host, "reason": "mcp_registry"}

    return {"passed": False, "host": host, "reason": "untrusted_host"}


# Fields that participate in at least one D4 rule (as either side of a
# pair). Used by compute_confidence to tell "D4 checked this and found no
# violation" apart from "D4 never looks at this field at all".
D4_SCOPED_FIELDS = {"access", "buildability", "api_type", "main_blocker", "existing_mcp_status", "existing_mcp_url"}


def check_d4(draft: dict) -> list[dict]:
    """D4: certain field values must agree with each other.

    Operates on a flat, ExtractionDraft-shaped dict (access, api_type,
    buildability, main_blocker, existing_mcp_status, existing_mcp_url).
    Module-level so scripts/d1_audit.py can reuse it against pass1.json
    records (after reshaping AppRecord's nested `existing_mcp` into the
    same flat keys) with no logic duplicated.

    Each rule below compares exactly two fields; a failure reports both,
    since it's not knowable from the rule alone which of the two is wrong.
    """
    results: list[dict] = []

    access = draft.get("access")
    buildability = draft.get("buildability")
    if access == "no_public_api":
        passed = buildability == "blocked"
        results.append({
            "rule": "no_public_api_implies_blocked",
            "fields": ["access", "buildability"],
            "passed": passed,
            "detail": f"access={access!r} buildability={buildability!r}",
        })

    api_type = draft.get("api_type") or []
    main_blocker = draft.get("main_blocker")
    if api_type == ["cli_only"]:
        passed = main_blocker == "cli_only_not_api"
        results.append({
            "rule": "cli_only_implies_main_blocker",
            "fields": ["api_type", "main_blocker"],
            "passed": passed,
            "detail": f"api_type={api_type!r} main_blocker={main_blocker!r}",
        })

    mcp_status = draft.get("existing_mcp_status")
    mcp_url = draft.get("existing_mcp_url")
    mcp_url_empty = not mcp_url or mcp_url == "unknown"
    if mcp_status in ("official", "community"):
        results.append({
            "rule": "mcp_exists_implies_url_present",
            "fields": ["existing_mcp_status", "existing_mcp_url"],
            "passed": not mcp_url_empty,
            "detail": f"existing_mcp_status={mcp_status!r} existing_mcp_url={mcp_url!r}",
        })
    elif mcp_status == "none":
        results.append({
            "rule": "mcp_none_implies_no_url",
            "fields": ["existing_mcp_status", "existing_mcp_url"],
            "passed": mcp_url_empty,
            "detail": f"existing_mcp_status={mcp_status!r} existing_mcp_url={mcp_url!r}",
        })

    return results


# Field categories for targeted_research's query-building. Auth-shaped
# fields and MCP-shaped fields get different query phrasing because
# "MCP server" and "API authentication" are different search problems.
TARGETED_AUTH_FIELDS = {"auth_methods", "access", "api_type"}
TARGETED_MCP_FIELDS = {"existing_mcp_status", "existing_mcp_url"}

MAX_QUERIES_PER_FIELD = 2
MAX_QUERIES_PER_RETRY = 4


def compute_confidence(
    is_unknown: bool,
    failed_fields: list[str],
    check_results: dict,
    evidence_list: list[dict],
    field_names: list[str],
) -> dict:
    """Confidence for one logical field (one_liner-style single field, or
    the existing_mcp_status+existing_mcp_url pair treated as one), from
    D1/D2/D4 check_results plus whether verify ever gave up on it.

    - low: value is "unknown", or the field is still in failed_fields
      (verify never got it to pass even after the retry loop ran out).
      Also flagged_for_human.
    - high: D1 passed, D2 passed (where applicable), D4 passed (where
      applicable), there's real evidence, and none of its quotes were
      short enough to need the exact-substring fallback instead of a
      real fuzzy match.
    - medium: otherwise passed, but D2 or D4 didn't apply to this field
      (skipped) or a quote was short (weak).
    """
    still_failing = any(f in failed_fields for f in field_names)
    if is_unknown or still_failing:
        reason = "value is unknown" if is_unknown else "still failing verification checks after max retries"
        return {"level": "low", "flagged": True, "reason": reason}

    evidence_items = [e for e in evidence_list if e["field"] in field_names]
    if not evidence_items:
        return {"level": "medium", "flagged": False, "reason": None}

    def _collect(check_name: str) -> list[dict]:
        values = [check_results.get(f, {}).get(check_name) for f in field_names]
        return [v for v in values if v is not None]

    d1_values = _collect("D1")
    d1_passed = all(v["passed"] for v in d1_values) if d1_values else False

    d2_values = _collect("D2")
    d2_passed = all(v["passed"] for v in d2_values) if d2_values else None  # None = D2 skipped for this field

    d4_lists = [check_results.get(f, {}).get("D4") for f in field_names]
    d4_list = next((lst for lst in d4_lists if lst), [])
    if d4_list:
        d4_passed = all(r["passed"] for r in d4_list)
    elif any(f in D4_SCOPED_FIELDS for f in field_names):
        d4_passed = True  # in D4's scope, but no rule's trigger condition fired - vacuously fine
    else:
        d4_passed = None  # this field is never referenced by any D4 rule - genuinely skipped

    weak_quote = any(len(e["quote"].split()) < 8 for e in evidence_items)

    if not d1_passed or d2_passed is False or d4_passed is False:
        return {"level": "low", "flagged": True, "reason": "failed D1/D2/D4 checks"}

    if d2_passed is None or d4_passed is None or weak_quote:
        return {"level": "medium", "flagged": False, "reason": None}

    return {"level": "high", "flagged": False, "reason": None}


def build_targeted_queries(app_name: str, hint_domain: str, field_name: str, d2_failed: bool) -> list[str]:
    """Build up to MAX_QUERIES_PER_FIELD search queries for one failed field.

    If this field's evidence failed D2 (untrusted source) specifically, a
    domain-scoped query is moved to the front so it survives the per-field
    cap - the goal on a D2 retry is "find this on the official site", not
    just "find this anywhere".
    """
    if field_name in TARGETED_AUTH_FIELDS:
        candidates = [f"{app_name} API authentication", f"{app_name} developer docs"]
        if hint_domain:
            candidates.append(f"site:{hint_domain} API auth")
    elif field_name in TARGETED_MCP_FIELDS:
        candidates = [f"{app_name} MCP server github", f"{app_name} MCP server npm"]
    else:
        candidates = [f"{app_name} {field_name} API documentation"]
        if hint_domain:
            candidates.append(f"site:{hint_domain} {field_name}")

    if d2_failed and hint_domain:
        domain_query = next((q for q in candidates if q.startswith("site:")), f"site:{hint_domain} {field_name}")
        candidates = [domain_query] + [q for q in candidates if q != domain_query]

    return candidates[:MAX_QUERIES_PER_FIELD]


def _is_retryable_extract_error(exc: Exception) -> bool:
    if isinstance(exc, StructuredOutputEmpty):
        return True
    return "failed to generate json" in str(exc).lower()


def build_graph(deps: Deps):
    def plan_queries(state: AppState) -> dict:
        plan, provider, model_name = deps.extraction_llm.invoke_structured(
            QueryPlan, prompts.plan_queries_user(state["name"], state["category"], state["hint"])
        )
        logger.info("[%s] plan_queries: provider=%s model=%s", state["name"], provider, model_name)
        return {"queries": plan.queries[:3], "llm_provider": provider, "llm_model": model_name}

    def search(state: AppState) -> dict:
        all_results = []
        for query in state["queries"]:
            result = call_with_backoff(
                tavily_search,
                query,
                composio_client=deps.composio_client,
                tavily_api_key=deps.tavily_api_key,
                credits=deps.credits,
                use_cache=deps.use_cache,
            )
            all_results.append({"query": query, "results": result.get("results", [])})
        return {"search_results": all_results}

    def select_sources(state: AppState) -> dict:
        hint_domain = _extract_domain(state["hint"])
        seen: set[str] = set()
        candidates: list[str] = []
        for block in state["search_results"]:
            for r in block["results"]:
                url = r.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                candidates.append(url)
        candidates.sort(key=lambda u: _domain_rank(u, hint_domain, state["name"]))
        return {"selected_urls": candidates[:6]}

    def fetch_pages(state: AppState) -> dict:
        pages: dict[str, dict] = {}
        for url in state["selected_urls"]:
            try:
                result = fetch_page(url, use_cache=deps.use_cache)
            except Exception as exc:
                logger.warning("fetch failed for %s: %s", url, exc)
                continue
            if result.get("text"):
                pages[url] = result
        return {"pages": pages}

    def extract(state: AppState) -> dict:
        pages = state.get("pages", {})
        failed_fields = state.get("failed_fields") or []
        previous_draft = state.get("draft") if failed_fields else None

        if not pages:
            draft = ExtractionDraft(
                one_liner="Insufficient page text was retrieved to research this app.",
                auth_methods=["unknown"],
                access="unknown",
                api_type=["unknown"],
                api_breadth="unknown",
                existing_mcp_status="unknown",
                buildability="unknown",
                main_blocker="unknown",
            )
            return {"draft": draft.model_dump(), "llm_provider": state.get("llm_provider", ""), "llm_model": state.get("llm_model", "")}

        page_list_full = [{"url": u, "text": d["text"]} for u, d in pages.items()]

        def attempt(token_budget: int):
            if deps.extraction_llm.provider == "groq":
                page_list = select_passages(page_list_full, token_budget=token_budget)
            else:
                page_list = page_list_full
            user_msg = prompts.extract_user(state["name"], state["category"], state["hint"], page_list)
            return deps.extraction_llm.invoke_structured(
                ExtractionDraft, [("system", prompts.EXTRACT_SYSTEM), ("human", user_msg)]
            )

        try:
            draft, provider, model_name = attempt(3500)
        except Exception as exc:
            if not _is_retryable_extract_error(exc):
                raise
            logger.warning(
                "[%s] extract failed (%s), retrying once with a smaller evidence cap (2500 tokens)",
                state["name"], exc,
            )
            draft, provider, model_name = attempt(2500)

        logger.info("[%s] extract: provider=%s model=%s", state["name"], provider, model_name)

        draft_dict = draft.model_dump()
        if previous_draft is not None:
            # Retry pass: only the fields verify() flagged get the new LLM
            # output; everything that already passed keeps its old value
            # instead of being silently overwritten by a fresh (and
            # possibly worse) re-extraction.
            merged = dict(previous_draft)
            for field_name in failed_fields:
                if field_name in draft_dict:
                    merged[field_name] = draft_dict[field_name]
            merged["evidence"] = [
                e for e in previous_draft.get("evidence", []) if e["field"] not in failed_fields
            ] + [e for e in draft_dict.get("evidence", []) if e["field"] in failed_fields]
            draft_dict = merged

        return {"draft": draft_dict, "llm_provider": provider, "llm_model": model_name}

    def verify(state: AppState) -> dict:
        draft = state.get("draft", {})
        pages = state.get("pages", {})
        hint_domain = _extract_domain(state["hint"])

        failed_fields: list[str] = []
        check_results: dict = {}

        def record(field_name: str, check_name: str, passed: bool, **extra) -> None:
            check_results.setdefault(field_name, {})[check_name] = {"passed": passed, **extra}
            if not passed and field_name not in failed_fields:
                failed_fields.append(field_name)

        for e in draft.get("evidence", []):
            field_name = e["field"]
            page = pages.get(e["url"])

            d3_passed = page is not None and page.get("status") == 200
            record(field_name, "D3", d3_passed, url=e["url"])

            d1_result = check_d1(e["quote"], page.get("text", "") if page is not None else "")
            record(field_name, "D1", d1_result["passed"], ratio=d1_result["ratio"], reason=d1_result["reason"])

            d2_result = check_d2(field_name, e["url"], hint_domain, state["name"])
            if d2_result is not None:
                record(field_name, "D2", d2_result["passed"], host=d2_result["host"], reason=d2_result["reason"])

        for d4_result in check_d4(draft):
            for field_name in d4_result["fields"]:
                check_results.setdefault(field_name, {}).setdefault("D4", []).append(d4_result)
                if not d4_result["passed"] and field_name not in failed_fields:
                    failed_fields.append(field_name)

        force_field = os.environ.get("FORCE_FAIL_FIELD")
        if force_field and state.get("retries", 0) < 2:
            record(force_field, "FORCE_FAIL_TEST", False, reason="forced via FORCE_FAIL_FIELD env var")

        return {"failed_fields": failed_fields, "check_results": check_results}

    def route_after_verify(state: AppState) -> str:
        if state.get("failed_fields") and state.get("retries", 0) < 2:
            return "targeted_research"
        return "finalize"

    def targeted_research(state: AppState) -> dict:
        retries = state.get("retries", 0) + 1
        failed_fields = state.get("failed_fields", [])
        check_results = state.get("check_results", {})
        hint_domain = _extract_domain(state["hint"])

        queries: list[str] = []
        for field_name in failed_fields:
            d2_failed = check_results.get(field_name, {}).get("D2", {}).get("passed", True) is False
            for query in build_targeted_queries(state["name"], hint_domain, field_name, d2_failed):
                if query not in queries:
                    queries.append(query)
            if len(queries) >= MAX_QUERIES_PER_RETRY:
                break
        queries = queries[:MAX_QUERIES_PER_RETRY]

        logger.info(
            "[%s] targeted_research retry %d: %d Tavily search(es) for %d failed field(s): %s",
            state["name"], retries, len(queries), len(failed_fields), queries,
        )

        new_results = []
        for query in queries:
            result = call_with_backoff(
                tavily_search,
                query,
                composio_client=deps.composio_client,
                tavily_api_key=deps.tavily_api_key,
                credits=deps.credits,
                use_cache=deps.use_cache,
            )
            new_results.append({"query": query, "results": result.get("results", [])})

        seen: set[str] = set(state.get("selected_urls", []))
        candidates: list[str] = list(state.get("selected_urls", []))
        for block in new_results:
            for r in block["results"]:
                url = r.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                candidates.append(url)
        candidates.sort(key=lambda u: _domain_rank(u, hint_domain, state["name"]))

        return {
            "retries": retries,
            "search_results": state.get("search_results", []) + new_results,
            "selected_urls": candidates[:6],
        }

    def finalize(state: AppState) -> dict:
        draft = state["draft"]
        pages = state.get("pages", {})

        one_liner_words = draft["one_liner"].split()
        one_liner = " ".join(one_liner_words[:20]) if len(one_liner_words) > 20 else draft["one_liner"]

        evidence: list[Evidence] = []
        for e in draft.get("evidence", []):
            fetched_at = pages.get(e["url"], {}).get("fetched_at")
            if fetched_at is None:
                continue
            evidence.append(Evidence(field=e["field"], url=e["url"], quote=e["quote"], fetched_at=fetched_at))

        confidence: dict[str, str] = {}
        verification: dict[str, FieldVerification] = {}
        check_results = state.get("check_results", {})
        failed_fields = state.get("failed_fields") or []
        draft_evidence = draft.get("evidence", [])

        def mark(field_name: str, is_unknown: bool, check_field_names: list[str]) -> None:
            result = compute_confidence(is_unknown, failed_fields, check_results, draft_evidence, check_field_names)
            confidence[field_name] = result["level"]
            if result["flagged"]:
                verification[field_name] = FieldVerification(status="flagged_for_human", reason=result["reason"])

        mark("access", draft["access"] == "unknown", ["access"])
        mark("api_breadth", draft["api_breadth"] == "unknown", ["api_breadth"])
        mark("buildability", draft["buildability"] == "unknown", ["buildability"])
        mark("main_blocker", draft["main_blocker"] == "unknown", ["main_blocker"])
        mark("existing_mcp", draft["existing_mcp_status"] == "unknown", ["existing_mcp_status", "existing_mcp_url"])
        mark("auth_methods", draft["auth_methods"] == ["unknown"], ["auth_methods"])
        mark("api_type", draft["api_type"] == ["unknown"], ["api_type"])

        record = AppRecord(
            id=state["id"],
            name=state["name"],
            category=state["category"],
            hint=state["hint"],
            one_liner=one_liner,
            auth_methods=draft["auth_methods"],
            auth_notes=draft.get("auth_notes", ""),
            access=draft["access"],
            api_type=draft["api_type"],
            api_breadth=draft["api_breadth"],
            existing_mcp=ExistingMcp(status=draft["existing_mcp_status"], mcp_url=draft.get("existing_mcp_url")),
            in_composio_catalog=False,
            buildability=draft["buildability"],
            main_blocker=draft["main_blocker"],
            evidence=evidence,
            confidence=confidence,
            verification=verification,
            human_edited=False,
            pass_number=1,
            model_provider=state.get("llm_provider", "unknown"),
            model_name=state.get("llm_model", "unknown"),
        )
        return {"record": record.model_dump(mode="json")}

    graph = StateGraph(AppState)
    graph.add_node("plan_queries", plan_queries)
    graph.add_node("search", search)
    graph.add_node("select_sources", select_sources)
    graph.add_node("fetch_pages", fetch_pages)
    graph.add_node("extract", extract)
    graph.add_node("verify", verify)
    graph.add_node("targeted_research", targeted_research)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "plan_queries")
    graph.add_edge("plan_queries", "search")
    graph.add_edge("search", "select_sources")
    graph.add_edge("select_sources", "fetch_pages")
    graph.add_edge("fetch_pages", "extract")
    graph.add_edge("extract", "verify")
    graph.add_conditional_edges(
        "verify",
        route_after_verify,
        {"targeted_research": "targeted_research", "finalize": "finalize"},
    )
    graph.add_edge("targeted_research", "fetch_pages")
    graph.add_edge("finalize", END)

    return graph.compile()
