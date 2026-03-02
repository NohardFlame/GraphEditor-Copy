"""CLI harness for MVP orchestrator: ingest, inspect."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import func, select

from app.db.models.pipeline_run import ChunkRun, PipelineRun
from app.db.session import init_db, session_scope
from app.orchestrator.orchestrator import MVPOrchestrator


def _print_ingest_summary(run_id: str) -> None:
    with session_scope() as session:
        run = session.get(PipelineRun, run_id)
        if not run:
            print(f"run_id={run_id}")
            return
        counts = (
            session.execute(
                select(ChunkRun.status, func.count(ChunkRun.id))
                .where(ChunkRun.run_id == run_id)
                .group_by(ChunkRun.status)
            )
        ).all()
        by_status = {s: c for s, c in counts}
        # Stage 1: SUCCESS, SUCCESS_WITH_WARNINGS, CACHED = done; FAILED, ERROR = errors
        done = sum(by_status.get(s, 0) for s in ("DONE", "SUCCESS", "SUCCESS_WITH_WARNINGS", "CACHED"))
        errors = sum(by_status.get(s, 0) for s in ("ERROR", "FAILED"))
        total = sum(by_status.values())
        print(f"run_id={run_id}")
        print(f"document_id={run.document_id}")
        print(f"chunks total={total}, done={done}, errors={errors}")


def _cmd_ingest(args: argparse.Namespace) -> int:
    init_db()
    orch = MVPOrchestrator()
    file_path = Path(args.file).resolve()
    if not file_path.exists():
        print(f"Error: file not found: {file_path}", file=sys.stderr)
        return 1

    async def run() -> str:
        return await orch.run_file_ingestion(
            workspace_id=args.workspace,
            file_path=file_path,
            force_reprocess=args.force,
        )

    try:
        run_id = asyncio.run(run())
        _print_ingest_summary(run_id)
        print(f"\nInspect: python -m app.orchestrator.cli inspect --run {run_id}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _cmd_stage2(args: argparse.Namespace) -> int:
    """Run Stage-2 normalization on a Stage-1 run."""
    init_db()
    from app.stage2.runner import run_stage2

    try:
        result = run_stage2(
            args.run,
            args.model_id,
            collection_name=args.collection or "stage1_cards",
            timeout_s=args.timeout,
            max_tokens=args.max_tokens,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if result.get("error"):
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1
    print(f"stage2_run_id={result.get('stage2_run_id')}")
    print(f"status={result.get('status')}")
    if result.get("stats"):
        print(json.dumps(result["stats"], indent=2))
    return 0


def _cmd_stage3(args: argparse.Namespace) -> int:
    """Run Stage-3 assembly on a Stage-1 run (after Stage-2: build actor/object/state/action cards)."""
    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    init_db()
    from app.stage3.runner import run_stage3

    try:
        result = run_stage3(
            args.run,
            args.model_id,
            collection_name=args.collection or "stage3_cards",
            timeout_s=args.timeout,
            max_tokens=args.max_tokens,
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if result.get("error"):
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1
    print(f"stage3_run_id={result.get('stage3_run_id')}")
    print(f"status={result.get('status')}")
    if result.get("stats"):
        print(json.dumps(result["stats"], indent=2))
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    from app.db.repositories.pipeline_run_repo import ChunkExtractionRepo, PipelineRunRepo
    from app.db.session import session_scope

    init_db()
    run_id = args.run
    limit = args.limit

    with session_scope() as session:
        pipeline_repo = PipelineRunRepo()
        extraction_repo = ChunkExtractionRepo()
        run = pipeline_repo.get(session, run_id)
        if not run:
            print(f"Error: run not found: {run_id}", file=sys.stderr)
            return 1

        print(f"Run: {run_id}")
        print(f"  document_id={run.document_id}")
        print(f"  status={run.status}")
        print(f"  started_at={run.started_at}")
        print(f"  finished_at={run.finished_at}")

        extractions = extraction_repo.list_by_run(session, run_id, limit=limit)
        print(f"\nExtractions (limit={limit}):")
        for i, ex in enumerate(extractions):
            print(f"\n--- Chunk {ex.chunk_id} (prompt={ex.prompt_name}) ---")
            if ex.parsed_json:
                try:
                    parsed = json.loads(ex.parsed_json)
                    print(json.dumps(parsed, indent=2, ensure_ascii=False))
                except Exception:
                    print(ex.parsed_json[:500])
            elif ex.raw_text:
                print(ex.raw_text[:500])
            if ex.validation_error:
                print(f"  [validation_error: {ex.validation_error[:200]}]")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MVP Orchestrator CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ingest = sub.add_parser("ingest", help="Ingest a file: parse -> chunk -> Stage 1 extract (claims)")
    p_ingest.add_argument("--workspace", "-w", default="ws1", help="Workspace ID/name (default: ws1)")
    p_ingest.add_argument("--force", "-f", action="store_true", help="Force reprocess all chunks")
    p_ingest.add_argument("file", help="Path to PDF or other document")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_inspect = sub.add_parser("inspect", help="Inspect a run: show status and sample extractions")
    p_inspect.add_argument("--run", "-r", required=True, help="Pipeline run ID")
    p_inspect.add_argument("--limit", "-n", type=int, default=5, help="Max extractions to show (default: 5)")
    p_inspect.set_defaults(func=_cmd_inspect)

    p_stage2 = sub.add_parser("stage2", help="Run Stage-2 normalization on a Stage-1 run (dedupe + canonicalize claims)")
    p_stage2.add_argument("--run", "-r", required=True, help="Stage-1 pipeline run ID to normalize")
    p_stage2.add_argument("--model-id", "-m", default="ollama/llama3.2", help="LLM model (default: ollama/llama3.2)")
    p_stage2.add_argument("--collection", default=None, help="Qdrant collection (default: stage1_cards)")
    p_stage2.add_argument("--timeout", type=float, default=120.0, help="LLM timeout seconds (default: 120)")
    p_stage2.add_argument("--max-tokens", type=int, default=2048, help="Max output tokens (default: 2048)")
    p_stage2.set_defaults(func=_cmd_stage2)

    p_stage3 = sub.add_parser("stage3", help="Run Stage-3 assembly on a Stage-1 run (build cards from canonicals)")
    p_stage3.add_argument("--run", "-r", required=True, help="Stage-1 pipeline run ID (after Stage-2)")
    p_stage3.add_argument("--model-id", "-m", default="ollama/llama3.2", help="LLM model (default: ollama/llama3.2)")
    p_stage3.add_argument("--collection", default=None, help="Qdrant collection (default: stage3_cards)")
    p_stage3.add_argument("--timeout", type=float, default=120.0, help="LLM timeout seconds (default: 120)")
    p_stage3.add_argument("--max-tokens", type=int, default=2048, help="Max output tokens (default: 2048)")
    p_stage3.set_defaults(func=_cmd_stage3)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
