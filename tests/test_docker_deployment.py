"""Docker reproducibility contract tests."""
from __future__ import annotations

import socket
from pathlib import Path

import yaml

import core.llm as llm


ROOT = Path(__file__).resolve().parents[1]


def test_compose_has_safe_defaults_and_persistent_state():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["app"]
    environment = service["environment"]

    assert environment["LLM_BACKEND"] == "${LLM_BACKEND:-offline}"
    assert "host.docker.internal" in environment["OLLAMA_BASE_URL"]
    assert environment["XHS_REAL_PUBLISH_ENABLED"] == "0"
    assert service["ports"] == [
        "${PERSONAX_BIND_HOST:-127.0.0.1}:${PERSONAX_PORT:-8501}:8501",
    ]
    assert service["read_only"] is True
    assert service["pids_limit"] == 256
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    mounts = set(service["volumes"])
    assert "personax_logs:/app/logs" in mounts
    assert "personax_rag_cache:/app/.rag_cache" in mounts
    assert "personax_knowledge:/app/knowledge" in mounts

    api = compose["services"]["api"]
    assert api["profiles"] == ["api"]
    assert api["ports"] == [
        "${PERSONAX_API_BIND_HOST:-127.0.0.1}:${PERSONAX_API_PORT:-8000}:8000",
    ]
    assert api["environment"]["XHS_REAL_PUBLISH_ENABLED"] == "0"
    assert "PERSONAX_API_TOKEN" in api["environment"]
    assert api["command"][-2:] == ["--workers", "1"]
    assert "restart" not in api
    assert api["read_only"] is True
    assert api["cap_drop"] == ["ALL"]


def test_docker_build_context_excludes_secrets():
    ignored = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".env" in ignored
    assert "storage_state.json" in ignored
    assert "logs" in ignored


def test_ollama_probe_uses_configured_container_host(monkeypatch):
    captured: dict[str, object] = {}

    class Connection:
        def close(self):
            captured["closed"] = True

    def fake_create_connection(address, timeout):
        captured["address"] = address
        captured["timeout"] = timeout
        return Connection()

    monkeypatch.setenv(
        "OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1",
    )
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    llm._OLLAMA_CHECK.update(ts=0.0, ok=False, endpoint=None)

    assert llm._ollama_reachable(timeout=1.25) is True
    assert captured == {
        "address": ("host.docker.internal", 11434),
        "timeout": 1.25,
        "closed": True,
    }
