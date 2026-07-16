"""Security posture of the local trace viewer.

Trace databases hold prompts, tool calls, and model outputs, so the server must
default to loopback-only, and the viewer must HTML-escape everything it renders:
payloads carry LLM/tool text, so an unescaped interpolation turns a
prompt-injected agent run into script execution in the developer's browser.
"""

from __future__ import annotations

import re
from pathlib import Path

from dprovenancekit.ui_server import create_server

INDEX_HTML = Path(__file__).parent.parent / "dprovenancekit" / "index.html"

# Interpolations that never carry trace-derived text: fixed style/class constants,
# and fragments assembled entirely from already-escaped pieces.
SAFE_INTERPOLATIONS = {
    "${badgeColor}",
    "${cardClass}",
    "${selected}",
    "${compareOptions}",
    "${engineName}",
}


def test_server_binds_loopback_by_default():
    server = create_server(db_path="unused.sqlite", port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_every_viewer_interpolation_is_escaped():
    html = INDEX_HTML.read_text()
    assert "function esc(" in html, "the esc() helper must exist"
    unescaped = [
        m
        for m in re.findall(r"\$\{[^}]*\}", html)
        if not m.startswith("${esc(")
        and not m.startswith("${encodeURIComponent(")
        and m not in SAFE_INTERPOLATIONS
    ]
    assert unescaped == [], (
        "unescaped template interpolations found in index.html — route them "
        f"through esc() or add a justified entry to SAFE_INTERPOLATIONS: {unescaped}"
    )


def test_viewer_normalizes_sqlite_and_event_timestamp_units():
    html = INDEX_HTML.read_text()
    assert "function formatTimestamp(value)" in html
    assert "magnitude >= 1e14" in html  # SQLite run metadata: microseconds
    assert "magnitude < 1e11" in html  # detailed event payloads: seconds
    assert "new Date(run.start_time * 1000)" not in html
    assert "esc(formatTimestamp(run.start_time))" in html
    assert "esc(formatTimestamp(event.timestamp))" in html


def test_run_picker_uses_keyboard_accessible_buttons():
    html = INDEX_HTML.read_text()
    assert "document.createElement('button')" in html
    assert "button.type = 'button'" in html
    assert "aria-pressed" in html
    assert ".run-item:focus-visible" in html
