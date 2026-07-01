"""Structural reasoning diff over two runs.

Diffing reduces each run to a sequence of structural signatures
(``type_identifier::engine_name``), filtered to a minimum priority (default
``STRUCTURAL``), then runs a sequence diff over the two signature streams. Reordered,
inserted, and removed reasoning steps fall out as ``added`` / ``removed`` changes
carrying their original ``sequence`` for traceability.

Signatures are structure only: two runs that took the same step types in the same order
diff as identical even if their payload *values* differ. For content changes use
``TraceAlignmentEngine``.
"""

from __future__ import annotations

import difflib
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import List

from .priority import TracePriority
from .query import TraceRun


class ChangeKind(Enum):
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    original_sequence: int
    type_identifier: str
    engine_name: str


@dataclass(frozen=True)
class TraceDiffResult:
    base_run_id: uuid.UUID
    comparison_run_id: uuid.UUID
    changes: List[Change]

    @property
    def is_identical(self) -> bool:
        return len(self.changes) == 0


@dataclass(frozen=True)
class _DiffElement:
    signature: str
    sequence: int
    type_identifier: str
    engine_name: str


class TraceDiffEngine:
    def diff(
        self,
        base: TraceRun,
        comparison: TraceRun,
        minimum_priority: TracePriority = TracePriority.STRUCTURAL,
    ) -> TraceDiffResult:
        base_elements = [
            _DiffElement(
                signature=f"{e.payload.type_identifier}::{e.engine_name}",
                sequence=e.sequence,
                type_identifier=e.payload.type_identifier,
                engine_name=e.engine_name,
            )
            for e in base.events
            if e.payload.priority >= minimum_priority
        ]
        comp_elements = [
            _DiffElement(
                signature=f"{e.payload.type_identifier}::{e.engine_name}",
                sequence=e.sequence,
                type_identifier=e.payload.type_identifier,
                engine_name=e.engine_name,
            )
            for e in comparison.events
            if e.payload.priority >= minimum_priority
        ]

        base_sigs = [el.signature for el in base_elements]
        comp_sigs = [el.signature for el in comp_elements]

        matcher = difflib.SequenceMatcher(a=base_sigs, b=comp_sigs, autojunk=False)
        changes: List[Change] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("delete", "replace"):
                for el in base_elements[i1:i2]:
                    changes.append(
                        Change(
                            kind=ChangeKind.REMOVED,
                            original_sequence=el.sequence,
                            type_identifier=el.type_identifier,
                            engine_name=el.engine_name,
                        )
                    )
            if tag in ("insert", "replace"):
                for el in comp_elements[j1:j2]:
                    changes.append(
                        Change(
                            kind=ChangeKind.ADDED,
                            original_sequence=el.sequence,
                            type_identifier=el.type_identifier,
                            engine_name=el.engine_name,
                        )
                    )

        return TraceDiffResult(
            base_run_id=base.run_id,
            comparison_run_id=comparison.run_id,
            changes=changes,
        )

