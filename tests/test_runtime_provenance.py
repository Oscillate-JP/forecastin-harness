"""Tests for the runtime_provenance module.

Every test injects a fixed list of MountSpec or a synthetic
``docker inspect`` JSON payload. No live docker daemon is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from forecastin_harness.runtime_provenance import (
    MountSpec,
    evaluate_mounts,
    load_mounts_from_docker_inspect,
    render_provenance_report,
)


def _sample_inspect_json(backend_source: str, frontend_source: str) -> str:
    """Build a minimal docker inspect payload with two services."""
    return json.dumps([
        {
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "backend",
                    "com.docker.compose.project": "forecastin",
                },
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": backend_source,
                    "Destination": "/app",
                },
                {
                    "Type": "volume",
                    "Source": "anon-vol",
                    "Destination": "/data",
                },
            ],
        },
        {
            "Config": {
                "Labels": {
                    "com.docker.compose.service": "frontend",
                },
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": frontend_source,
                    "Destination": "/app",
                },
            ],
        },
    ])


def test_allowed_root_passes(tmp_path: Path) -> None:
    allowed = tmp_path / "coordinator-main"
    allowed.mkdir()
    backend_src = allowed / "backend"
    frontend_src = allowed / "frontend"
    backend_src.mkdir()
    frontend_src.mkdir()

    payload = _sample_inspect_json(str(backend_src), str(frontend_src))
    mounts = load_mounts_from_docker_inspect(payload)

    report = evaluate_mounts(
        mounts,
        allowed_roots=[allowed],
        forbidden_roots=[],
        required_services=("backend", "frontend"),
    )
    assert report.runtime_evidence_valid is True
    by_service = {s.service: s for s in report.services}
    assert by_service["backend"].ok is True
    assert by_service["frontend"].ok is True
    assert by_service["backend"].bind_mounts_under_allowed == 1
    assert by_service["frontend"].bind_mounts_under_allowed == 1


def test_forbidden_root_fails(tmp_path: Path) -> None:
    allowed = tmp_path / "coordinator-main"
    forbidden = tmp_path / "Forcastin"
    allowed.mkdir()
    forbidden.mkdir()
    backend_src = forbidden / "backend"   # WRONG — under forbidden root
    frontend_src = allowed / "frontend"
    backend_src.mkdir()
    frontend_src.mkdir()

    payload = _sample_inspect_json(str(backend_src), str(frontend_src))
    mounts = load_mounts_from_docker_inspect(payload)

    report = evaluate_mounts(
        mounts,
        allowed_roots=[allowed],
        forbidden_roots=[forbidden],
        required_services=("backend", "frontend"),
    )
    assert report.runtime_evidence_valid is False
    by_service = {s.service: s for s in report.services}
    assert by_service["backend"].ok is False
    assert by_service["backend"].bind_mounts_under_forbidden == 1
    assert by_service["frontend"].ok is True


def test_unknown_source_fails_closed(tmp_path: Path) -> None:
    allowed = tmp_path / "coordinator-main"
    allowed.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    backend_src = other / "backend"      # WRONG — under no allowed root
    frontend_src = allowed / "frontend"
    backend_src.mkdir()
    frontend_src.mkdir()

    payload = _sample_inspect_json(str(backend_src), str(frontend_src))
    mounts = load_mounts_from_docker_inspect(payload)

    report = evaluate_mounts(
        mounts,
        allowed_roots=[allowed],
        forbidden_roots=[],
        required_services=("backend", "frontend"),
    )
    assert report.runtime_evidence_valid is False
    by_service = {s.service: s for s in report.services}
    assert by_service["backend"].ok is False
    assert by_service["backend"].bind_mounts_unknown_source == 1


def test_missing_service_fails_closed() -> None:
    payload = json.dumps([
        {
            "Config": {
                "Labels": {"com.docker.compose.service": "backend"},
            },
            "Mounts": [
                {"Type": "bind", "Source": "/some/path", "Destination": "/app"},
            ],
        },
        # frontend is intentionally absent
    ])
    mounts = load_mounts_from_docker_inspect(payload)
    report = evaluate_mounts(
        mounts,
        allowed_roots=[Path("/some")],
        required_services=("backend", "frontend"),
    )
    assert report.runtime_evidence_valid is False
    by_service = {s.service: s for s in report.services}
    assert by_service["frontend"].ok is False
    assert by_service["frontend"].bind_mounts_seen == 0


def test_volumes_and_tmpfs_are_ignored(tmp_path: Path) -> None:
    allowed = tmp_path / "coordinator-main"
    allowed.mkdir()
    backend_src = allowed / "backend"
    backend_src.mkdir()

    payload = json.dumps([
        {
            "Config": {
                "Labels": {"com.docker.compose.service": "backend"},
            },
            "Mounts": [
                {"Type": "bind", "Source": str(backend_src), "Destination": "/app"},
                {"Type": "volume", "Source": "anon", "Destination": "/data"},
                {"Type": "tmpfs", "Source": "", "Destination": "/tmp"},
            ],
        },
    ])
    mounts = load_mounts_from_docker_inspect(payload)
    report = evaluate_mounts(
        mounts,
        allowed_roots=[allowed],
        required_services=("backend",),
    )
    assert report.runtime_evidence_valid is True
    assert report.services[0].bind_mounts_seen == 1


def test_render_includes_runtime_evidence_valid(tmp_path: Path) -> None:
    allowed = tmp_path / "coordinator-main"
    allowed.mkdir()
    backend_src = allowed / "backend"
    backend_src.mkdir()
    mounts = [
        MountSpec(service="backend", source=str(backend_src), destination="/app"),
    ]
    report = evaluate_mounts(
        mounts,
        allowed_roots=[allowed],
        required_services=("backend",),
    )
    text = render_provenance_report(report)
    assert "backend_mount_ok = true" in text
    assert "runtime_evidence_valid = true" in text
