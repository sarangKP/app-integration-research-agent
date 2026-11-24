"""CLI entry: run the research pipeline over data/apps.csv (or a subset
picked by --apps-file, e.g. data/pass2_apps.json from pick_pass2_apps.py,
optionally narrowed further with --only).

Resumable: apps already present in output/<output-prefix>.json are
skipped. --output-prefix (default "pass1") controls which output file
pair (output/<prefix>.json, output/<prefix>_failures.json) is read AND
written - a --output-prefix pass2 run never opens pass1.json for writing,
and its completed-app check only looks at pass2.json, so apps already done
in pass 1 are re-run under pass 2 rather than skipped.

Stops cleanly (saving progress) on a Tavily/Firecrawl credit hard stop, a
Groq daily-token/request hard stop, or - for the Gemini-backed
verify/retry pass - a Gemini quota exhaustion (3 failed 60s-apart retries
on a 429). LLM_PROVIDER in .env picks the backend; Gemini gets its own
request/token pacing and daily-usage counter in llm.py, with no fallback
model.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from composio import Composio
from dotenv import load_dotenv

from research_agent.config import load_config
from research_agent.graph import Deps, build_graph
from research_agent.llm import AllProvidersExhausted, GeminiQuotaExhausted, build_extraction_llm
from research_agent.tools import CreditLimitReached

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
logger = logging.getLogger("run")

APPS_CSV = Path("data/apps.csv")
LOCK_PATH = Path("output/run.lock")


def output_paths(output_prefix: str) -> tuple[Path, Path]:
    """Every output path is derived from output_prefix, computed fresh here
    - never read off a module-level pass1-named constant - so a
    --output-prefix pass2 run has no code path that can touch pass1.json."""
    return Path(f"output/{output_prefix}.json"), Path(f"output/{output_prefix}_failures.json")


def load_apps() -> list[dict]:
    with APPS_CSV.open() as f:
        return list(csv.DictReader(f))


def load_existing(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save_json(path: Path, data: dict) -> None:
    """Atomic write: write to a temp file, then rename over the target.
    Prevents a reader from ever seeing a half-written file, and (combined
    with the run.lock below) a second process from clobbering this one's
    progress."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, path)


def acquire_lock() -> None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        content = LOCK_PATH.read_text().strip()
        try:
            pid = int(content)
        except ValueError:
            pid = None
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                logger.warning("Stale lock file (pid %d not running), taking over.", pid)
            else:
                raise RuntimeError(
                    f"Another research run (pid {pid}) appears to be active "
                    f"({LOCK_PATH}). Refusing to start a second writer."
                )
    LOCK_PATH.write_text(str(os.getpid()))


def release_lock() -> None:
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text().strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except OSError:
        pass


def run_one(graph, app: dict) -> dict:
    initial_state = {
        "id": int(app["id"]),
        "name": app["name"],
        "category": app["category"],
        "hint": app["hint"],
    }
    result = graph.invoke(initial_state)
    return result["record"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apps", type=str, default=None, help="Comma-separated app names")
    parser.add_argument(
        "--apps-file", type=str, default=None,
        help="Path to a JSON file of [{'id': ..., ...}, ...] (e.g. data/pass2_apps.json) to restrict the run to",
    )
    parser.add_argument(
        "--only", type=str, default=None,
        help="Comma-separated app names (case-insensitive) to trial, applied on top of --apps-file/--apps. "
             "Errors out if any name isn't found in the already-selected app list.",
    )
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Cap the selected app list to the first N (apply a larger N on each resumed run to do batches)")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--output-prefix", type=str, default="pass1",
        help="Output files are output/<prefix>.json and output/<prefix>_failures.json. "
             "Completed-app skipping only looks at THIS file, not pass1.json, when prefix != pass1.",
    )
    parser.add_argument(
        "--gemini-used-today", type=int, default=0,
        help="Starting count for today's Gemini usage (e.g. from AI Studio), if other projects share the same key. "
             "Added on top of this process's own tracked count when printing 'Gemini calls used today'.",
    )
    args = parser.parse_args()

    acquire_lock()
    try:
        _main(args)
    finally:
        release_lock()


def _main(args) -> None:
    config = load_config()
    pass_path, failures_path = output_paths(args.output_prefix)

    all_apps = load_apps()
    if args.apps_file:
        wanted_ids = {str(item["id"]) for item in json.loads(Path(args.apps_file).read_text())}
        all_apps = [a for a in all_apps if a["id"] in wanted_ids]
    if args.apps:
        wanted = {n.strip().lower() for n in args.apps.split(",")}
        all_apps = [a for a in all_apps if a["name"].strip().lower() in wanted]
    if args.only:
        wanted_names = [n.strip() for n in args.only.split(",")]
        wanted_lower = {n.lower() for n in wanted_names}
        matched = [a for a in all_apps if a["name"].strip().lower() in wanted_lower]
        matched_lower = {a["name"].strip().lower() for a in matched}
        missing = [n for n in wanted_names if n.lower() not in matched_lower]
        if missing:
            raise RuntimeError(
                f"--only: app name(s) not found in the already-selected app list: {', '.join(missing)}"
            )
        print(f"--only matched {len(matched)} app(s): {', '.join(a['name'] for a in matched)}", flush=True)
        all_apps = matched
    if args.limit:
        all_apps = all_apps[: args.limit]

    pass1 = load_existing(pass_path)
    failures = load_existing(failures_path)

    todo = [a for a in all_apps if a["id"] not in pass1]
    logger.info(
        "Output prefix: %s (%s / %s). Total apps selected: %d, already done: %d, remaining: %d",
        args.output_prefix, pass_path, failures_path, len(all_apps), len(all_apps) - len(todo), len(todo),
    )

    composio_client = Composio()
    llm = build_extraction_llm(config)
    fallback = config.groq_fallback_model if config.llm_provider == "groq" else "none"
    print(f"[startup] provider={llm.provider} model={llm.model_name} fallback={fallback}", flush=True)
    logger.info("[startup] provider=%s model=%s fallback=%s", llm.provider, llm.model_name, fallback)

    from research_agent.tools import CreditCounter

    credits = CreditCounter.load(tavily_limit=config.tavily_credit_limit, firecrawl_limit=config.firecrawl_credit_limit)
    deps = Deps(
        composio_client=composio_client,
        tavily_api_key=config.tavily_api_key,
        firecrawl_api_key=config.firecrawl_api_key,
        credits=credits,
        extraction_llm=llm,
        use_cache=not args.no_cache,
    )
    graph = build_graph(deps)

    stopped_early = False
    done_count = 0
    total = len(todo)

    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        futures = {pool.submit(run_one, graph, app): app for app in todo}
        for future in as_completed(futures):
            app = futures[future]
            done_count += 1
            try:
                record = future.result()
                pass1[app["id"]] = record
                failures.pop(app["id"], None)
                low_confidence_fields = sum(1 for v in record.get("confidence", {}).values() if v == "low")
                logger.info(
                    "[%d/%d] %s (id=%s): done, %d unknown/low-confidence field(s). Tavily credits: %d/%d",
                    done_count, total, app["name"], app["id"], low_confidence_fields,
                    credits.tavily_used, credits.tavily_limit,
                )
                if config.llm_provider == "gemini":
                    print(f"Gemini calls used today: {llm.requests_today + args.gemini_used_today} / 500", flush=True)
            except CreditLimitReached as exc:
                logger.error("CREDIT LIMIT REACHED: %s. Pausing run, saving progress.", exc)
                failures[app["id"]] = {"name": app["name"], "error": str(exc), "reason": "credit_limit"}
                stopped_early = True
                save_json(pass_path, pass1)
                save_json(failures_path, failures)
                for f in futures:
                    f.cancel()
                break
            except AllProvidersExhausted as exc:
                remaining = total - done_count
                logger.error(
                    "GROQ DAILY LIMIT REACHED: %s. %d app(s) remaining this run. Pausing, saving progress.",
                    exc, remaining,
                )
                failures[app["id"]] = {"name": app["name"], "error": str(exc), "reason": "llm_exhausted"}
                stopped_early = True
                save_json(pass_path, pass1)
                save_json(failures_path, failures)
                for f in futures:
                    f.cancel()
                break
            except GeminiQuotaExhausted as exc:
                remaining = total - done_count
                logger.error(
                    "GEMINI QUOTA REACHED: %s. %d app(s) remaining this run. Pausing, saving progress.",
                    exc, remaining,
                )
                print("Gemini quota reached, resume later", flush=True)
                failures[app["id"]] = {"name": app["name"], "error": str(exc), "reason": "llm_exhausted"}
                stopped_early = True
                save_json(pass_path, pass1)
                save_json(failures_path, failures)
                for f in futures:
                    f.cancel()
                break
            except Exception as exc:
                logger.exception("[%d/%d] %s (id=%s): FAILED", done_count, total, app["name"], app["id"])
                failures[app["id"]] = {"name": app["name"], "error": repr(exc), "reason": "exception"}

            save_json(pass_path, pass1)
            save_json(failures_path, failures)

    logger.info(
        "Run finished. %d/%d apps processed this run. Tavily: %d/%d, Firecrawl: %d/%d. Failures: %d",
        done_count, total, credits.tavily_used, credits.tavily_limit,
        credits.firecrawl_used, credits.firecrawl_limit, len(failures),
    )
    if stopped_early:
        remaining_total = len(load_apps()) - len(load_existing(pass_path))
        logger.error(
            "STOPPED EARLY: hard stop reached. %d app(s) still not done overall. "
            "Re-run the same command later to resume.",
            remaining_total,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
