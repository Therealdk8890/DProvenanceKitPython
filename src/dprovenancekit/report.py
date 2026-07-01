"""Render a regression-gate result as a standalone, shareable HTML report.

A self-contained HTML document (inline CSS, no assets, no dependencies) summarizing a
:class:`~dprovenancekit.testing.RegressionReport`: the verdict, severity, fingerprints, the
per-step changes, and the engine's reasoning. It is print-friendly, so a browser's
*Print → Save as PDF* turns it into a PDF without any extra tooling.

    from dprovenancekit import RegressionGate, render_report_html

    report = RegressionGate().check(golden, candidate)
    html = render_report_html(report, golden_label="main@abc123", candidate_label="PR #42")
"""

from __future__ import annotations

import html
from typing import Optional

from .alignment_models import AlignmentStateKind

# Per-step change kinds in a stable display order, with human labels. ``semanticMatch`` is an
# evaluator-accepted equivalence (not a divergence), so it is shown separately.
_CHANGE_LABELS = [
    (AlignmentStateKind.REMOVED.value, "Removed", "removed"),
    (AlignmentStateKind.ADDED.value, "Added", "added"),
    (AlignmentStateKind.REORDERED.value, "Reordered", "reordered"),
    (AlignmentStateKind.AMBIGUOUS.value, "Changed (ambiguous)", "ambiguous"),
]

_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f6f8fa;color:#1f2328;font:15px/1.6 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:32px 24px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:12px;padding:24px;margin-bottom:18px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:#656d76;margin:0 0 12px;font-weight:600}
.sub{color:#656d76;font-size:13px;margin:0 0 20px}
.badge{display:inline-block;font-weight:700;letter-spacing:.5px;padding:4px 14px;border-radius:6px;font-size:14px}
.badge.pass{background:#1f883d;color:#fff}
.badge.fail{background:#cf222e;color:#fff}
.meta{font-size:14px;margin-top:14px}
.meta b{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.grid{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:14px}
.grid .k{color:#656d76}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px}
.chip{display:inline-block;border:1px solid #d0d7de;border-radius:20px;padding:1px 10px;margin:2px 4px 2px 0;font-size:13px}
.chip.removed{border-color:#cf222e;color:#cf222e}
.chip.added{border-color:#1f883d;color:#1f883d}
.chip.reordered,.chip.ambiguous{border-color:#9a6700;color:#9a6700}
.chip.accepted{border-color:#0969da;color:#0969da}
.grp{margin-top:12px}
.grp .label{font-size:12px;color:#656d76;text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px}
pre{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:12px;overflow:auto;font-size:13px;white-space:pre-wrap}
.foot{color:#8c959f;font-size:12px;text-align:center}
@media print{body{background:#fff}.card{break-inside:avoid}}
"""


def _chips(steps, cls: str) -> str:
    if not steps:
        return '<span style="color:#8c959f">—</span>'
    return "".join(f'<span class="chip {cls}">{html.escape(s)}</span>' for s in steps)


def _short(fp: str) -> str:
    return html.escape(fp[:16]) + "…" if fp else "—"


def render_report_html(
    report,
    *,
    title: str = "DProvenanceKit regression report",
    golden_label: Optional[str] = None,
    candidate_label: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> str:
    """Render ``report`` (a :class:`RegressionReport`) as a standalone HTML document."""
    passed = report.passed
    verdict = "PASS" if passed else "REGRESSION"
    badge_cls = "pass" if passed else "fail"

    change_rows = []
    for kind_value, label, cls in _CHANGE_LABELS:
        steps = report.steps_by_change.get(kind_value, [])
        if steps:
            change_rows.append(
                f'<div class="grp"><div class="label">{label}</div>{_chips(steps, cls)}</div>'
            )
    accepted = report.steps_by_change.get(AlignmentStateKind.SEMANTIC_MATCH.value, [])
    if accepted:
        change_rows.append(
            '<div class="grp"><div class="label">Accepted as equivalent</div>'
            f'{_chips(accepted, "accepted")}</div>'
        )
    if not change_rows:
        change_rows.append('<p style="color:#656d76;margin:0">No per-step changes — all exact matches.</p>')

    meta_rows = [
        ("Severity", f"{html.escape(report.regression_level.value)} "
                     f"(strength {report.strength:.2f})"),
        ("Max allowed", html.escape(report.max_regression_level.value)),
        ("Allow divergent", "yes" if report.allow_divergent_steps else "no"),
        ("Fingerprint", "match" if report.fingerprint_match else "differs"),
    ]
    if golden_label:
        meta_rows.append(("Golden", html.escape(golden_label)))
    if candidate_label:
        meta_rows.append(("Candidate", html.escape(candidate_label)))
    meta_rows.append(("Golden fingerprint", f'<span class="mono">{_short(report.golden_fingerprint)}</span>'))
    meta_rows.append(("Candidate fingerprint", f'<span class="mono">{_short(report.candidate_fingerprint)}</span>'))
    if generated_at:
        meta_rows.append(("Generated", html.escape(generated_at)))

    meta_html = "".join(
        f'<div class="k">{k}</div><div>{v}</div>' for k, v in meta_rows
    )
    reasoning = (
        f'<div class="grp"><div class="label">Engine</div><div>{html.escape(report.reasoning)}</div></div>'
        if report.reasoning
        else ""
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
  <div class="card">
    <h1>{html.escape(title)}</h1>
    <p class="sub">Reasoning regression gate</p>
    <span class="badge {badge_cls}">{verdict}</span>
  </div>
  <div class="card">
    <h2>Verdict</h2>
    <div class="grid">{meta_html}</div>
  </div>
  <div class="card">
    <h2>Per-step changes</h2>
    {''.join(change_rows)}
    {reasoning}
  </div>
  <div class="card">
    <h2>Summary</h2>
    <pre>{html.escape(report.summary())}</pre>
  </div>
  <p class="foot">Generated by DProvenanceKit</p>
</div></body></html>"""


__all__ = ["render_report_html"]

