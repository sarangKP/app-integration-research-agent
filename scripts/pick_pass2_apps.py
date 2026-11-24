"""Pick the 50 apps for pass 2: no LLM calls, no writes to pass1.json.

The 50 = 20 gold-labeled apps (data/gold/gold_labels.csv, so pass 2's
output can be scored against ground truth) + 5 apps that failed outright
in pass 1 (output/pass1_failures.json) + 25 apps from pass1.json with the
most combined D1/D2/D4 check failures (the ones most likely to benefit
from the retry loop). Any app already picked by an earlier category is
skipped when filling a later one, so the list never has duplicates.

Writes the result to data/pass2_apps.json and prints it.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from research_agent.graph import _extract_domain, check_d1, check_d2, check_d4
from research_agent.tools import fetch_page_cached_only

APPS_CSV = Path("data/apps.csv")
GOLD_PATH = Path("data/gold/gold_labels.csv")
PASS1_PATH = Path("output/pass1.json")
FAILURES_PATH = Path("output/pass1_failures.json")
OUT_PATH = Path("data/pass2_apps.json")

GOLD_TARGET = 20
FAILED_TARGET = 5
TOP_FAILURE_TARGET = 25


def _draft_like(record: dict) -> dict:
    existing_mcp = record.get("existing_mcp") or {}
    return {
        "access": record.get("access"),
        "api_type": record.get("api_type", []),
        "buildability": record.get("buildability"),
        "main_blocker": record.get("main_blocker"),
        "existing_mcp_status": existing_mcp.get("status"),
        "existing_mcp_url": existing_mcp.get("mcp_url"),
    }


def _d1_d2_d4_failure_count(record: dict) -> int:
    hint_domain = _extract_domain(record.get("hint", ""))
    app_name = record.get("name", "?")
    count = 0

    for e in record.get("evidence", []):
        page = fetch_page_cached_only(e["url"])
        page_text = page.get("text", "") if page else ""
        if not check_d1(e["quote"], page_text)["passed"]:
            count += 1

        d2 = check_d2(e["field"], e["url"], hint_domain, app_name)
        if d2 is not None and not d2["passed"]:
            count += 1

    for d4 in check_d4(_draft_like(record)):
        if not d4["passed"]:
            count += 1

    return count


def main() -> None:
    apps_by_id: dict[str, dict] = {}
    with APPS_CSV.open() as f:
        for row in csv.DictReader(f):
            apps_by_id[row["id"]] = row

    with GOLD_PATH.open() as f:
        gold_ids = [row["id"] for row in csv.DictReader(f)]
    assert len(gold_ids) == GOLD_TARGET, f"expected {GOLD_TARGET} gold apps, found {len(gold_ids)}"

    failures = json.loads(FAILURES_PATH.read_text()) if FAILURES_PATH.exists() else {}
    failed_ids = list(failures.keys())
    assert len(failed_ids) == FAILED_TARGET, f"expected {FAILED_TARGET} failed apps, found {len(failed_ids)}"

    pass1 = json.loads(PASS1_PATH.read_text())

    picked: list[dict] = []
    picked_ids: set[str] = set()

    for app_id in gold_ids:
        app = apps_by_id.get(app_id, {})
        picked.append({"id": app_id, "name": app.get("name", "?"), "reason": "gold"})
        picked_ids.add(app_id)

    for app_id in failed_ids:
        if app_id in picked_ids:
            continue
        name = failures[app_id].get("name") or apps_by_id.get(app_id, {}).get("name", "?")
        picked.append({"id": app_id, "name": name, "reason": "failed_pass1"})
        picked_ids.add(app_id)

    scored = []
    for app_id, record in pass1.items():
        if app_id in picked_ids:
            continue
        score = _d1_d2_d4_failure_count(record)
        scored.append((score, app_id, record.get("name", "?")))
    scored.sort(key=lambda t: (-t[0], t[1]))

    for score, app_id, name in scored[:TOP_FAILURE_TARGET]:
        picked.append({"id": app_id, "name": name, "reason": "most_d1_d2_d4_failures", "score": score})
        picked_ids.add(app_id)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(picked, indent=2))

    print(f"Wrote {len(picked)} apps to {OUT_PATH}")
    by_reason = Counter(p["reason"] for p in picked)
    for reason, count in by_reason.items():
        print(f"  {reason}: {count}")
    print()
    for p in picked:
        extra = f" (score={p['score']})" if "score" in p else ""
        print(f"  {p['id']:>3}  {p['name']:<30} {p['reason']}{extra}")


if __name__ == "__main__":
    main()
