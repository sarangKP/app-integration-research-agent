"""Compute pattern numbers from output/pass1.json with pandas (PRD §8).

Every number on the site comes from this script, never from the LLM.
Pass 2 and the final human-review pass were not run (out of time), so
this analyzes pass1 only - no "final" numbers exist to report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

PASS1_PATH = Path("output/pass1.json")
FAILURES_PATH = Path("output/pass1_failures.json")
APPS_CSV = Path("data/apps.csv")
OUT_PATH = Path("output/patterns.json")


def _load_df() -> pd.DataFrame:
    pass1 = json.loads(PASS1_PATH.read_text())
    rows = list(pass1.values())
    df = pd.DataFrame(rows)
    df["existing_mcp_status"] = df["existing_mcp"].apply(lambda m: m.get("status"))
    return df


def main() -> None:
    all_apps = pd.read_csv(APPS_CSV)
    total_apps = len(all_apps)

    df = _load_df()
    failures = json.loads(FAILURES_PATH.read_text()) if FAILURES_PATH.exists() else {}
    done_count = len(df)
    failed_count = len(failures)

    not_researched = [
        {"id": fid, "name": info["name"], "reason": info.get("reason", "exception")}
        for fid, info in failures.items()
    ]

    # auth method distribution: overall + per category
    auth_exploded = df.explode("auth_methods")
    auth_overall = auth_exploded["auth_methods"].value_counts().to_dict()
    auth_by_category = (
        auth_exploded.groupby(["category", "auth_methods"]).size().unstack(fill_value=0).to_dict(orient="index")
    )

    # access by category crosstab
    access_by_category = pd.crosstab(df["category"], df["access"]).to_dict(orient="index")

    # top blockers. "none" is excluded when picking the top *real* blocker -
    # on the gold set the agent wrote "none" where a real blocker existed
    # (DealCloud, Sherlock, Waterfall.io), so it's reported separately rather
    # than treated as just another blocker value.
    top_blockers = df["main_blocker"].value_counts().to_dict()
    no_blocker_count = int(top_blockers.get("none", 0))
    unknown_blocker_count = int(top_blockers.get("unknown", 0))
    real_blockers = {k: v for k, v in top_blockers.items() if k not in ("none", "unknown")}
    named_blocker_count = int(sum(real_blockers.values()))
    top_real_blocker_name, top_real_blocker_count = (
        max(real_blockers.items(), key=lambda kv: kv[1]) if real_blockers else (None, 0)
    )

    # api_type split
    api_type_exploded = df.explode("api_type")
    api_type_split = api_type_exploded["api_type"].value_counts().to_dict()

    # existing MCP count
    mcp_counts = df["existing_mcp_status"].value_counts().to_dict()

    # Ready and self-serve (NOT "easy wins" - the Composio catalog cross-check
    # (D5) was never run, in_composio_catalog is always False in every record,
    # so filtering on it would be meaningless, not a real signal).
    ready_self_serve_mask = (df["buildability"] == "ready") & (
        df["access"].isin(["self_serve_free", "self_serve_trial"])
    )
    ready_self_serve = df.loc[ready_self_serve_mask, ["id", "name", "category", "access"]].to_dict(orient="records")

    # needs outreach: partner_gated / contact_sales / app_review_required / admin_approval
    outreach_mask = df["access"].isin(["partner_gated", "contact_sales", "app_review_required", "admin_approval"])
    needs_outreach = df.loc[outreach_mask, ["id", "name", "category", "access", "main_blocker"]].to_dict(
        orient="records"
    )

    # Inconsistent: gated access (admin/app-review/sales) but rated ready anyway
    inconsistent_mask = outreach_mask & (df["buildability"] == "ready")
    inconsistent_buildability = df.loc[
        inconsistent_mask, ["id", "name", "category", "access", "buildability"]
    ].to_dict(orient="records")

    # not an API: api_type is a confirmed non-API type only (cli_only/sdk_only/
    # none), never "unknown" (not knowing the type isn't the same as
    # confirming there's no hosted API), and never mixed with a hosted type
    # (rest/graphql/soap/grpc/websocket) like HubSpot's ['rest', ..., 'cli_only'].
    non_api_types = {"cli_only", "sdk_only", "none"}
    not_an_api_mask = df["api_type"].apply(
        lambda types: isinstance(types, list) and len(types) > 0 and set(types).issubset(non_api_types)
    )
    not_an_api = df.loc[not_an_api_mask, ["id", "name", "category", "api_type", "main_blocker"]].to_dict(
        orient="records"
    )

    # buildable today - percentage is of researched apps (done_count), not
    # all 100 targeted, since the 5 unresearched apps have no buildability
    # value to count either way.
    buildable_ready = int((df["buildability"] == "ready").sum())
    buildable_pct = round(buildable_ready / done_count * 100, 1)

    # model/provider breakdown
    model_counts = df.groupby(["model_provider", "model_name"]).size().reset_index(name="count")
    model_breakdown = model_counts.to_dict(orient="records")

    # unknown/low-confidence field count (limitation)
    def _count_low(conf: dict) -> int:
        return sum(1 for v in conf.values() if v == "low")

    df["low_confidence_field_count"] = df["confidence"].apply(_count_low)
    apps_with_unknown = int((df["low_confidence_field_count"] > 0).sum())
    total_unknown_fields = int(df["low_confidence_field_count"].sum())

    patterns = {
        "total_apps": total_apps,
        "done_count": done_count,
        "failed_count": failed_count,
        "not_researched": not_researched,
        "buildable_ready_count": buildable_ready,
        "buildable_pct": buildable_pct,
        "auth_overall": auth_overall,
        "auth_by_category": auth_by_category,
        "access_by_category": access_by_category,
        "top_blockers": top_blockers,
        "no_blocker_count": no_blocker_count,
        "unknown_blocker_count": unknown_blocker_count,
        "named_blocker_count": named_blocker_count,
        "top_real_blocker_name": top_real_blocker_name,
        "top_real_blocker_count": top_real_blocker_count,
        "api_type_split": api_type_split,
        "existing_mcp_counts": mcp_counts,
        "ready_self_serve": ready_self_serve,
        "needs_outreach": needs_outreach,
        "inconsistent_buildability": inconsistent_buildability,
        "not_an_api": not_an_api,
        "model_breakdown": model_breakdown,
        "apps_with_unknown_fields": apps_with_unknown,
        "total_unknown_low_confidence_fields": total_unknown_fields,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(patterns, indent=2, default=str))
    print(f"Saved {OUT_PATH}")
    print(f"total_apps={total_apps} done={done_count} failed={failed_count} buildable_pct={buildable_pct}%")


if __name__ == "__main__":
    main()
