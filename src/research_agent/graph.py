"""LangGraph pipeline, extract-only (PRD §14 step 3): plan_queries -> search ->
select_sources -> fetch_pages -> extract -> finalize.

The verify/retry loop (D1-D6) is not here yet; it's added in step 5, at which
point this graph grows the conditional edges back to targeted_research.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import urlparse

from composio import Composio
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

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
        return {"draft": draft.model_dump(), "llm_provider": provider, "llm_model": model_name}

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

        def mark(field_name: str, is_unknown: bool) -> None:
            if is_unknown:
                confidence[field_name] = "low"
                verification[field_name] = FieldVerification(status="flagged_for_human", reason="value is unknown")
            else:
                confidence[field_name] = "medium"

        mark("access", draft["access"] == "unknown")
        mark("api_breadth", draft["api_breadth"] == "unknown")
        mark("buildability", draft["buildability"] == "unknown")
        mark("main_blocker", draft["main_blocker"] == "unknown")
        mark("existing_mcp", draft["existing_mcp_status"] == "unknown")
        mark("auth_methods", draft["auth_methods"] == ["unknown"])
        mark("api_type", draft["api_type"] == ["unknown"])

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
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "plan_queries")
    graph.add_edge("plan_queries", "search")
    graph.add_edge("search", "select_sources")
    graph.add_edge("select_sources", "fetch_pages")
    graph.add_edge("fetch_pages", "extract")
    graph.add_edge("extract", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()
