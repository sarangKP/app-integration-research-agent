"""Score output/pass1.json against the gold set (read-only).

The 20 gold labels were drafted with Claude from official docs,
independently of the agent's output, then spot-checked by a human against
the sources (15 of 20 verified; one error found and corrected - fanbasis).

Scored fields: auth_methods, access, api_type, buildability, main_blocker.
existing_mcp is scored only for gold rows where it was actually verified
(gold value != "not_checked"). api_breadth is not scored (PRD/user choice).

Matching rule: predicted == gold counts as correct, including when both
are "unknown" (e.g. Waterfall.io/access, where gold itself is "unknown" and
the agent honestly said the same). A predicted "unknown" only counts as a
miss when gold has a real, verified answer. auth_methods/api_type use exact
set matching, so an extra predicted item (e.g. HubSpot predicting an extra
"other" auth method) counts as a miss - per_field_accuracy_loose_subset
reports the looser "gold subset of predicted" variant alongside it.

Read-only: never writes to pass1.json or gold_labels.csv.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

PASS1_PATH = Path("output/pass1.json")
GOLD_PATH = Path("data/gold/gold_labels.csv")
OUT_PATH = Path("output/eval_pass1.json")

SET_FIELDS = ("auth_methods", "api_type")
SCALAR_FIELDS = ("access", "buildability", "main_blocker")
ALL_MAIN_FIELDS = SET_FIELDS + SCALAR_FIELDS


def _load_gold() -> list[dict]:
    with GOLD_PATH.open() as f:
        return list(csv.DictReader(f))


def _load_pass1() -> dict[str, dict]:
    return json.loads(PASS1_PATH.read_text())


def _is_unknown(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return value == ["unknown"] or len(value) == 0
    return str(value).strip().lower() == "unknown"


def _gold_set(value: str) -> set[str]:
    return {v.strip() for v in value.split("|") if v.strip()}


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
    present in predicted (extra items in predicted don't count against it).
    auth_methods/api_type use exact-set matching in the main rule above, so
    e.g. HubSpot predicting an extra 'other' auth method is a miss there -
    this shows what the score would be if only missing items counted."""
    gold_set = _gold_set(gold_value)
    if _is_unknown(predicted_value):
        return gold_set == {"unknown"}
    return gold_set.issubset(set(predicted_value))


def main() -> None:
    gold_rows = _load_gold()
    pass1 = _load_pass1()

    per_field: dict[str, dict[str, int]] = {f: {"correct": 0, "total": 0} for f in ALL_MAIN_FIELDS}
    per_field["existing_mcp"] = {"correct": 0, "total": 0}
    per_field_loose: dict[str, dict[str, int]] = {f: {"correct": 0, "total": 0} for f in SET_FIELDS}

    by_provider_model: dict[str, dict[str, int]] = defaultdict(lambda: {"correct": 0, "total": 0})

    mismatches: list[dict] = []
    low_confidence_rows: list[dict] = []
    skipped: list[dict] = []
    scored_app_count = 0

    for gold in gold_rows:
        gid = gold["id"]
        record = pass1.get(gid)
        if record is None:
            skipped.append({"id": gid, "name": gold["name"]})
            continue
        scored_app_count += 1

        provider_model = f"{record.get('model_provider', 'unknown')}/{record.get('model_name', 'unknown')}"
        confidence = record.get("confidence", {})

        fields_to_score = list(ALL_MAIN_FIELDS)
        if gold["existing_mcp"].strip().lower() != "not_checked":
            fields_to_score.append("existing_mcp")

        for field in fields_to_score:
            gold_value = gold[field] if field != "existing_mcp" else gold["existing_mcp"]
            if field == "existing_mcp":
                predicted_value = record.get("existing_mcp", {}).get("status")
            else:
                predicted_value = record.get(field)

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
                mismatches.append(
                    {
                        "app": gold["name"],
                        "id": gid,
                        "field": field,
                        "gold": gold_value,
                        "predicted": predicted_value,
                        "confidence": confidence.get(field, "n/a"),
                        "provider": record.get("model_provider"),
                        "model": record.get("model_name"),
                    }
                )

            field_confidence = confidence.get(field)
            if field_confidence == "low":
                low_confidence_rows.append(
                    {
                        "app": gold["name"],
                        "id": gid,
                        "field": field,
                        "gold": gold_value,
                        "predicted": predicted_value,
                        "correct": correct,
                    }
                )

    overall_correct = sum(v["correct"] for v in per_field.values())
    overall_total = sum(v["total"] for v in per_field.values())

    def _acc(d: dict[str, int]) -> float | None:
        return round(d["correct"] / d["total"], 4) if d["total"] else None

    per_field_report = {f: {**v, "accuracy": _acc(v)} for f, v in per_field.items()}
    per_field_loose_report = {f: {**v, "accuracy": _acc(v)} for f, v in per_field_loose.items()}
    by_provider_model_report = {k: {**v, "accuracy": _acc(v)} for k, v in by_provider_model.items()}

    result = {
        "gold_total": len(gold_rows),
        "scored_apps": scored_app_count,
        "scored_apps_label": f"{scored_app_count}/{len(gold_rows)}",
        "skipped_missing_from_pass1": skipped,
        "per_field_accuracy": per_field_report,
        "per_field_accuracy_loose_subset": per_field_loose_report,
        "overall_accuracy": {
            "correct": overall_correct,
            "total": overall_total,
            "accuracy": round(overall_correct / overall_total, 4) if overall_total else None,
        },
        "accuracy_by_provider_model": by_provider_model_report,
        "low_confidence_rows": low_confidence_rows,
        "mismatches": mismatches,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2, default=str))

    print(f"Scored {result['scored_apps_label']} gold apps")
    if skipped:
        print(f"Skipped (missing from pass1.json): {', '.join(s['name'] for s in skipped)}")
    print()

    print("Per-field accuracy:")
    for field, v in per_field_report.items():
        acc = f"{v['accuracy']:.1%}" if v["accuracy"] is not None else "n/a (0 scored)"
        print(f"  {field:15s} {v['correct']:3d}/{v['total']:3d}  {acc}")
    print()

    print("Looser scoring for set fields (gold values all present in predicted, extra items don't count against it):")
    for field, v in per_field_loose_report.items():
        acc = f"{v['accuracy']:.1%}" if v["accuracy"] is not None else "n/a (0 scored)"
        print(f"  {field:15s} {v['correct']:3d}/{v['total']:3d}  {acc}")
    print()

    oa = result["overall_accuracy"]
    print(f"Overall accuracy: {oa['correct']}/{oa['total']}  ({oa['accuracy']:.1%})" if oa["accuracy"] is not None else "Overall accuracy: n/a")
    print()

    print("Accuracy by provider/model:")
    for pm, v in by_provider_model_report.items():
        acc = f"{v['accuracy']:.1%}" if v["accuracy"] is not None else "n/a"
        print(f"  {pm:40s} {v['correct']:3d}/{v['total']:3d}  {acc}")
    print()

    print(f"Low-confidence (flagged_for_human) field rows: {len(low_confidence_rows)}")
    for row in low_confidence_rows:
        status = "correct" if row["correct"] else "WRONG"
        print(f"  [{status}] {row['app']} / {row['field']}: gold={row['gold']!r} predicted={row['predicted']!r}")
    print()

    print(f"All mismatches ({len(mismatches)}):")
    for m in mismatches:
        print(
            f"  {m['app']:25s} {m['field']:15s} gold={m['gold']!r:30} predicted={m['predicted']!r:30} "
            f"(confidence={m['confidence']}, {m['provider']}/{m['model']})"
        )

    print()
    print(f"Saved {OUT_PATH}")


if __name__ == "__main__":
    main()
