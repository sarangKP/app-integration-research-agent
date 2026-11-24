"""Score output/<prefix>.json against the gold set (read-only), or compare
two such files (e.g. pass1 vs pass2) on the gold apps they share.

The 20 gold labels were drafted with Claude from official docs,
independently of the agent's output, then spot-checked by a human against
the sources (15 of 20 verified; one error found and corrected - fanbasis).

Scored fields: auth_methods, access, api_type, buildability, main_blocker,
existing_mcp_status. existing_mcp_status is scored only for gold rows where
it was actually verified (gold value != "not_checked"). api_breadth is not
scored (PRD/user choice). existing_mcp_url has no ground truth at all -
gold_labels.csv only has one existing_mcp column (the status), not a URL -
so it is never scored against gold; --compare reports it as "n/a".

Matching rule: predicted == gold counts as correct, including when both
are "unknown" (e.g. Waterfall.io/access, where gold itself is "unknown" and
the agent honestly said the same). A predicted "unknown" only counts as a
miss when gold has a real, verified answer. auth_methods/api_type use exact
set matching, so an extra predicted item (e.g. HubSpot predicting an extra
"other" auth method) counts as a miss - per_field_accuracy_loose_subset
reports the looser "gold subset of predicted" variant alongside it.

Read-only: never writes to <prefix>.json or gold_labels.csv - only ever to
output/eval_<prefix>.json.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

GOLD_PATH = Path("data/gold/gold_labels.csv")

SET_FIELDS = ("auth_methods", "api_type")
SCALAR_FIELDS = ("access", "buildability", "main_blocker")
ALL_MAIN_FIELDS = SET_FIELDS + SCALAR_FIELDS
# existing_mcp_url deliberately excluded: gold_labels.csv has no URL column,
# so there is nothing to score it against.
COMPARE_FIELDS = ALL_MAIN_FIELDS + ("existing_mcp_status",)


def record_path(prefix: str, base_dir: Path = Path("output")) -> Path:
    return base_dir / f"{prefix}.json"


def eval_out_path(prefix: str, base_dir: Path = Path("output")) -> Path:
    return base_dir / f"eval_{prefix}.json"


def _load_gold() -> list[dict]:
    with GOLD_PATH.open() as f:
        return list(csv.DictReader(f))


def _load_records(path: Path) -> dict[str, dict]:
    return json.loads(path.read_text())


def _is_unknown(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return value == ["unknown"] or len(value) == 0
    return str(value).strip().lower() == "unknown"


def _gold_set(value: str) -> set[str]:
    return {v.strip() for v in value.split("|") if v.strip()}


def _predicted_value(record: dict, field: str) -> Any:
    if field == "existing_mcp_status":
        return record.get("existing_mcp", {}).get("status")
    if field == "existing_mcp_url":
        return record.get("existing_mcp", {}).get("mcp_url")
    return record.get(field)


def _gold_value(gold_row: dict, field: str) -> Any:
    if field == "existing_mcp_status":
        return gold_row["existing_mcp"]
    return gold_row[field]


def _score_field(field: str, gold_value: Any, predicted_value: Any) -> bool:
    """True = correct. predicted == gold counts as correct even when both
    are "unknown"; predicted "unknown" against a real gold answer is a miss."""
    if field in SET_FIELDS:
        gold_set = _gold_set(gold_value)
        if _is_unknown(predicted_value):
            return gold_set == {"unknown"}
        return set(predicted_value) == gold_set
    gold_str = str(gold_value).strip()
    if _is_unknown(predicted_value):
        return gold_str.lower() == "unknown"
    return str(predicted_value).strip() == gold_str


def _score_field_loose(field: str, gold_value: Any, predicted_value: Any) -> bool:
    """Looser variant for set fields only: correct if every gold value is
    present in predicted (extra items in predicted don't count against it)."""
    gold_set = _gold_set(gold_value)
    if _is_unknown(predicted_value):
        return gold_set == {"unknown"}
    return gold_set.issubset(set(predicted_value))


def _acc(d: dict[str, int]) -> float | None:
    return round(d["correct"] / d["total"], 4) if d["total"] else None


def score_against_gold(gold_rows: list[dict], records: dict[str, dict]) -> dict:
    """Score `records` (an already-loaded <prefix>.json dict) against
    `gold_rows`, scoring only the gold rows present in `records` (so callers
    that want a fair two-way comparison should pre-filter gold_rows to the
    ids common to both files before calling this)."""
    per_field: dict[str, dict[str, int]] = {f: {"correct": 0, "total": 0} for f in COMPARE_FIELDS}
    per_field_loose: dict[str, dict[str, int]] = {f: {"correct": 0, "total": 0} for f in SET_FIELDS}
    by_provider_model: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})
    mismatches: list[dict] = []
    low_confidence_rows: list[dict] = []
    skipped: list[dict] = []
    scored_app_count = 0

    for gold in gold_rows:
        gid = gold["id"]
        record = records.get(gid)
        if record is None:
            skipped.append({"id": gid, "name": gold["name"]})
            continue
        scored_app_count += 1

        provider_model = f"{record.get('model_provider', 'unknown')}/{record.get('model_name', 'unknown')}"
        confidence = record.get("confidence", {})

        fields_to_score = list(ALL_MAIN_FIELDS)
        if gold["existing_mcp"].strip().lower() != "not_checked":
            fields_to_score.append("existing_mcp_status")

        for field in fields_to_score:
            gold_value = _gold_value(gold, field)
            predicted_value = _predicted_value(record, field)
            correct = _score_field(field, gold_value, predicted_value)

            if field in SET_FIELDS:
                per_field_loose[field]["total"] += 1
                if _score_field_loose(field, gold_value, predicted_value):
                    per_field_loose[field]["correct"] += 1

            per_field[field]["total"] += 1
            by_provider_model[provider_model]["total"] += 1
            if correct:
                per_field[field]["correct"] += 1
                by_provider_model[provider_model]["correct"] += 1
            else:
                mismatches.append({
                    "app": gold["name"], "id": gid, "field": field,
                    "gold": gold_value, "predicted": predicted_value,
                    "confidence": confidence.get(field, "n/a"),
                    "provider": record.get("model_provider"), "model": record.get("model_name"),
                })

            field_confidence = confidence.get("existing_mcp" if field == "existing_mcp_status" else field)
            if field_confidence == "low":
                low_confidence_rows.append({
                    "app": gold["name"], "id": gid, "field": field,
                    "gold": gold_value, "predicted": predicted_value, "correct": correct,
                })

    overall_correct = sum(v["correct"] for v in per_field.values())
    overall_total = sum(v["total"] for v in per_field.values())

    return {
        "gold_total": len(gold_rows),
        "scored_apps": scored_app_count,
        "scored_apps_label": f"{scored_app_count}/{len(gold_rows)}",
        "skipped_missing": skipped,
        "per_field_accuracy": {f: {**v, "accuracy": _acc(v)} for f, v in per_field.items()},
        "per_field_accuracy_loose_subset": {f: {**v, "accuracy": _acc(v)} for f, v in per_field_loose.items()},
        "overall_accuracy": {
            "correct": overall_correct, "total": overall_total,
            "accuracy": round(overall_correct / overall_total, 4) if overall_total else None,
        },
        "accuracy_by_provider_model": {k: {**v, "accuracy": _acc(v)} for k, v in by_provider_model.items()},
        "low_confidence_rows": low_confidence_rows,
        "mismatches": mismatches,
    }


def run_single(output_prefix: str) -> None:
    gold_rows = _load_gold()
    records = _load_records(record_path(output_prefix))
    result = score_against_gold(gold_rows, records)

    out_path = eval_out_path(output_prefix)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, default=str))

    print(f"[{output_prefix}] Scored {result['scored_apps_label']} gold apps")
    if result["skipped_missing"]:
        print(f"Skipped (missing from {output_prefix}.json): {', '.join(s['name'] for s in result['skipped_missing'])}")
    print()

    print("Per-field accuracy:")
    for field, v in result["per_field_accuracy"].items():
        acc = f"{v['accuracy']:.1%}" if v["accuracy"] is not None else "n/a (0 scored)"
        print(f"  {field:20s} {v['correct']:3d}/{v['total']:3d}  {acc}")
    print()

    print("Looser scoring for set fields (gold values all present in predicted, extra items don't count against it):")
    for field, v in result["per_field_accuracy_loose_subset"].items():
        acc = f"{v['accuracy']:.1%}" if v["accuracy"] is not None else "n/a (0 scored)"
        print(f"  {field:20s} {v['correct']:3d}/{v['total']:3d}  {acc}")
    print()

    oa = result["overall_accuracy"]
    print(f"Overall accuracy: {oa['correct']}/{oa['total']}  ({oa['accuracy']:.1%})" if oa["accuracy"] is not None else "Overall accuracy: n/a")
    print()

    print("Accuracy by provider/model:")
    for pm, v in result["accuracy_by_provider_model"].items():
        acc = f"{v['accuracy']:.1%}" if v["accuracy"] is not None else "n/a"
        print(f"  {pm:40s} {v['correct']:3d}/{v['total']:3d}  {acc}")
    print()

    print(f"Low-confidence (flagged_for_human) field rows: {len(result['low_confidence_rows'])}")
    for row in result["low_confidence_rows"]:
        status = "correct" if row["correct"] else "WRONG"
        print(f"  [{status}] {row['app']} / {row['field']}: gold={row['gold']!r} predicted={row['predicted']!r}")
    print()

    print(f"All mismatches ({len(result['mismatches'])}):")
    for m in result["mismatches"]:
        print(
            f"  {m['app']:25s} {m['field']:18s} gold={m['gold']!r:30} predicted={m['predicted']!r:30} "
            f"(confidence={m['confidence']}, {m['provider']}/{m['model']})"
        )

    print()
    print(f"Saved {out_path}")


def _print_compare_table(title: str, report1: dict, report2: dict, label1: str, label2: str) -> None:
    print(title)
    print(f"  {'field':20s} {label1:>10s} {label2:>10s} {'change':>10s}")
    for field in (*COMPARE_FIELDS, "existing_mcp_url"):
        if field == "existing_mcp_url":
            print(f"  {field:20s} {'n/a':>10s} {'n/a':>10s} {'n/a':>10s}  (no gold ground truth - gold_labels.csv has no URL column)")
            continue
        v1 = report1["per_field_accuracy"][field] if field in report1["per_field_accuracy"] else {"accuracy": None}
        v2 = report2["per_field_accuracy"][field] if field in report2["per_field_accuracy"] else {"accuracy": None}
        a1, a2 = v1["accuracy"], v2["accuracy"]
        s1 = f"{a1:.1%}" if a1 is not None else "n/a"
        s2 = f"{a2:.1%}" if a2 is not None else "n/a"
        if a1 is not None and a2 is not None:
            change = f"{(a2 - a1) * 100:+.1f}pp"
        else:
            change = "n/a"
        print(f"  {field:20s} {s1:>10s} {s2:>10s} {change:>10s}")

    oa1, oa2 = report1["overall_accuracy"]["accuracy"], report2["overall_accuracy"]["accuracy"]
    s1 = f"{oa1:.1%}" if oa1 is not None else "n/a"
    s2 = f"{oa2:.1%}" if oa2 is not None else "n/a"
    change = f"{(oa2 - oa1) * 100:+.1f}pp" if oa1 is not None and oa2 is not None else "n/a"
    print(f"  {'overall':20s} {s1:>10s} {s2:>10s} {change:>10s}")
    print()


def run_compare(prefix1: str, prefix2: str) -> None:
    gold_rows = _load_gold()
    records1 = _load_records(record_path(prefix1))
    records2 = _load_records(record_path(prefix2))

    common_ids = {g["id"] for g in gold_rows if g["id"] in records1 and g["id"] in records2}
    common_gold = [g for g in gold_rows if g["id"] in common_ids]
    print(f"Gold apps present in BOTH {prefix1}.json and {prefix2}.json: {len(common_gold)} / {len(gold_rows)}")
    missing = [g["name"] for g in gold_rows if g["id"] not in common_ids]
    if missing:
        print(f"  Excluded (missing from one side): {', '.join(missing)}")
    print()

    report1 = score_against_gold(common_gold, records1)
    report2 = score_against_gold(common_gold, records2)
    _print_compare_table(
        f"Comparison on {len(common_gold)} shared gold apps:", report1, report2, prefix1, prefix2,
    )

    gemini_gold = [g for g in common_gold if records1[g["id"]].get("model_provider") == "gemini"]
    print(f"Of those, {len(gemini_gold)} had a {prefix1} record sourced from Gemini (model_provider == 'gemini', stored on every record): "
          f"{', '.join(g['name'] for g in gemini_gold) or '(none)'}")
    if gemini_gold:
        report1_gemini = score_against_gold(gemini_gold, records1)
        report2_gemini = score_against_gold(gemini_gold, records2)
        _print_compare_table(
            f"Comparison restricted to those {len(gemini_gold)} Gemini-sourced gold apps:",
            report1_gemini, report2_gemini, prefix1, prefix2,
        )
    else:
        print("  (skipping Gemini-only comparison table - no overlap)")
        print()

    regressions = []
    for gold in common_gold:
        gid = gold["id"]
        record1, record2 = records1[gid], records2[gid]
        fields_to_check = list(ALL_MAIN_FIELDS)
        if gold["existing_mcp"].strip().lower() != "not_checked":
            fields_to_check.append("existing_mcp_status")
        for field in fields_to_check:
            gold_value = _gold_value(gold, field)
            v1 = _predicted_value(record1, field)
            v2 = _predicted_value(record2, field)
            correct1 = _score_field(field, gold_value, v1)
            correct2 = _score_field(field, gold_value, v2)
            if correct1 and not correct2:
                regressions.append({
                    "app": gold["name"], "field": field,
                    f"{prefix1}_value": v1, f"{prefix2}_value": v2, "gold_value": gold_value,
                })

    print(f"Regressions ({prefix2} worse than {prefix1}): {len(regressions)}")
    for r in regressions:
        print(
            f"  {r['app']:25s} {r['field']:18s} "
            f"{prefix1}={r[f'{prefix1}_value']!r:30} {prefix2}={r[f'{prefix2}_value']!r:30} gold={r['gold_value']!r}"
        )
    if not regressions:
        print("  (none)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-prefix", type=str, default="pass1")
    parser.add_argument(
        "--compare", type=str, nargs=2, metavar=("PREFIX1", "PREFIX2"), default=None,
        help="Compare two output/<prefix>.json files on the gold apps present in both, e.g. --compare pass1 pass2",
    )
    args = parser.parse_args()

    if args.compare:
        run_compare(args.compare[0], args.compare[1])
    else:
        run_single(args.output_prefix)


if __name__ == "__main__":
    main()
