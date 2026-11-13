"""Offline acceptance scenario for workflow history and portable regression evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Union

from .instrument import TracedEvent, record_event, traced_run
from .repository import ReasoningRepository
from .sqlite_store import SQLiteTraceStore


def run_demo(output_dir: Union[str, Path]) -> Dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("choose an empty output directory; the history demo never replaces existing evidence")
    history = output / "repository.sqlite"
    capture_db = output / "traces.sqlite"
    bundle = output / "history.dpkbundle"
    restored = output / "restored.sqlite"

    with ReasoningRepository(history) as repo, SQLiteTraceStore(TracedEvent, str(capture_db)) as store:
        workflow = repo.snapshot_workflow(
            {"prompt.txt": b"Retrieve evidence, plan, verify, then answer."},
            config={"model": "scripted-offline-demo", "temperature": "0"},
            source={"description": "Deterministic fixture; no model or network calls"},
        )

        def capture(snapshot: str, verify: bool = True) -> str:
            with traced_run(store, context_id="history-demo", repository=repo, workflow_snapshot=snapshot) as run:
                for step in ("retrieve", "plan", "verify", "answer"):
                    if step == "verify" and not verify:
                        continue
                    record_event(step, {"answer": "approved"} if step == "answer" else {},
                                 logical_step_id=step)
            assert run.repository_evidence_id is not None
            return run.repository_evidence_id

        baseline = repo.commit(capture(workflow), "Preserve the verified decision path")
        initial = repo.compare(baseline, baseline)
        repo.promote(baseline, initial["evaluation_id"])
        repo.create_branch("skip-verification", baseline)
        repo.switch("skip-verification")
        changed_workflow = repo.snapshot_workflow(
            {"prompt.txt": b"Retrieve evidence, plan, then answer immediately."},
            config={"model": "scripted-offline-demo", "temperature": "0"},
        )
        candidate = repo.commit(capture(changed_workflow, verify=False), "Experiment without verification")
        regression = repo.compare(baseline, candidate)
        assert regression["outcome"] == "regressed"
        repo.switch("main")
        matching = repo.commit(capture(workflow), "Fresh execution with the same accepted path")
        accepted = repo.compare(baseline, matching)
        assert accepted["outcome"] == "accepted" and baseline != matching
        repo.export_bundle(bundle, roots=[candidate, matching, initial["evaluation_id"],
                                          regression["evaluation_id"], accepted["evaluation_id"]])
        assert repo.fsck()["valid"]

    with ReasoningRepository(restored) as imported:
        imported.import_bundle(bundle, ref_mapping={
            "refs/heads/main": "refs/heads/main",
            "refs/heads/skip-verification": "refs/heads/skip-verification",
        })
        reproduced = imported.compare(baseline, candidate, persist=False)
        assert reproduced == regression
        assert imported.fsck()["valid"]
    report = {"baseline": baseline, "candidate": candidate, "matching": matching,
              "regression": regression, "matching_evaluation": accepted,
              "bundle": str(bundle), "history": str(history), "restored": str(restored),
              "same_final_answer": "approved", "offline_roundtrip_verified": True}
    (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
