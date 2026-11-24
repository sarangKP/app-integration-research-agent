"""Offline audit for D1-D4: no LLM or search calls, no writes to pass1.json.

Loads output/pass1.json and whatever is already in data/cache, and re-runs
the exact same check_d1/check_d2/check_d4 functions the live `verify` node
uses (D3 is simple enough to inline) against every evidence item / app, so
there's no duplicated logic to drift out of sync with the real pipeline.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from research_agent.graph import _extract_domain, check_d1, check_d2, check_d4
from research_agent.tools import fetch_page_cached_only

PASS1_PATH = Path("output/pass1.json")


def _draft_like(record: dict) -> dict:
    """Reshape an AppRecord (nested existing_mcp) into the flat
    ExtractionDraft-shaped dict check_d4 expects."""
    existing_mcp = record.get("existing_mcp") or {}
    return {
        "access": record.get("access"),
        "api_type": record.get("api_type", []),
        "buildability": record.get("buildability"),
        "main_blocker": record.get("main_blocker"),
        "existing_mcp_status": existing_mcp.get("status"),
        "existing_mcp_url": existing_mcp.get("mcp_url"),
    }


def main() -> None:
    pass1 = json.loads(PASS1_PATH.read_text())

    total_evidence = 0
    apps_with_failure: dict[str, set[str]] = {"D1": set(), "D2": set(), "D3": set(), "D4": set()}
    failed_counts = {"D1": 0, "D2": 0, "D3": 0, "D4": 0}

    d1_reason_counts: Counter[str] = Counter()
    d1_field_quote_not_found: Counter[str] = Counter()
    d1_field_totals: Counter[str] = Counter()
    d2_field_failures: Counter[str] = Counter()
    d2_field_totals: Counter[str] = Counter()
    d2_examples: list[str] = []
    d4_examples: list[str] = []

    for record in pass1.values():
        app_name = record.get("name", "?")
        hint_domain = _extract_domain(record.get("hint", ""))

        for e in record.get("evidence", []):
            total_evidence += 1
            field = e["field"]

            page = fetch_page_cached_only(e["url"])
            page_text = page.get("text", "") if page else ""

            d1_field_totals[field] += 1
            d1 = check_d1(e["quote"], page_text)
            d1_reason_counts[d1["reason"]] += 1
            if not d1["passed"]:
                failed_counts["D1"] += 1
                apps_with_failure["D1"].add(app_name)
                if d1["reason"] == "quote_not_found":
                    d1_field_quote_not_found[field] += 1

            d3_passed = page is not None and page.get("status") == 200
            if not d3_passed:
                failed_counts["D3"] += 1
                apps_with_failure["D3"].add(app_name)

            d2 = check_d2(field, e["url"], hint_domain, app_name)
            if d2 is not None:
                d2_field_totals[field] += 1
                if not d2["passed"]:
                    failed_counts["D2"] += 1
                    apps_with_failure["D2"].add(app_name)
                    d2_field_failures[field] += 1
                    if len(d2_examples) < 5:
                        d2_examples.append(
                            f"{app_name} / {field}: host={d2['host']!r} reason={d2['reason']} url={e['url']}"
                        )

        for d4 in check_d4(_draft_like(record)):
            if not d4["passed"]:
                failed_counts["D4"] += 1
                apps_with_failure["D4"].add(app_name)
                if len(d4_examples) < 5:
                    d4_examples.append(f"{app_name}: rule={d4['rule']} ({d4['detail']})")

    print("Apps with >=1 failure, per check:")
    for check in ("D1", "D2", "D3", "D4"):
        print(f"  {check}: {len(apps_with_failure[check])} / {len(pass1)}")
    print()
    print(f"Total evidence items: {total_evidence}")
    for check in ("D1", "D2", "D3"):
        print(f"  {check} items failed: {failed_counts[check]} / {total_evidence}")
    print(f"  D4 rule checks failed: {failed_counts['D4']} (per-app rule, not per-evidence-item)")

    print()
    print("D1 - counts per reason:")
    for reason in ("ok", "quote_not_found", "page_text_missing"):
        print(f"  {reason}: {d1_reason_counts.get(reason, 0)}")
    print()
    print("D1 - failures per field (quote_not_found only):")
    for field in sorted(d1_field_totals):
        print(f"  {field}: {d1_field_quote_not_found.get(field, 0)}/{d1_field_totals[field]}")

    print()
    print("D2 - failures per field (fields D2 actually checks):")
    for field in sorted(d2_field_totals):
        print(f"  {field}: {d2_field_failures.get(field, 0)}/{d2_field_totals[field]}")

    print()
    print("D2 - 5 example failures:")
    for ex in d2_examples:
        print(f"  {ex}")

    print()
    print("D4 - 5 example failures:")
    for ex in d4_examples:
        print(f"  {ex}")

    print()
    print("Copper detail (D1):")
    copper = next((r for r in pass1.values() if r.get("name") == "Copper"), None)
    if copper is None:
        print("  (no 'Copper' record in pass1.json)")
    else:
        for e in copper.get("evidence", []):
            page = fetch_page_cached_only(e["url"])
            page_text = page.get("text", "") if page else ""
            result = check_d1(e["quote"], page_text)
            cached = "yes" if page is not None else "no"
            print(
                f"  field={e['field']:<20} reason={result['reason']:<18} "
                f"cached_in_data/cache={cached:<3} url={e['url']}"
            )


if __name__ == "__main__":
    main()
