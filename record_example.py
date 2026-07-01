
import json
from dprovenancekit import DProvenanceKit, SQLiteTraceStore, AnyTraceableEvent, TracePriority

DB = "my-traces.sqlite"
store = SQLiteTraceStore(AnyTraceableEvent, DB, start_writer=False)

def record(context_id, steps):
    """steps = list of (engine, action, detail). 'verify'/'decide' are marked CRITICAL."""
    kit = DProvenanceKit(AnyTraceableEvent)
    with kit.run(context_id=context_id, store=store) as run:
        rid = run.run_id
        for engine, action, detail in steps:
            prio = TracePriority.CRITICAL if action in ("verify", "decide") else TracePriority.STRUCTURAL
            with kit.with_engine(engine):
                kit.record(AnyTraceableEvent(
                    type_identifier_value=action,
                    priority_value=int(prio),
                    raw_json=json.dumps({"detail": detail}),
                ))
    return rid

# The known-good baseline:
golden = record("my-agent · main", [
    ("planner",   "plan",   "break the task down"),
    ("retriever", "search", "look up the docs"),
    ("verifier",  "verify", "cross-check two sources"),
    ("planner",   "decide", "final answer"),
])

# A regressed change (looped search, skipped verify):
candidate = record("my-agent · PR-1", [
    ("planner",   "plan",   "break the task down"),
    ("retriever", "search", "look up the docs"),
    ("retriever", "search", "retry"),
    ("retriever", "search", "retry"),
    ("retriever", "search", "retry"),
    ("planner",   "decide", "final answer"),   # <-- no verify!
])

store.close()
print("db       :", DB)
print("golden   :", golden)
print("candidate:", candidate)