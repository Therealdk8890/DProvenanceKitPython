"""Informative capture/seal/compare costs; run from an installed source checkout."""

import argparse
import json
from pathlib import Path
import tempfile
import time

from dprovenancekit import ReasoningRepository, SQLiteTraceStore
from dprovenancekit.instrument import TracedEvent, record_event, traced_run


def measure(count):
    with tempfile.TemporaryDirectory(prefix="dpk-repository-benchmark-") as temporary:
        directory = Path(temporary)
        with ReasoningRepository(directory / "history.sqlite") as repo, SQLiteTraceStore(
            TracedEvent, str(directory / "capture.sqlite"), start_writer=False
        ) as store:
            workflow = repo.snapshot_workflow({"prompt": b"repeat the declared steps"})
            capture_times = []
            seal_times = []
            commits = []
            for index in range(2):
                with traced_run(store, repository=repo, workflow_snapshot=workflow) as run:
                    start = time.perf_counter()
                    for step in range(count):
                        record_event("step", {"value": step})
                    captured = time.perf_counter()
                sealed = time.perf_counter()
                capture_times.append(captured - start)
                seal_times.append(sealed - captured)
                commits.append(repo.commit(run.repository_evidence_id, "benchmark " + str(index)))
            start = time.perf_counter()
            report = repo.compare(commits[0], commits[1], persist=False)
            compared = time.perf_counter() - start
            assert report["outcome"] == "accepted"
            return {"events_per_run": count, "mean_record_us": sum(capture_times) / (2 * count) * 1e6,
                    "mean_finish_and_seal_ms": sum(seal_times) * 500,
                    "compare_ms": compared * 1000, "objects": repo.status()["object_count"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", nargs="+", type=int, default=[100, 1000])
    args = parser.parse_args()
    if any(not 1 <= count <= 1000 for count in args.events):
        parser.error("use 1-1000 events for the default comparison budget")
    for count in args.events:
        print(json.dumps(measure(count), sort_keys=True), flush=True)
