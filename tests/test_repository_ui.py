"""The history viewer is paged, source-safe and strictly read-only."""

import hashlib
import json
import threading
import urllib.error
import urllib.request

import pytest

from dprovenancekit import ReasoningRepository, InMemoryTraceStore
from dprovenancekit.instrument import record_event, traced_run
from dprovenancekit.ui_server import create_server


@pytest.fixture
def history_server(tmp_path):
    path = tmp_path / "history.sqlite"
    with ReasoningRepository(path) as repo, InMemoryTraceStore() as store:
        workflow = repo.snapshot_workflow({"<img src=x onerror=alert(1)>": b"A selected prompt."})
        ids = []
        for message in ("first", "<script>untrusted message</script>"):
            with traced_run(store, repository=repo, workflow_snapshot=workflow) as run:
                record_event("verify")
            ids.append(repo.commit(run.repository_evidence_id, message))
        repo.tag("baseline", ids[0])
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    server = create_server(repository_path=str(path), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(server.server_address[1]), path, ids, original
    server.shutdown()
    server.server_close()
    thread.join(5)


def fetch(base, route):
    with urllib.request.urlopen(base + route) as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        return json.loads(response.read())


def test_history_paging_lazy_objects_and_readonly_comparison(history_server):
    base, path, ids, original = history_server
    status = fetch(base, "/api/repository")
    assert "path" not in status
    assert fetch(base, "/api/history?limit=1")[0]["id"] == ids[1]
    assert fetch(base, "/api/history?limit=1&offset=1")[0]["id"] == ids[0]
    commit = fetch(base, "/api/objects/" + ids[0])
    assert commit["type"] == "commit"
    report = fetch(base, "/api/repository-diff?baseline=" + ids[0] + "&candidate=" + ids[1])
    assert report["outcome"] == "accepted"
    assert fetch(base, "/api/repository")["object_count"] == status["object_count"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


def test_history_viewer_rejects_bad_paging_and_foreign_host(history_server):
    base, _, _, _ = history_server
    for route in ("/api/history?limit=10000", "/api/history?offset=-1"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + route)
        assert caught.value.code == 400
    request = urllib.request.Request(base + "/api/refs", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)
    assert caught.value.code == 403


def test_history_document_uses_safe_text_rendering(history_server):
    base, _, _, _ = history_server
    with urllib.request.urlopen(base) as response:
        html = response.read().decode()
        assert "Content-Security-Policy" in response.headers
    assert ".textContent" in html
    assert ".innerHTML" not in html
    assert "/api/history" in html
