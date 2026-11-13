"""Opt-in reasoning history CLI (experimental; requires the repository backend).

This module is intentionally not installed into the existing `dpk` dispatch
until the backend's read-only sealing and atomic ref operations are validated.
Legacy trace and gate commands are unchanged.
"""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path
from typing import Any, Dict, List, Optional


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if hasattr(value, "to_dict"):
        return _json_value(value.to_dict())
    if hasattr(value, "__dict__"):
        return _json_value(vars(value))
    return value


def _emit(value: Any) -> None:
    print(json.dumps(_json_value(value), sort_keys=True, ensure_ascii=False, indent=2))


def _mapping_file(path: Optional[str]) -> Dict[str, Any]:
    if path is None:
        return {}
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("configuration must be a JSON object")
    return value


def _verified(result: Any, *keys: str) -> bool:
    """Only an explicit Boolean True is successful. Conflicting flags fail closed."""
    if not isinstance(result, dict):
        return False
    flags = [result[key] for key in keys if key in result]
    return bool(flags) and all(flag is True for flag in flags)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dpk repo", description="Local, immutable reasoning history (opt-in format v1)."
    )
    parser.add_argument("--repo", default=".dprovenance/repository.sqlite", help="repository path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create or inspect a reasoning repository")
    commands.add_parser("status", help="show branch tips and baseline references")

    snapshot = commands.add_parser("snapshot", help="snapshot explicitly selected workflow assets")
    snapshot.add_argument("--asset", action="append", default=[], metavar="NAME=PATH")
    snapshot.add_argument("--config", help="workflow configuration JSON")
    snapshot.add_argument("--source", help="source provenance JSON; never collected implicitly")
    snapshot.add_argument("--capture-policy", help="capture/redaction policy JSON")

    commit = commands.add_parser("commit", help="commit one workflow and sealed execution")
    selection = commit.add_mutually_exclusive_group(required=True)
    selection.add_argument("--evidence", help="sealed run object ID")
    selection.add_argument("--run", help="recorded run UUID, bound to a workflow before execution")
    commit.add_argument("--db", default=".dprovenance/traces.sqlite", help="capture database")
    commit.add_argument("-m", "--message", required=True)
    commit.add_argument("--author", default="")
    commit.add_argument("--branch")
    commit.add_argument(
        "--expected-parent", help="required for an existing branch; checked atomically by backend"
    )

    log = commands.add_parser("log", help="page through immutable history")
    log.add_argument("ref", nargs="?", default="HEAD")
    log.add_argument("--limit", type=int, default=50)
    log.add_argument("--offset", type=int, default=0)

    show = commands.add_parser("show", help="show an object or commit reference")
    show.add_argument("ref", nargs="?", default="HEAD")
    branch = commands.add_parser("branch", help="list or create a reasoning branch")
    branch.add_argument("name", nargs="?")
    branch.add_argument("--from", dest="start", default="HEAD")
    switch = commands.add_parser("switch", help="select reasoning history; source files stay in place")
    switch.add_argument("name")
    tag = commands.add_parser("tag", help="create a tag without replacing an existing tag")
    tag.add_argument("name")
    tag.add_argument("target", nargs="?", default="HEAD")

    for name in ("diff", "gate"):
        compare = commands.add_parser(name, help="compare workflow assets and execution paths")
        compare.add_argument("--baseline", required=True)
        compare.add_argument("--candidate", required=True)
        compare.add_argument("--policy", help="versioned comparison policy JSON")
        compare.add_argument("--json", action="store_true")

    baseline = commands.add_parser("baseline", help="conditionally promote an evaluated commit")
    baseline.add_argument("candidate")
    baseline.add_argument("--name", default="default")
    baseline.add_argument("--evaluation", required=True, help="accepted evaluation object ID")
    baseline.add_argument("--expected-old", help="previous baseline ID; omit only for initial baseline")

    bundle = commands.add_parser("bundle", help="portable, verified, full-closure bundles")
    bundle_commands = bundle.add_subparsers(dest="bundle_command", required=True)
    export = bundle_commands.add_parser("export")
    export.add_argument("path")
    export.add_argument("--root", action="append", dest="roots")
    imp = bundle_commands.add_parser("import")
    imp.add_argument("path")
    imp.add_argument("--map-ref", action="append", default=[], metavar="IMPORTED=LOCAL")

    commands.add_parser("fsck", help="verify object hashes, references, and evidence health")
    sign = commands.add_parser("sign", help="add detached P-256 signature (requires [crypto])")
    sign.add_argument("subject")
    sign.add_argument("--key", required=True, help="PEM private key path; never printed")
    verify = commands.add_parser("verify", help="verify detached signatures separately")
    verify.add_argument("subject")
    verify.add_argument(
        "--trusted-key", action="append", dest="trusted_key_ids", required=True,
        help="independently trusted signer key ID (repeatable)",
    )
    return parser


def _seal_run_without_mutating_capture(repo: Any, db_path: str, run_id: str) -> Any:
    """Delegate sealing only to a backend that guarantees strictly read-only capture.

    Do NOT pass SQLiteTraceStore(start_writer=False): it still opens and
    initializes a writable WAL database. Do NOT use the UI reader here: it
    deliberately skips undecodable events, which is unsafe for sealing.
    """
    if not Path(db_path).is_file():
        raise ValueError("capture database does not exist")
    seal = getattr(repo, "seal_run_from_readonly_database", None)
    if not callable(seal):
        raise RuntimeError(
            "read-only capture sealing is not supported by this repository backend"
        )
    # The backend must reject missing/dropped/corrupt events, unbound runs,
    # mismatched workflow IDs, and concurrent capture modifications.
    return seal(db_path, run_id)


def main(argv: Optional[List[str]] = None, *, repository_factory: Any = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command != "init" and not Path(args.repo).is_file():
            raise ValueError("repository does not exist; run 'dpk repo init' first")
        if repository_factory is None:
            module = import_module(".repository", package=__package__)
            repository_factory = module.ReasoningRepository

        with repository_factory(args.repo) as repo:
            command = args.command
            if command in ("init", "status"):
                _emit({"format": "DPK-REPOSITORY-V1", "refs": repo.refs(),
                       "branches": repo.branches()})
            elif command == "snapshot":
                assets: Dict[str, bytes] = {}
                for asset in args.asset:
                    name, separator, path = asset.partition("=")
                    if not separator or not name or not path or name in assets:
                        raise ValueError("--asset must be a unique NAME=PATH")
                    assets[name] = Path(path).read_bytes()
                print(repo.snapshot_workflow(
                    assets, config=_mapping_file(args.config), source=_mapping_file(args.source),
                    capture_policy=_mapping_file(args.capture_policy),
                ))
            elif command == "commit":
                # A preflight check improves CLI diagnostics; the backend MUST also
                # perform compare-and-swap in the same transaction as the ref update.
                ref = args.branch or "HEAD"
                try:
                    existing = repo.resolve(ref)
                except KeyError:
                    existing = None
                if existing is not None and args.expected_parent is None:
                    raise ValueError("--expected-parent is required for an existing branch")
                evidence = args.evidence
                if args.run:
                    evidence = repo.lookup_run(args.run)
                    if evidence is None:
                        evidence = _seal_run_without_mutating_capture(repo, args.db, args.run)
                if evidence is None:
                    raise ValueError("no sealed evidence available for commit")
                print(repo.commit(
                    evidence, args.message, branch=args.branch,
                    expected_parent=args.expected_parent, author=args.author,
                ))
            elif command == "log":
                if args.limit < 1 or args.offset < 0:
                    raise ValueError("--limit must be positive and --offset non-negative")
                _emit(repo.log(args.ref, limit=args.limit, offset=args.offset))
            elif command == "show":
                _emit(repo.get_object(repo.resolve(args.ref)))
            elif command == "branch":
                _emit(repo.create_branch(args.name, args.start) if args.name else repo.branches())
            elif command == "switch":
                _emit(repo.switch(args.name))
            elif command == "tag":
                _emit(repo.tag(args.name, args.target))
            elif command in ("diff", "gate"):
                report = repo.compare(args.baseline, args.candidate,
                                      policy=_mapping_file(args.policy) or None)
                if args.json:
                    _emit(report)
                else:
                    print("Reasoning comparison: " + report["outcome"].upper())
                    _emit(report)
                outcome = report["outcome"]
                return 2 if outcome in ("invalid", "incomplete") else int(
                    command == "gate" and outcome != "accepted"
                )
            elif command == "baseline":
                _emit(repo.promote(args.candidate, args.evaluation,
                                   baseline=args.name, expected_old=args.expected_old))
            elif command == "bundle":
                if args.bundle_command == "export":
                    _emit(repo.export_bundle(args.path, roots=args.roots))
                else:
                    mapping: Dict[str, str] = {}
                    for value in args.map_ref:
                        imported, sep, local = value.partition("=")
                        if not sep or not imported or not local or imported in mapping:
                            raise ValueError("--map-ref must be a unique IMPORTED=LOCAL")
                        mapping[imported] = local
                    _emit(repo.import_bundle(args.path, ref_mapping=mapping))
            elif command == "fsck":
                result = repo.fsck()
                _emit(result)
                return 0 if _verified(result, "valid", "ok") else 2
            elif command == "sign":
                from cryptography.hazmat.primitives.serialization import load_pem_private_key
                key = load_pem_private_key(Path(args.key).read_bytes(), password=None)
                _emit(repo.sign(repo.resolve(args.subject), key))
            elif command == "verify":
                result = repo.verify_signatures(repo.resolve(args.subject),
                                                args.trusted_key_ids)
                _emit(result)
                return 0 if _verified(result, "valid", "verified") else 2
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, ImportError,
            sqlite3.Error) as exc:
        print("dpk repo: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
