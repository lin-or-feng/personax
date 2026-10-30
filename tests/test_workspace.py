"""Single-operator P1 contracts, persistence, approval binding and HTTP boundaries."""
from __future__ import annotations
import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from api import create_app
from core.document_parser import DocumentError
from core.types import (KnowledgeImportRequest, KnowledgeSearchRequest, WorkspaceConfig,
    WorkspaceEditRequest, WorkspaceReviewRequest, WorkspaceTextEdit)
from core.workspace import WorkspaceError, WorkspaceService


@pytest.fixture
def service(tmp_path):
    return WorkspaceService(WorkspaceConfig(root=str(tmp_path / "private")))


def staged(service, text=b"QuartzToken requires explicit human approval before publication."):
    job = service.upload("guide.txt", text)
    assert service.run_once()
    return service.preview(job.job_id)


def confirmation(preview):
    return WorkspaceReviewRequest(revision=preview.job.revision, preview_hash=preview.job.preview_hash)


def test_approval_is_required_and_commit_uses_exact_preview_without_reparsing(service, monkeypatch):
    preview = staged(service)
    assert service.knowledge.store.documents("workspace") == []
    monkeypatch.setattr("core.knowledge.parse_document_result", lambda *a, **k: pytest.fail("must not parse after approval"))
    service.confirm(preview.job.job_id, confirmation(preview))
    service.run_once()
    done = service.job(preview.job.job_id)
    assert done.status == "completed"
    assert service.source_job(done.document_id, done.document_version).job_id == done.job_id
    hit = service.knowledge.search(KnowledgeSearchRequest(kb_id="workspace", query="QuartzToken", min_score=0.0001), retrieval_mode="bm25")
    assert hit.sources and "human approval" in hit.answer


def test_edits_invalidate_old_approval_and_preserve_human_provenance(service):
    original = staged(service)
    edited = service.edit(original.job.job_id, WorkspaceEditRequest(**confirmation(original).model_dump(),
        edits=[WorkspaceTextEdit(unit_index=0, text="QuartzToken must not publish without approval.")]))
    assert edited.job.preview_hash != original.job.preview_hash
    with pytest.raises(WorkspaceError, match="stale_review"):
        service.confirm(original.job.job_id, confirmation(original))
    service.confirm(edited.job.job_id, confirmation(edited)); service.run_once()
    doc = service.knowledge.store.documents("workspace")[0]
    assert doc.chunks[0].human_edited and "must not publish" in doc.chunks[0].text


def test_restart_restores_preview_and_queued_commit(service):
    preview = staged(service)
    restarted = WorkspaceService(service.config)
    same = restarted.preview(preview.job.job_id)
    assert same.job.preview_hash == preview.job.preview_hash
    restarted.confirm(same.job.job_id, confirmation(same))
    again = WorkspaceService(service.config)
    assert again.run_once() and again.job(same.job.job_id).status == "completed"


def test_repeated_confirmation_is_idempotent(service):
    preview = staged(service); request = confirmation(preview)
    first = service.confirm(preview.job.job_id, request)
    assert service.confirm(preview.job.job_id, request).revision == first.revision
    service.run_once()
    assert service.confirm(preview.job.job_id, request).status == "completed"
    assert not service.run_once() and len(service.knowledge.store.documents("workspace")) == 1


@pytest.mark.parametrize("change", ["source", "preview"])
def test_tampering_invalidates_confirmation(service, change):
    preview = staged(service)
    if change == "source":
        service.source_path(preview.job.job_id).write_bytes(b"changed")
    else:
        row = service._row(preview.job.job_id)
        service._artifact(row["preview_key"]).write_text('{"private":"tampered"}')
    with pytest.raises(WorkspaceError, match="source_changed|preview_changed"):
        service.confirm(preview.job.job_id, confirmation(preview))
    assert not service.knowledge.store.documents("workspace")


def test_source_change_after_confirmation_fails_without_indexing(service):
    preview = staged(service); service.confirm(preview.job.job_id, confirmation(preview))
    service.source_path(preview.job.job_id).write_bytes(b"modified after approval")
    service.run_once()
    assert service.job(preview.job.job_id).reason == "source_changed"
    assert not service.knowledge.store.documents("workspace")


def test_catalog_change_after_preview_cannot_be_overwritten(service):
    preview = staged(service)
    path = service.source_path(preview.job.job_id)
    result = service.knowledge.ingest(KnowledgeImportRequest(kb_id="workspace", source_path=path.name))
    assert result.status == "created"
    service.confirm(preview.job.job_id, confirmation(preview)); service.run_once()
    assert service.job(preview.job.job_id).reason == "review_stale"
    assert service.knowledge.store.get("workspace", result.document_id).document_version == result.document_version


def test_cancelled_stale_parser_cannot_republish_preview(service):
    job = service.upload("guide.txt", b"QuartzToken")
    claim = service._claim()
    assert claim is not None
    service.cancel(job.job_id)
    assert not service._finish(claim, "awaiting_review", key="a"*32+".json")
    assert service.job(job.job_id).status == "cancelled"
    with pytest.raises(WorkspaceError):
        service.preview(job.job_id)


def test_committing_cannot_be_cancelled(service):
    preview = staged(service); service.confirm(preview.job.job_id, confirmation(preview))
    assert service._claim()["status"] == "committing"
    with pytest.raises(WorkspaceError, match="job_not_cancellable"):
        service.cancel(preview.job.job_id)


def test_expired_lease_fails_explicitly_and_new_worker_can_retry(service):
    job = service.upload("guide.txt", b"QuartzToken")
    old = service._claim()
    with service._connect() as db:
        db.execute("UPDATE workspace_jobs SET lease_until=? WHERE job_id=?", (time.time()-1, job.job_id))
    assert not service.run_once()
    assert service.job(job.job_id).reason == "worker_lease_expired"
    service.retry(job.job_id)
    assert service.run_once() and service.job(job.job_id).status == "awaiting_review"
    assert not service._finish(old, "completed")


def test_two_workers_cannot_claim_the_same_job(service):
    job = service.upload("guide.txt", b"QuartzToken")
    with ThreadPoolExecutor(2) as executor:
        claimed = list(executor.map(lambda _: WorkspaceService(service.config)._claim(), range(2)))
    assert sum(row is not None for row in claimed) == 1


@pytest.mark.parametrize("name", ["../private.txt", "folder\\private.txt", "disk:private.txt", "bad\x00.txt"])
def test_filename_paths_rejected(service, name):
    with pytest.raises(WorkspaceError, match="invalid_filename"):
        service.upload(name, b"data")


def test_format_capacity_and_fixed_failure_codes(service):
    with pytest.raises(WorkspaceError, match="file_signature_invalid"):
        service.upload("fake.pdf", b"not pdf")
    with pytest.raises(WorkspaceError, match="unsupported_format"):
        service.upload("tool.exe", b"not executed")
    limited = WorkspaceService(service.config.model_copy(update={"max_jobs": 1}))
    first = limited.upload("broken.txt", b"\xffprivate-value")
    with pytest.raises(WorkspaceError, match="workspace_capacity_reached"):
        limited.upload("another.txt", b"x")
    limited.run_once()
    assert limited.job(first.job_id).reason == "invalid_encoding"
    assert "private-value" not in json.dumps(limited.events(first.job_id))


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setenv("PERSONAX_WORKSPACE_ENABLED", "1")
    monkeypatch.setenv("PERSONAX_WORKSPACE_TOKEN", "fixture-only-" + "x"*32)
    monkeypatch.setenv("LLM_BACKEND", "offline")
    headers = {"Authorization": "Bearer fixture-only-" + "x"*32}
    with TestClient(create_app(workspace_service_factory=lambda: service)) as value:
        value.headers.update(headers)
        yield value


def test_http_upload_review_answer_and_private_source_mapping(client, service):
    uploaded = client.post("/v1/workspace/uploads", content=b"QuartzToken needs explicit approval.",
        headers={"X-Upload-Name": base64.b64encode("guide.txt".encode()).decode()})
    assert uploaded.status_code == 202 and uploaded.headers["cache-control"] == "no-store"
    job_id = uploaded.json()["job_id"]
    assert service.run_once()
    preview = client.get(f"/v1/workspace/jobs/{job_id}/preview").json()
    confirmed = client.post(f"/v1/workspace/jobs/{job_id}/confirm", json={
        "revision": preview["job"]["revision"], "preview_hash": preview["job"]["preview_hash"]})
    assert confirmed.status_code == 200 and service.run_once()
    answered = client.post("/v1/workspace/answer", json={"question": "QuartzToken", "kb_id":"untrusted-kb"})
    assert answered.status_code == 200 and answered.json()["kb_id"] == "workspace"
    assert answered.json()["sources"]
    assert "source_root" not in json.dumps(client.get("/v1/workspace/jobs").json())


@pytest.mark.parametrize("headers,expected", [
    ({"Authorization":""},401),
    ({"Origin":"https://external.invalid"},403),
    ({"Host":"untrusted.invalid"},403),
])
def test_workspace_auth_origin_host_boundary(client, headers, expected):
    response = client.get("/v1/workspace/jobs", headers=headers)
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"


def test_missing_token_is_fail_closed(client, monkeypatch):
    monkeypatch.delenv("PERSONAX_WORKSPACE_TOKEN")
    assert client.get("/v1/workspace/jobs").status_code == 503


def test_http_body_limit_precedes_parsing_and_model_modes_default_off(client):
    rejected = client.post("/v1/workspace/uploads", content=b"x", headers={"Content-Length":str(6*1024*1024)})
    assert rejected.status_code == 413
    llm = client.post("/v1/workspace/answer", json={"question":"private question", "answer_mode":"llm"})
    assert llm.status_code == 403 and llm.json()["detail"]["code"] == "workspace_llm_disabled"


def test_workspace_does_not_add_publish_or_filesystem_path_inputs(client):
    paths = client.get("/openapi/en.json").json()["paths"]
    assert not any("publish" in path for path in paths)
    job = client.post("/v1/workspace/uploads", content=b"guide", headers={"X-Upload-Name":base64.b64encode(b"guide.txt").decode()}).json()
    assert client.post(f"/v1/workspace/jobs/{job['job_id']}/confirm", json={"revision":1,"preview_hash":"a"*64,"source_path":"../private"}).status_code == 422
