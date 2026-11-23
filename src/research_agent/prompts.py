"""LLM prompts (PRD §6)."""

from __future__ import annotations

PLAN_QUERIES_SYSTEM = (
    "You are planning web searches to research a software app's API for a developer-tooling catalog."
)


def plan_queries_user(name: str, category: str, hint: str) -> str:
    return (
        f"App: {name}\nCategory: {category}\nHint (likely domain or starting point): {hint}\n\n"
        "Write exactly 3 targeted web search queries to research this app's API surface:\n"
        "1. Authentication and getting-started / API keys\n"
        "2. Pricing, plans, and partner-program access\n"
        "3. API reference and whether it has an existing MCP server\n"
        "Keep each query short and specific, as if typed into a search engine."
    )


EXTRACT_SYSTEM = (
    "You are extracting structured facts about a software app's API from fetched web page text. "
    "Only use the provided page text. Every claim needs a verbatim quote from that text. "
    'If the answer is not found in the provided text, say "unknown", don\'t guess.'
)


def extract_user(name: str, category: str, hint: str, pages: list[dict]) -> str:
    page_blocks = "\n\n".join(f"--- SOURCE: {p['url']} ---\n{p['text'][:8000]}" for p in pages)
    return (
        f"App: {name}\nCategory: {category}\nHint: {hint}\n\n"
        f"{page_blocks}\n\n"
        "Using only the text above, fill in the fields. For every field you answer "
        '(anything other than "unknown"), include at least one evidence entry: the '
        "field name, the exact source URL it came from (must be one of the SOURCE "
        "URLs above), and a short verbatim quote from that source that supports your "
        'answer. If nothing in the text answers a field, set it to "unknown" and do '
        "not include evidence for it."
    )
