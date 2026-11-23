"""Build site/dist/index.html from output/pass1.json, output/patterns.json,
and output/eval_pass1.json (PRD §9). Numbers are all pre-computed by
analyze.py/evaluate.py - this script only renders them, never computes
counts itself beyond trivial footer math (sums already in the data).
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent
PASS1_PATH = ROOT / "output/pass1.json"
FAILURES_PATH = ROOT / "output/pass1_failures.json"
PATTERNS_PATH = ROOT / "output/patterns.json"
EVAL_PATH = ROOT / "output/eval_pass1.json"
GROQ_USAGE_PATH = ROOT / "output/groq_usage.json"
DIST_DIR = ROOT / "site/dist"
TEMPLATE_DIR = ROOT / "site/templates"

QWEN_MODEL_NAME = "qwen/qwen3.8-27b"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _safe_json(data) -> str:
    """json.dumps, with </script> neutered so embedded page text (evidence
    quotes, etc.) can never prematurely close the <script> tag it's in."""
    return json.dumps(data, default=str).replace("</", "<\\/")


# Visual-only color cycle for the HTML/CSS bar rows (not data, just styling).
_BAR_COLORS = ["var(--accent)", "var(--info)", "var(--accent-2)", "var(--ok)", "var(--bad)"]


def _count_bars(counts: dict[str, int], relabel: dict[str, str] | None = None) -> list[dict]:
    """Server-rendered bar data from a {label: count} dict - bar width is
    relative to the largest count in the set."""
    relabel = relabel or {}
    items = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    max_val = max((v for _, v in items), default=1) or 1
    return [
        {
            "label": relabel.get(k, k),
            "value": v,
            "pct": round(v / max_val * 100, 1),
            "color": _BAR_COLORS[i % len(_BAR_COLORS)],
        }
        for i, (k, v) in enumerate(items)
    ]


def _accuracy_bars(acc_dict: dict[str, dict]) -> list[dict]:
    """Server-rendered bar data from an {key: {"accuracy": 0..1, "correct":
    n, "total": n}} dict - bar width is the accuracy percentage itself."""
    items = sorted(acc_dict.items(), key=lambda kv: (kv[1].get("accuracy") or 0), reverse=True)
    return [
        {
            "label": k,
            "pct": round((v.get("accuracy") or 0) * 100, 1),
            "correct": v.get("correct"),
            "total": v.get("total"),
            "color": _BAR_COLORS[i % len(_BAR_COLORS)],
        }
        for i, (k, v) in enumerate(items)
    ]


def _heat_table(access_by_category: dict[str, dict[str, int]]) -> dict:
    """Server-rendered heat-table cells with precomputed color intensity
    (alpha, 0-1) so no client-side JS is needed to render it."""
    categories = list(access_by_category.keys())
    access_types = sorted({a for cat in categories for a in access_by_category[cat]})
    max_val = 1
    for cat in categories:
        for a in access_types:
            max_val = max(max_val, access_by_category[cat].get(a, 0))

    rows = []
    for cat in categories:
        cells = []
        for a in access_types:
            v = access_by_category[cat].get(a, 0)
            alpha = round(0.12 + (v / max_val) * 0.68, 3) if v else 0.0
            cells.append({"value": v, "alpha": alpha})
        rows.append({"category": cat, "cells": cells})
    return {"access_types": access_types, "rows": rows}


def _write_results_json_csv(records: list[dict]) -> None:
    (DIST_DIR / "results.json").write_text(json.dumps(records, indent=2, default=str))

    flat_rows = []
    for r in records:
        flat_rows.append(
            {
                "id": r["id"],
                "name": r["name"],
                "category": r["category"],
                "one_liner": r["one_liner"],
                "auth_methods": "|".join(r["auth_methods"]),
                "access": r["access"],
                "api_type": "|".join(r["api_type"]),
                "api_breadth": r["api_breadth"],
                "existing_mcp_status": r["existing_mcp"]["status"],
                "existing_mcp_url": r["existing_mcp"].get("mcp_url") or "",
                "in_composio_catalog": r["in_composio_catalog"],
                "buildability": r["buildability"],
                "main_blocker": r["main_blocker"],
                "model_provider": r["model_provider"],
                "model_name": r["model_name"],
                "low_confidence_fields": ",".join(k for k, v in r["confidence"].items() if v == "low"),
            }
        )
    if flat_rows:
        with (DIST_DIR / "results.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(flat_rows[0].keys()))
            writer.writeheader()
            writer.writerows(flat_rows)


def _write_llms_txt(patterns: dict) -> None:
    text = f"""# App Integration Research Agent - results for agents

This site reports pattern analysis across {patterns['total_apps']} apps researched
for API/auth/buildability characteristics, by an autonomous LangGraph agent.

Only pass 1 (extraction, no verification/retry loop) was run - pass 2 and
final human review were not completed (time/API limits). Treat all numbers
as pass-1 accuracy, not final accuracy.

Files in this directory:
- results.json / results.csv: one record per researched app
- metrics.json: accuracy of pass 1 against a 20-app hand-labeled gold set
- index.html: the full report (data embedded in a <script type="application/json" id="data"> tag)

{patterns['failed_count']} apps could not be researched at all (listed as
"not researched" on the page) due to LLM provider errors.
"""
    (DIST_DIR / "llms.txt").write_text(text)


def main() -> None:
    pass1 = _load(PASS1_PATH)
    failures = _load(FAILURES_PATH) if FAILURES_PATH.exists() else {}
    patterns = _load(PATTERNS_PATH)
    eval_metrics = _load(EVAL_PATH)
    groq_usage = _load(GROQ_USAGE_PATH) if GROQ_USAGE_PATH.exists() else {}

    records = list(pass1.values())
    records.sort(key=lambda r: r["id"])

    for r in records:
        r["lower_reliability"] = r.get("model_name") == QWEN_MODEL_NAME

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    _write_results_json_csv(records)
    (DIST_DIR / "metrics.json").write_text(json.dumps(eval_metrics, indent=2, default=str))
    _write_llms_txt(patterns)

    low_conf_rows = eval_metrics.get("low_confidence_rows", [])
    low_conf_wrong = sum(1 for row in low_conf_rows if not row["correct"])
    low_conf_pct_wrong = round(low_conf_wrong / len(low_conf_rows) * 100, 1) if low_conf_rows else None

    groq_requests_total = sum(m.get("requests", 0) for m in groq_usage.values())
    gemini_apps = sum(1 for r in records if r["model_provider"] == "gemini")

    auth_bars = _count_bars(patterns["auth_overall"])
    blocker_bars = _count_bars(patterns["top_blockers"], relabel={"none": "no blocker found"})
    field_accuracy_bars = _accuracy_bars(eval_metrics["per_field_accuracy"])
    model_accuracy_bars = _accuracy_bars(eval_metrics["accuracy_by_provider_model"])
    heat_table = _heat_table(patterns["access_by_category"])

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template("index.html.j2")

    html = template.render(
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        records=records,
        records_json=_safe_json(records),
        patterns=patterns,
        patterns_json=_safe_json(patterns),
        eval_metrics=eval_metrics,
        eval_metrics_json=_safe_json(eval_metrics),
        failures=failures,
        groq_requests_total=groq_requests_total,
        gemini_apps=gemini_apps,
        low_conf_wrong=low_conf_wrong,
        low_conf_total=len(low_conf_rows),
        low_conf_pct_wrong=low_conf_pct_wrong,
        qwen_model_name=QWEN_MODEL_NAME,
        auth_bars=auth_bars,
        blocker_bars=blocker_bars,
        field_accuracy_bars=field_accuracy_bars,
        model_accuracy_bars=model_accuracy_bars,
        heat_table=heat_table,
    )

    (DIST_DIR / "index.html").write_text(html)
    print(f"Built {DIST_DIR / 'index.html'}")


if __name__ == "__main__":
    main()
