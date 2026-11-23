"""Token-budgeted passage selection for small-context providers (Groq).

Blindly truncating each fetched page at N characters risks cutting out the
one paragraph that actually answers a field. Instead, score paragraphs by
keyword relevance (auth, pricing, API surface, MCP) and keep the
highest-scoring ones until the token budget is spent.
"""

from __future__ import annotations

import re

from research_agent.llm import count_tokens

_KEYWORDS = [
    "oauth", "api key", "api-key", "apikey", "bearer", "basic auth", "hmac",
    "jwt", "session", "cookie", "token", "client id", "client secret",
    "pricing", "plan", "partner", "enterprise", "contact sales", "free",
    "trial", "paid", "rest api", "graphql", "soap", "grpc", "websocket",
    "sdk", "cli", "mcp", "model context protocol", "endpoint", "rate limit",
    "scope", "app review", "approval", "self-serve", "self serve",
]


def _score(paragraph: str) -> int:
    lowered = paragraph.lower()
    return sum(1 for kw in _KEYWORDS if kw in lowered)


def select_passages(pages: list[dict], token_budget: int = 3500) -> list[dict]:
    """pages: [{"url": str, "text": str}, ...] -> same shape, text replaced
    by a budget-limited, keyword-prioritized excerpt."""
    if not pages:
        return []

    per_page_budget = max(token_budget // len(pages), 200)
    selected: list[dict] = []
    total_used = 0

    for page in pages:
        if total_used >= token_budget:
            break
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", page["text"]) if p.strip()]
        if not paragraphs:
            continue

        scored = sorted(enumerate(paragraphs), key=lambda ip: (-_score(ip[1]), ip[0]))
        budget_left = min(per_page_budget, token_budget - total_used)

        chosen: set[int] = set()
        used = 0
        for idx, para in scored:
            t = count_tokens(para)
            if used + t > budget_left and chosen:
                continue
            chosen.add(idx)
            used += t
            if used >= budget_left:
                break
        if not chosen:
            chosen = {0}

        excerpt = "\n\n".join(paragraphs[i] for i in sorted(chosen))
        selected.append({"url": page["url"], "text": excerpt})
        total_used += count_tokens(excerpt)

    return selected
