"""Minimal instrumented agent used by the e2e decision-path demo.

Uses only the public SDK surface (``DProvenanceKit`` + ``AnyTraceableEvent``) so the
demo has no framework extras. Each fixture step becomes one recorded event; CRITICAL
steps are the ones the CI gate refuses to lose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union
from uuid import UUID

from dprovenancekit import DProvenanceKit, TracePriority
from dprovenancekit.event import AnyTraceableEvent

_PRIORITY = {
    "telemetry": TracePriority.TELEMETRY,
    "diagnostic": TracePriority.DIAGNOSTIC,
    "structural": TracePriority.STRUCTURAL,
    "critical": TracePriority.CRITICAL,
}

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_path_fixture(name: str) -> Dict[str, Any]:
    """Load a decision-path fixture (``baseline_path`` / ``candidate_regressed``)."""
    path = FIXTURES_DIR / f"{name}.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def record_path(
    store,
    fixture: Union[str, Mapping[str, Any]],
    *,
    context_id: Optional[str] = None,
) -> UUID:
    """Record one fixture path into ``store``. Returns the new run id."""
    data = load_path_fixture(fixture) if isinstance(fixture, str) else dict(fixture)
    ctx = context_id or str(data["context_id"])
    steps: Sequence[Mapping[str, Any]] = data["steps"]

    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=ctx, store=store) as run:
        for step in steps:
            priority = _PRIORITY[str(step["priority"]).lower()]
            payload = AnyTraceableEvent(
                type_identifier_value=str(step["type"]),
                priority_value=int(priority),
                raw_json=json.dumps({"detail": step.get("detail", "")}, sort_keys=True),
            )
            with kit.with_engine(str(step.get("engine", "Agent"))):
                kit.record(payload)
        return run.run_id


def step_types(fixture: Union[str, Mapping[str, Any]]) -> List[str]:
    data = load_path_fixture(fixture) if isinstance(fixture, str) else dict(fixture)
    return [str(s["type"]) for s in data["steps"]]
