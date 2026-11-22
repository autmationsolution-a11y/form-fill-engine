"""Command-line interface: ``formfill <command>``.

Defaults point at the bundled samples so the quickstart works from a fresh clone. Override
with flags or the ``FORMFILL_MAPPINGS``, ``FORMFILL_DATA`` and ``FORMFILL_WORKDIR`` variables.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .adapters import open_source
from .engine import FillReport, check_mapping, prepare, unmapped_fields
from .registry import TemplateRegistry
from .review.store import ReviewError

_MARK = {
    "filled": "ok ", "doubtful": "?? ", "missing": "!! ", "invalid": "XX ",
    "empty": " - ", "skipped": " ~ ",
}  # fmt: skip


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _print_report(report: FillReport, out) -> None:
    print(
        f"template {report.template_id} v{report.template_version}  client {report.client_id}",
        file=out,
    )
    width = max((len(f.pdf_field) for f in report.fields), default=10)
    for f in report.fields:
        if f.type == "checkbox" and f.output_value is not None:
            shown = "[x]" if f.output_value else "[ ]"
        else:
            shown = f.output_value if f.output_value is not None else ""
        line = f"  {_MARK[f.status.value]}{f.pdf_field:<{width}}  {f.status.value:<8}  {shown}"
        print(line.rstrip(), file=out)
        for issue in f.issues:
            print(f"  {'':3}{'':<{width}}  {issue.severity}: {issue.message}", file=out)
    for issue in report.rule_issues:
        print(f"  rule {issue.code} ({issue.severity}): {issue.message}", file=out)
    summary = ", ".join(f"{n} {k}" for k, n in report.summary().items() if n)
    verdict = "BLOCKED: fix source data before approval" if report.blocking else "ready for review"
    print(f"  summary: {summary}. {verdict}", file=out)


def cmd_list_templates(args) -> int:
    registry = TemplateRegistry.from_directory(args.mappings)
    if not registry.templates and not registry.errors:
        print(f"no mapping files in {args.mappings}")
        return 1
    for t in registry.list():
        state = "approved" if t.is_approved else t.status
        pdf_ok = "pdf ok" if t.pdf_path.exists() else "PDF MISSING"
        print(
            f"{t.template_id:<32} v{t.version:<4} {state:<9} {len(t.fields):>3} fields  "
            f"{pdf_ok}  {t.provider}: {t.title}"
        )
    for name, error in registry.errors.items():
        print(f"ERROR {name}: {error}", file=sys.stderr)
    return 1 if registry.errors else 0


def _today(args) -> date | None:
    return date.fromisoformat(args.today) if args.today else None


def cmd_validate(args) -> int:
    registry = TemplateRegistry.from_directory(args.mappings)
    mapping = registry.get(args.template)
    problems = check_mapping(mapping)
    for p in problems:
        print(f"mapping problem: {p}", file=sys.stderr)
    unmapped = unmapped_fields(mapping)
    if unmapped:
        print(
            f"note: PDF fields left for a person to complete: {', '.join(unmapped)}",
            file=sys.stderr,
        )
    if problems:
        return 1
    if not args.client:
        print(f"mapping {mapping.template_id} matches its PDF ({len(mapping.fields)} fields)")
        return 0
    source = open_source(args.data)
    report = prepare(mapping, source.get_client(args.client), args.client, today=_today(args))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report(report, sys.stdout)
    return 1 if report.blocking else 0


def cmd_fill(args) -> int:
    from .review.store import PackageStore

    registry = TemplateRegistry.from_directory(args.mappings)
    mapping = registry.get(args.template)
    source = open_source(args.data)
    report = prepare(mapping, source.get_client(args.client), args.client, today=_today(args))
    _print_report(report, sys.stdout)
    store = PackageStore(args.workdir)
    pkg = store.create(mapping, report, prepared_by=args.prepared_by, source=source.describe())
    print(f"package {pkg.id} created, status {pkg.status}: {pkg.pdf_path}")
    print("next: formfill serve, then review and approve or reject in the browser")
    return 0


def cmd_suggest_mapping(args) -> int:
    from .mapping_assist import SUGGESTERS, draft_mapping, write_draft

    pdf_path = Path(args.pdf).resolve()
    source = open_source(args.data)
    client_id = args.client or source.list_clients()[0]
    suggester = SUGGESTERS[args.provider]()
    out = Path(args.out) if args.out else Path(args.mappings) / "_drafts" / f"{pdf_path.stem}.yaml"
    pdf_ref = Path(os.path.relpath(pdf_path, out.parent.resolve())).as_posix()
    draft = draft_mapping(
        pdf_path,
        source.get_client(client_id),
        suggester,
        template_id=args.template_id,
        pdf_ref=pdf_ref,
    )
    write_draft(draft, out)
    matched = sum(1 for f in draft["fields"] if f.get("source"))
    print(
        f"draft written to {out}: {matched}/{len(draft['fields'])} fields matched "
        f"by {suggester.name}. It is NOT active."
    )
    print(f"review it, then: formfill approve-mapping {out} --by <your name>")
    return 0


def cmd_approve_mapping(args) -> int:
    from .mapping_assist import approve_draft
    from .pdf import sha256_file
    from .review.audit import AuditLog

    target = approve_draft(Path(args.draft), args.by, Path(args.mappings))
    AuditLog(Path(args.workdir) / "audit.jsonl").append(
        "mapping.approved",
        args.by.strip(),
        mapping_file=target.name,
        mapping_sha256=sha256_file(target),
    )
    print(f"approved and installed: {target}")
    return 0


def cmd_verify_audit(args) -> int:
    from .review.audit import AuditLog

    ok, message = AuditLog(Path(args.workdir) / "audit.jsonl").verify()
    print(("OK: " if ok else "FAILED: ") + message)
    return 0 if ok else 1


def cmd_serve(args) -> int:
    import uvicorn

    from .review.app import create_app

    app = create_app(args.workdir, trusted_user_header=args.trusted_user_header)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="formfill", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"formfill {__version__}")
    parser.add_argument(
        "--mappings",
        default=_env("FORMFILL_MAPPINGS", "samples/mappings"),
        help="directory of mapping YAML files",
    )
    parser.add_argument(
        "--data",
        default=_env("FORMFILL_DATA", "samples/clients/clients.json"),
        help="client data file (.json or .csv)",
    )
    parser.add_argument(
        "--workdir",
        default=_env("FORMFILL_WORKDIR", "work"),
        help="where packages, outbox and audit log live",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-templates", help="list registered form templates")

    p = sub.add_parser("validate", help="check a mapping, optionally against a client record")
    p.add_argument("--template", required=True)
    p.add_argument("--client")
    p.add_argument("--json", action="store_true", help="print the review report as JSON")
    p.add_argument("--today", help="override today's date (YYYY-MM-DD) for age checks")

    p = sub.add_parser("fill", help="fill a form and queue it for human review")
    p.add_argument("--template", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--prepared-by", default=_env("FORMFILL_USER", getpass.getuser()))
    p.add_argument("--today", help="override today's date (YYYY-MM-DD) for age checks")

    p = sub.add_parser("suggest-mapping", help="draft a mapping for a new PDF (never applied)")
    p.add_argument("pdf")
    p.add_argument(
        "--provider", default="heuristic", choices=["heuristic", "fake", "anthropic", "openai"]
    )
    p.add_argument("--client", help="client whose record shape is used (values are not sent)")
    p.add_argument("--template-id")
    p.add_argument("--out")

    p = sub.add_parser("approve-mapping", help="promote a reviewed draft into the registry")
    p.add_argument("draft")
    p.add_argument("--by", required=True, help="name of the person approving")

    sub.add_parser("verify-audit", help="check the audit log hash chain")

    p = sub.add_parser("serve", help="run the review web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--trusted-user-header", help="take reviewer identity from this header set by an SSO proxy"
    )
    return parser


COMMANDS = {
    "list-templates": cmd_list_templates,
    "validate": cmd_validate,
    "fill": cmd_fill,
    "suggest-mapping": cmd_suggest_mapping,
    "approve-mapping": cmd_approve_mapping,
    "verify-audit": cmd_verify_audit,
    "serve": cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (
        KeyError,
        PermissionError,
        ValueError,
        FileExistsError,
        FileNotFoundError,
        ReviewError,
    ) as exc:
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
