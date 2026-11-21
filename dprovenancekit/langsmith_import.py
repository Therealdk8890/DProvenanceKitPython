"""Convert exported LangSmith runs into OTLP JSON so they can be ingested like OTel traces.

LangSmith stores a trace as a tree of *runs* (``id``, ``trace_id``, ``parent_run_id``,
``run_type``, ``name``, ``start_time``, ``end_time``, ``inputs``, ``outputs``, ``error``,
``extra``). This module maps those documented run fields onto OpenInference-style span
attributes and hands the result to :func:`dprovenancekit.otel_ingest.ingest_otlp`, so
LangSmith history gets the same classification, ordering and fingerprints as any other
OpenTelemetry source. No LangSmith SDK is needed; the input is plain JSON.

Accepted input (one file):

* a JSON array of run objects,
* JSON-lines, one run object per line,
* an object with a ``"runs"`` array (the shape of LangSmith's ``POST /runs/query``), or
* a single run object, optionally carrying nested ``child_runs``.

Produce one with the LangSmith SDK, for example::

    import json
    from langsmith import Client

    runs = Client().list_runs(project_name="my-agent", start_time=week_ago)
    with open("langsmith-runs.json", "w") as fh:
        json.dump([r.dict() for r in runs], fh, default=str)

**Identity.** The OTLP trace id is the LangSmith ``trace_id`` (the root run's id) as 32
hex characters, so the DProvenanceKit run id of an imported trace *equals* the LangSmith
trace id. Span ids are the run ids as 32 hex characters. Both make it trivial to send a
gate verdict back to the right LangSmith run (see
:mod:`dprovenancekit.exporters.langsmith`).

Standard library only.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

# LangSmith run_type → openinference.span.kind (classified by otel_ingest §2.1).
_RUN_TYPE_KINDS = {
    "llm": "LLM",
    "chain": "CHAIN",
    "tool": "TOOL",
    "retriever": "RETRIEVER",
    "embedding": "EMBEDDING",
    "prompt": "PROMPT",
    "parser": "CHAIN",
}

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


def _to_nanos(value: Any) -> int:
    """Parse a LangSmith timestamp (ISO 8601 text, naive = UTC; or epoch seconds)."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError(f"invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        return int(round(float(value) * 1e9))
    text = str(value).strip()
    match = _ISO_RE.match(text)
    if not match:
        raise ValueError(f"invalid timestamp: {value!r}")
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    tz = timezone.utc
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        from datetime import timedelta

        tz = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    moment = datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second), tzinfo=tz
    )
    seconds = int(moment.timestamp())
    nanos = int((fraction or "0").ljust(9, "0")[:9])
    return seconds * 1_000_000_000 + nanos


def _hex(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    try:
        return uuid.UUID(str(value)).hex
    except ValueError:
        return None


def _string_attr(key: str, value: str) -> Dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def _flatten(runs: Iterable[Any]) -> List[Mapping[str, Any]]:
    out: List[Mapping[str, Any]] = []
    stack = [r for r in reversed(list(runs)) if isinstance(r, Mapping)]
    while stack:
        run = stack.pop()
        out.append(run)
        children = run.get("child_runs") or []
        if isinstance(children, list):
            stack.extend(c for c in reversed(children) if isinstance(c, Mapping))
    return out


def parse_langsmith_runs(text: str) -> List[Mapping[str, Any]]:
    """Parse exported LangSmith runs (see the module docstring for accepted shapes)."""
    stripped = text.strip()
    if not stripped:
        return []
    try:
        parsed: Any = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = []
        for lineno, line in enumerate(stripped.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {lineno} is not valid JSON: {exc}") from exc
    if isinstance(parsed, Mapping):
        if isinstance(parsed.get("runs"), list):
            parsed = parsed["runs"]
        else:
            parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("expected a run object, an array of runs, or {'runs': [...]}")
    return _flatten(parsed)


def _model_name(run: Mapping[str, Any]) -> Optional[str]:
    extra = run.get("extra") or {}
    if not isinstance(extra, Mapping):
        return None
    metadata = extra.get("metadata") or {}
    params = extra.get("invocation_params") or {}
    for source, key in (
        (metadata, "ls_model_name"),
        (params, "model"),
        (params, "model_name"),
    ):
        if isinstance(source, Mapping) and source.get(key):
            return str(source[key])
    return None


def _metadata(run: Mapping[str, Any]) -> Mapping[str, Any]:
    extra = run.get("extra") or {}
    metadata = extra.get("metadata") if isinstance(extra, Mapping) else None
    return metadata if isinstance(metadata, Mapping) else {}


def langsmith_runs_to_otlp(
    runs: Iterable[Mapping[str, Any]],
    *,
    capture_payloads: bool = True,
    service_name: str = "langsmith",
) -> Dict[str, Any]:
    """Build one OTLP/HTTP JSON request holding every run as a span.

    Runs without a usable UUID ``id`` are skipped. A run without ``trace_id`` inherits
    its parent's trace, or starts its own trace when it has no known parent.
    """
    flat = _flatten(runs)
    by_id: Dict[str, Mapping[str, Any]] = {}
    for run in flat:
        run_hex = _hex(run.get("id"))
        if run_hex is not None and run_hex not in by_id:
            by_id[run_hex] = run

    def trace_of(run_hex: str) -> str:
        seen = set()
        current = run_hex
        while current not in seen:
            seen.add(current)
            run = by_id[current]
            explicit = _hex(run.get("trace_id"))
            if explicit is not None:
                return explicit
            parent = _hex(run.get("parent_run_id"))
            if parent is None or parent not in by_id:
                return current
            current = parent
        return run_hex  # parent cycle: fall back to the run itself

    spans: List[Dict[str, Any]] = []
    for run_hex, run in by_id.items():
        run_type = str(run.get("run_type") or "chain").lower()
        name = str(run.get("name") or run_type)
        attrs = [
            _string_attr("openinference.span.kind", _RUN_TYPE_KINDS.get(run_type, "CHAIN")),
            _string_attr("langsmith.run_type", run_type),
            _string_attr("langsmith.run_id", str(uuid.UUID(hex=run_hex))),
        ]
        if run_type == "llm":
            model = _model_name(run)
            if model:
                attrs.append(_string_attr("llm.model_name", model))
            provider = _metadata(run).get("ls_provider")
            if provider:
                attrs.append(_string_attr("llm.provider", str(provider)))
        elif run_type == "tool":
            attrs.append(_string_attr("tool.name", name))
        node = _metadata(run).get("langgraph_node")
        if node and run_type == "chain":
            attrs.append(_string_attr("graph.node.name", str(node)))
        if capture_payloads:
            for key, field_name in (("input.value", "inputs"), ("output.value", "outputs")):
                value = run.get(field_name)
                if value not in (None, {}, ""):
                    attrs.append(
                        _string_attr(
                            key,
                            value if isinstance(value, str)
                            else json.dumps(value, sort_keys=True, default=str),
                        )
                    )
        start_ns = _to_nanos(run.get("start_time"))
        end_ns = _to_nanos(run.get("end_time")) or start_ns
        error = run.get("error")
        is_error = bool(error) or str(run.get("status") or "").lower() == "error"
        span: Dict[str, Any] = {
            "traceId": trace_of(run_hex),
            "spanId": run_hex,
            "name": name,
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": attrs,
            "status": (
                {"code": 2, "message": str(error) if error else "error"}
                if is_error
                else {"code": 0}
            ),
        }
        parent = _hex(run.get("parent_run_id"))
        if parent is not None:
            span["parentSpanId"] = parent
        spans.append(span)

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_string_attr("service.name", service_name)]},
                "scopeSpans": [{"scope": {"name": "langsmith-import"}, "spans": spans}],
            }
        ]
    }


def load_langsmith_file(path: str, *, capture_payloads: bool = True) -> Dict[str, Any]:
    """Read an exported LangSmith runs file and return the OTLP JSON request."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ValueError(f"cannot read {path!r}: {exc}") from exc
    return langsmith_runs_to_otlp(
        parse_langsmith_runs(text), capture_payloads=capture_payloads
    )


__all__ = ["langsmith_runs_to_otlp", "load_langsmith_file", "parse_langsmith_runs"]
