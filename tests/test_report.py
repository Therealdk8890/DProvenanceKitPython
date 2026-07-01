"""Tests for the HTML report renderer (``dprovenancekit.report``)."""

from __future__ import annotations

from dataclasses import dataclass

from dprovenancekit import (
    DProvenanceKit,
    InMemoryTraceStore,
    RegressionGate,
    TraceableEvent,
    TracePriority,
    render_report_html,
)


@dataclass(frozen=True)
class FCEvent(TraceableEvent):
    kind: str
    detail: str = ""

    @property
    def type_identifier(self) -> str:
        return self.kind

    @property
    def priority(self) -> TracePriority:
        return TracePriority.CRITICAL if self.kind in ("verified", "decided") else TracePriority.STRUCTURAL

    def to_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail}


def _build(store, ctx, steps):
    kit = DProvenanceKit(FCEvent)
    with kit.run(context_id=ctx, store=store) as run:
        for s in steps:
            kit.record(FCEvent(s))
        return store.get_run(run.run_id)


def test_report_pass_renders_standalone_html():
    store = InMemoryTraceStore()
    g = _build(store, "golden", ["retrieved", "verified", "decided"])
    c = _build(store, "candidate", ["retrieved", "verified", "decided"])
    html = render_report_html(RegressionGate().check(g, c), golden_label="main@abc", candidate_label="PR #42")

    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "PASS" in html
    assert "main@abc" in html and "PR #42" in html
    assert "No per-step changes" in html


def test_report_fail_lists_removed_step():
    store = InMemoryTraceStore()
    g = _build(store, "golden", ["retrieved", "verified", "decided"])
    c = _build(store, "regressed", ["retrieved", "decided"])  # dropped the CRITICAL verify
    html = render_report_html(RegressionGate().check(g, c))

    assert "REGRESSION" in html
    assert "Removed" in html and "verified" in html


def test_report_escapes_step_names():
    # A step type_identifier containing HTML must be escaped, never injected.
    store = InMemoryTraceStore()
    g = _build(store, "g", ["retrieved", "<script>x</script>", "decided"])
    c = _build(store, "c", ["retrieved", "decided"])  # the scripty step is dropped
    html = render_report_html(RegressionGate().check(g, c))

    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html

