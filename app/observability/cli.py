"""CLI for Phase 6 debug views and prompt registry (JSON output)."""
from __future__ import annotations

import argparse
import json
import sys


def _get_session():
    """Return a sync session (init_db if needed, then session_scope)."""
    from app.db.session import init_db, session_scope
    init_db()
    return session_scope()


def cmd_doc_debug(args: argparse.Namespace) -> int:
    """Show pipeline status for a document (runs + stats)."""
    with _get_session() as session:
        from app.observability.debug_views import doc_debug
        out = doc_debug(args.doc_id, session)
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_chunk_view(args: argparse.Namespace) -> int:
    """Show chunk text, frames, mentions, and canonical mappings."""
    with _get_session() as session:
        from app.observability.debug_views import chunk_view
        out = chunk_view(args.doc_id, args.chunk_index, session)
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_mention_view(args: argparse.Namespace) -> int:
    """Show mention details, frame/chunk context, canonical mapping."""
    with _get_session() as session:
        from app.observability.debug_views import mention_view
        out = mention_view(args.mention_id, session)
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_canonical_view(args: argparse.Namespace) -> int:
    """Show canonical details, aliases, object_states, and mapped mentions."""
    with _get_session() as session:
        from app.observability.debug_views import canonical_view
        out = canonical_view(args.canonical_id, session)
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_prompt_template(args: argparse.Namespace) -> int:
    """Print prompt template text for a prompt_version."""
    from app.observability.prompt_registry import get_prompt_template
    text = get_prompt_template(args.prompt_version)
    if text is None:
        print(json.dumps({"error": "unknown_prompt_version", "prompt_version": args.prompt_version}), file=sys.stderr)
        return 1
    print(text)
    return 0


def cmd_ingest_file(args: argparse.Namespace) -> int:
    """Ingest a file into the DB (workspace, document, chunks). Returns doc_id for export-for-llm."""
    with _get_session() as session:
        from app.extraction.ingest_file import ingest_file
        doc_id = ingest_file(
            session,
            args.path,
            workspace_name=args.workspace,
        )
    print(json.dumps({"doc_id": doc_id, "path": str(args.path)}))
    return 0


def cmd_export_for_llm(args: argparse.Namespace) -> int:
    """Prepare prompts and doc parts for manual LLM run. Run the LLM yourself, then use ingest-results."""
    with _get_session() as session:
        from pathlib import Path
        from app.extraction.big_extract_runner import prepare_big_llm_export
        run_id, part_paths, manifest_path = prepare_big_llm_export(
            args.doc_id,
            args.model,
            output_dir=Path(args.output_dir),
            session=session,
        )
    out = {
        "run_id": str(run_id),
        "doc_id": args.doc_id,
        "model": args.model,
        "manifest": str(manifest_path),
        "parts": [str(p) for p in part_paths],
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_ingest_results(args: argparse.Namespace) -> int:
    """Ingest LLM result files from a manual run. path = dir (part_0.json, ...) or single JSON file."""
    with _get_session() as session:
        from pathlib import Path
        from app.extraction.big_extract_runner import ingest_big_llm_results
        from app.db.models.extraction import ExtractionRun
        ingest_big_llm_results(
            args.run_id,
            Path(args.path),
            session=session,
            remap_to_run_doc=getattr(args, "remap_to_run_doc", False),
        )
        run_entity = session.get(ExtractionRun, args.run_id)
        if not run_entity:
            print(json.dumps({"run_id": args.run_id, "error": "run_not_found_after_ingest"}))
            return 1
        stats = {}
        if run_entity.stats_json:
            try:
                stats = json.loads(run_entity.stats_json)
            except json.JSONDecodeError:
                stats = {"_raw_preview": (run_entity.stats_json or "")[:500]}
        out = {
            "run_id": args.run_id,
            "doc_id": run_entity.document_id,
            "status": run_entity.status,
            "stats": stats,
        }
        if run_entity.status == "FAILED":
            out["hint"] = "Check stats.parse_errors, stats.validation_errors, or stats.evidence_errors. Fix LLM output and re-run ingest-results with new JSON files."
        else:
            out["hint"] = "Run: python -m app.observability.cli doc-debug " + run_entity.document_id
        print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_ingest_file_and_json(args: argparse.Namespace) -> int:
    """Ingest file (Docling chunks) + saved JSON: remap doc/chunk IDs by position and ingest. Reuse costly LLM output."""
    with _get_session() as session:
        from pathlib import Path
        from app.extraction.big_extract_runner import ingest_file_and_json
        from app.db.models.extraction import ExtractionRun
        try:
            doc_id, run_id = ingest_file_and_json(
                args.file_path,
                args.json_path,
                session=session,
                workspace_name=args.workspace,
                model_id=args.model,
            )
        except (FileNotFoundError, ValueError) as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            return 1
        run_entity = session.get(ExtractionRun, str(run_id))
        stats = {}
        if run_entity and run_entity.stats_json:
            try:
                stats = json.loads(run_entity.stats_json)
            except json.JSONDecodeError:
                stats = {"_raw_preview": (run_entity.stats_json or "")[:500]}
        out = {
            "doc_id": doc_id,
            "run_id": str(run_id),
            "status": run_entity.status if run_entity else "SUCCEEDED",
            "stats": stats,
        }
        print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_run_canonicalization(args: argparse.Namespace) -> int:
    """Run full canonicalization pipeline: bootstrap Qdrant collections, then actors -> objects -> states -> actions."""
    import asyncio
    from app.db.document_helpers import workspace_id_for_document
    from app.canonical.bootstrap import bootstrap_canonical_collections
    from app.canonical.runners import run_canonicalization_for_document
    from app.embeddings.ollama import OllamaEmbedClient
    from app.embeddings.settings import EmbedSettings
    from app.vectorstore.client import build_qdrant_client
    from app.llm.service import LLMService
    from app.llm.settings import LLMSettings
    from app.canonical.llm_adapter import LLMServiceComparatorAdapter

    async def _run(doc_id: str, session) -> dict:
        await bootstrap_canonical_collections()
        embed_client = OllamaEmbedClient(EmbedSettings())
        qdrant_client = build_qdrant_client()
        llm_service = LLMService(LLMSettings())
        llm_client = LLMServiceComparatorAdapter(llm_service)
        workspace_id = workspace_id_for_document(doc_id, session)
        result = await run_canonicalization_for_document(
            doc_id,
            session=session,
            embed_client=embed_client,
            qdrant_client=qdrant_client,
            llm_client=llm_client,
            workspace_id=workspace_id,
        )
        return result

    with _get_session() as session:
        try:
            run_ids = asyncio.run(_run(args.doc_id, session))
        except Exception as e:
            print(json.dumps({"error": str(e)}, indent=2), file=sys.stderr)
            return 1
    print(json.dumps({"doc_id": args.doc_id, "run_ids": run_ids}, indent=2))
    return 0


def cmd_run_extract(args: argparse.Namespace) -> int:
    """Run big-LLM extraction for a document using a real LLM (Ollama/Gemini)."""
    with _get_session() as session:
        from app.extraction.big_extract_runner import run_big_llm_extract
        from app.extraction.real_llm_client import make_real_client

        client = make_real_client(
            model_id=args.model,
            timeout_s=args.timeout,
        )
        run_id = run_big_llm_extract(
            args.doc_id,
            client.model_id,
            session=session,
            llm_client=client,
        )
    print(json.dumps({"run_id": str(run_id), "doc_id": args.doc_id, "model": client.model_id}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="observability", description="Phase 6 debug and prompt inspection")
    sub = parser.add_subparsers(dest="command", required=True)

    doc_p = sub.add_parser("doc-debug", help="Pipeline status for a document")
    doc_p.add_argument("doc_id", help="Document ID")
    doc_p.set_defaults(func=cmd_doc_debug)

    chunk_p = sub.add_parser("chunk-view", help="Chunk text, frames, mentions, canonical mappings")
    chunk_p.add_argument("doc_id", help="Document ID")
    chunk_p.add_argument("chunk_index", type=int, help="Chunk index")
    chunk_p.set_defaults(func=cmd_chunk_view)

    mention_p = sub.add_parser("mention-view", help="Mention details and canonical mapping")
    mention_p.add_argument("mention_id", help="Mention ID")
    mention_p.set_defaults(func=cmd_mention_view)

    canonical_p = sub.add_parser("canonical-view", help="Canonical details and mapped mentions")
    canonical_p.add_argument("canonical_id", help="Canonical ID")
    canonical_p.set_defaults(func=cmd_canonical_view)

    prompt_p = sub.add_parser("prompt-template", help="Get prompt template text for a version")
    prompt_p.add_argument("prompt_version", help="e.g. big_extract_v1, local_compare_v1")
    prompt_p.set_defaults(func=cmd_prompt_template)

    ingest_p = sub.add_parser("ingest-file", help="Ingest a file into DB (document + chunks). For manual LLM path.")
    ingest_p.add_argument("path", help="Path to PDF/document file")
    ingest_p.add_argument("--workspace", default="manual", help="Workspace name (default: manual)")
    ingest_p.set_defaults(func=cmd_ingest_file)

    export_p = sub.add_parser("export-for-llm", help="Export prompts + doc parts for manual LLM run")
    export_p.add_argument("doc_id", help="Document ID (from ingest-file)")
    export_p.add_argument("output_dir", help="Directory to write part_0.txt, part_1.txt, manifest.json")
    export_p.add_argument("--model", default="manual", help="Model id for manifest (default: manual)")
    export_p.set_defaults(func=cmd_export_for_llm)

    ingest_res_p = sub.add_parser("ingest-results", help="Ingest LLM result files (part_0.json, ...) from manual run")
    ingest_res_p.add_argument("run_id", help="Run ID from export-for-llm output")
    ingest_res_p.add_argument("path", help="Directory with part_N.json or single JSON file")
    ingest_res_p.add_argument(
        "--remap-to-run-doc",
        action="store_true",
        help="Rewrite doc_id and chunk_ids in JSON to match this run's document (use when JSON was for a different doc, e.g. same file re-ingested)",
    )
    ingest_res_p.set_defaults(func=cmd_ingest_results)

    ingest_file_json_p = sub.add_parser(
        "ingest-file-and-json",
        help="Ingest file (Docling chunks) + saved big-extract JSON; remap IDs by position. Reuse costly LLM output.",
    )
    ingest_file_json_p.add_argument("file_path", help="Path to document (e.g. large_tz.md)")
    ingest_file_json_p.add_argument(
        "json_path",
        help="Path to JSON: single file (one part or array of parts), or dir with part_0.json, ...",
    )
    ingest_file_json_p.add_argument("--workspace", default="manual", help="Workspace name (default: manual)")
    ingest_file_json_p.add_argument("--model", default="saved", help="Model id for run (default: saved)")
    ingest_file_json_p.set_defaults(func=cmd_ingest_file_and_json)

    run_extract_p = sub.add_parser("run-extract", help="Run big-LLM extraction with real LLM (Ollama/Gemini)")
    run_extract_p.add_argument("doc_id", help="Document ID (must have chunks in DB)")
    run_extract_p.add_argument("--model", default=None, help="Model id (default: LLM_OLLAMA_MODEL or ollama/llama3.2)")
    run_extract_p.add_argument("--timeout", type=float, default=300.0, help="Request timeout in seconds")
    run_extract_p.set_defaults(func=cmd_run_extract)

    run_canon_p = sub.add_parser(
        "run-canonicalization",
        help="Run canonicalization (actors, objects, object_states, actions) with Qdrant + local LLM",
    )
    run_canon_p.add_argument("doc_id", help="Document ID (must have frames/mentions from extraction)")
    run_canon_p.add_argument("--force", action="store_true", help="Re-process all mentions (default: only unmapped)")
    run_canon_p.set_defaults(func=cmd_run_canonicalization)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
