"""CLI entry: run the pass1 (extract-only) pipeline over data/apps.csv.

Resumable: apps already present in output/pass1.json are skipped. Stops
cleanly (saving progress) if the Tavily/Firecrawl credit hard stop or the
Groq daily-token/request hard stop is hit. Requires LLM_PROVIDER=groq: with
Gemini's free-tier daily quota exhausted, pass1 only ever calls Groq - there
is no Gemini fallback path reachable from this entry point.
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
from research_agent.llm import AllProvidersExhausted, GroqExtractionLLM
from research_agent.tools import CreditLimitReached

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
logger = logging.getLogger("run")

APPS_CSV = Path("data/apps.csv")
PASS1_PATH = Path("output/pass1.json")
FAILURES_PATH = Path("output/pass1_failures.json")
LOCK_PATH = Path("output/run.lock")


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
                    f"({LOCK_PATH}). Refusing to start a second writer to pass1.json."
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
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    acquire_lock()
    try:
        _main(args)
    finally:
        release_lock()


def _main(args) -> None:
    config = load_config()
    if config.llm_provider != "groq":
        raise RuntimeError(
            f"Pass 1 requires LLM_PROVIDER=groq (Gemini's free-tier daily quota is "
            f"exhausted). Got LLM_PROVIDER={config.llm_provider!r}. Set LLM_PROVIDER=groq "
            f"in .env before running."
        )

    all_apps = load_apps()
    if args.apps:
        wanted = {n.strip().lower() for n in args.apps.split(",")}
        all_apps = [a for a in all_apps if a["name"].strip().lower() in wanted]
    if args.limit:
        all_apps = all_apps[: args.limit]

    pass1 = load_existing(PASS1_PATH)
    failures = load_existing(FAILURES_PATH)

    todo = [a for a in all_apps if a["id"] not in pass1]
    logger.info(
        "Total apps selected: %d, already done: %d, remaining: %d",
        len(all_apps), len(all_apps) - len(todo), len(todo),
    )

    composio_client = Composio()
    llm = GroqExtractionLLM(config)
    print(f"[startup] provider={llm.provider} model={llm.model_name} fallback={config.groq_fallback_model}", flush=True)
    logger.info("[startup] provider=%s model=%s fallback=%s", llm.provider, llm.model_name, config.groq_fallback_model)

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
            except CreditLimitReached as exc:
                logger.error("CREDIT LIMIT REACHED: %s. Pausing run, saving progress.", exc)
                failures[app["id"]] = {"name": app["name"], "error": str(exc), "reason": "credit_limit"}
                stopped_early = True
                save_json(PASS1_PATH, pass1)
                save_json(FAILURES_PATH, failures)
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
                save_json(PASS1_PATH, pass1)
                save_json(FAILURES_PATH, failures)
                for f in futures:
                    f.cancel()
                break
            except Exception as exc:
                logger.exception("[%d/%d] %s (id=%s): FAILED", done_count, total, app["name"], app["id"])
                failures[app["id"]] = {"name": app["name"], "error": repr(exc), "reason": "exception"}

            save_json(PASS1_PATH, pass1)
            save_json(FAILURES_PATH, failures)

    logger.info(
        "Run finished. %d/%d apps processed this run. Tavily: %d/%d, Firecrawl: %d/%d. Failures: %d",
        done_count, total, credits.tavily_used, credits.tavily_limit,
        credits.firecrawl_used, credits.firecrawl_limit, len(failures),
    )
    if stopped_early:
        remaining_total = len(load_apps()) - len(load_existing(PASS1_PATH))
        logger.error(
            "STOPPED EARLY: hard stop reached. %d app(s) still not done overall. "
            "Re-run the same command later to resume.",
            remaining_total,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
