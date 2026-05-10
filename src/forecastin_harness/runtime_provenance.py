"""Runtime provenance guard — verify Docker mounts come from the right root.

Background
==========

The 2026-05-09 drain produced runtime evidence (logs, curl payloads,
DB rows) that turned out to be invalid because backend / frontend
containers were bind-mounting from the legacy ``J:/Forcastin``
checkout instead of the worktree the operator was editing. The
operator paid for the time twice: once running the test, once
discovering the test had run against stale code.

This module classifies a list of Docker mounts (typed as
``MountSpec``) and refuses to call evidence "valid" unless the bind
sources match an allowed root.

Design
------

* Pure synthesis: :func:`evaluate_mounts` accepts an iterable of
  :class:`MountSpec` and returns a :class:`ProvenanceReport`. The CLI
  parses ``docker inspect`` JSON; tests inject a fixed list and never
  touch the daemon.
* Mount classification is conservative: only ``Source`` paths that
  resolve under one of ``allowed_roots`` pass. ``forbidden_roots``
  win regardless. Anything else is flagged ``unknown`` and treated as
  failure (fail-closed).
* The report breaks results out per ``service`` so the CLI can print
  ``backend_mount_ok`` / ``frontend_mount_ok`` independently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .root_guard import DEFAULT_EXPECTED_ROOT, DEFAULT_FORBIDDEN_ROOTS, _is_under, _resolve


@dataclass(frozen=True)
class MountSpec:
    """One bind mount on a Docker container.

    ``service`` is the operator-facing name we want to classify by
    (``"backend"``, ``"frontend"``, …); the CLI maps from a container
    name or compose service via :func:`load_mounts_from_docker_inspect`.

    ``source`` is the host path (``Mounts[].Source`` in ``docker inspect``).
    ``destination`` is the in-container path. ``mount_type`` is one of
    ``"bind"`` / ``"volume"`` / ``"tmpfs"`` (case-insensitive).
    """

    service: str
    source: str  # raw, pre-resolution
    destination: str
    mount_type: str = "bind"


@dataclass(frozen=True)
class ServiceVerdict:
    """Per-service verdict in :class:`ProvenanceReport`."""

    service: str
    ok: bool
    bind_mounts_seen: int
    bind_mounts_under_allowed: int
    bind_mounts_under_forbidden: int
    bind_mounts_unknown_source: int
    detail: str = ""


@dataclass(frozen=True)
class ProvenanceReport:
    """Result of evaluating a set of mounts.

    ``runtime_evidence_valid`` is the single boolean operators check.
    It is the AND of every required service verdict's ``ok`` field.
    """

    services: tuple[ServiceVerdict, ...]
    allowed_roots: tuple[Path, ...]
    forbidden_roots: tuple[Path, ...]
    required_services: tuple[str, ...]
    runtime_evidence_valid: bool
    notes: tuple[str, ...] = field(default_factory=tuple)


def evaluate_mounts(
    mounts: Iterable[MountSpec],
    *,
    allowed_roots: Iterable[Path] = (DEFAULT_EXPECTED_ROOT,),
    forbidden_roots: Iterable[Path] = DEFAULT_FORBIDDEN_ROOTS,
    required_services: Iterable[str] = ("backend", "frontend"),
) -> ProvenanceReport:
    """Classify every bind mount and return a :class:`ProvenanceReport`.

    A service passes (``ServiceVerdict.ok == True``) when at least one
    bind mount resolves under an ``allowed_roots`` entry AND no bind
    mount resolves under a ``forbidden_roots`` entry. Volumes and
    tmpfs mounts are ignored — the failure mode the guard exists for
    is bind-mounting source code from the wrong host path.
    """
    allowed = tuple(_resolve(p) for p in allowed_roots)
    forbidden = tuple(_resolve(p) for p in forbidden_roots)
    required = tuple(required_services)

    # Group bind mounts by service.
    by_service: dict[str, list[MountSpec]] = {}
    for m in mounts:
        if m.mount_type.lower() != "bind":
            continue
        by_service.setdefault(m.service, []).append(m)

    services: list[ServiceVerdict] = []
    for svc in required:
        svc_mounts = by_service.get(svc, [])
        seen = len(svc_mounts)
        under_allowed = 0
        under_forbidden = 0
        unknown_source = 0
        for m in svc_mounts:
            try:
                src = _resolve(Path(m.source))
            except (OSError, ValueError):
                unknown_source += 1
                continue
            if any(src == f or _is_under(src, f) for f in forbidden):
                under_forbidden += 1
                continue
            if any(src == a or _is_under(src, a) for a in allowed):
                under_allowed += 1
                continue
            unknown_source += 1

        ok = seen > 0 and under_allowed > 0 and under_forbidden == 0 and unknown_source == 0
        if seen == 0:
            detail = f"no bind mounts seen for service {svc!r}"
        elif under_forbidden > 0:
            detail = (
                f"{under_forbidden} bind mount(s) for {svc!r} resolve under "
                f"forbidden roots {[str(p) for p in forbidden]}"
            )
        elif unknown_source > 0:
            detail = (
                f"{unknown_source} bind mount(s) for {svc!r} did not resolve "
                f"under any allowed root {[str(p) for p in allowed]}"
            )
        else:
            detail = (
                f"all {under_allowed} bind mount(s) for {svc!r} resolve under "
                f"allowed roots"
            )
        services.append(
            ServiceVerdict(
                service=svc,
                ok=ok,
                bind_mounts_seen=seen,
                bind_mounts_under_allowed=under_allowed,
                bind_mounts_under_forbidden=under_forbidden,
                bind_mounts_unknown_source=unknown_source,
                detail=detail,
            )
        )

    runtime_valid = all(s.ok for s in services) and len(services) == len(required)
    return ProvenanceReport(
        services=tuple(services),
        allowed_roots=allowed,
        forbidden_roots=forbidden,
        required_services=required,
        runtime_evidence_valid=runtime_valid,
    )


def load_mounts_from_docker_inspect(
    raw: str | bytes | Mapping[str, object],
    *,
    service_label: str = "com.docker.compose.service",
) -> list[MountSpec]:
    """Parse ``docker inspect`` JSON output into a list of :class:`MountSpec`.

    ``raw`` may be the JSON text, bytes, or an already-parsed list /
    dict. Each container is inspected for its ``Mounts`` array and its
    compose-service label; a container without a service label is
    skipped (the guard cannot classify what it cannot name).
    """
    if isinstance(raw, (str, bytes)):
        data = json.loads(raw)
    else:
        data = raw
    if not isinstance(data, list):
        # ``docker inspect`` always returns a JSON array; if a single
        # container was inspected directly, normalise to a list of one
        # so downstream code stays uniform.
        data = [data]

    out: list[MountSpec] = []
    for container in data:
        if not isinstance(container, Mapping):
            continue
        config = container.get("Config")
        labels: Mapping[str, str] = {}
        if isinstance(config, Mapping):
            raw_labels = config.get("Labels")
            if isinstance(raw_labels, Mapping):
                labels = {str(k): str(v) for k, v in raw_labels.items()}
        service = labels.get(service_label) or ""
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or not service:
            continue
        for m in mounts:
            if not isinstance(m, Mapping):
                continue
            mtype = str(m.get("Type") or "bind")
            src = str(m.get("Source") or "")
            dst = str(m.get("Destination") or "")
            if not src:
                continue
            out.append(
                MountSpec(
                    service=service,
                    source=src,
                    destination=dst,
                    mount_type=mtype,
                )
            )
    return out


def render_provenance_report(report: ProvenanceReport) -> str:
    """Human-readable rendering used by the CLI."""
    lines: list[str] = []
    for svc in report.services:
        flag = "ok" if svc.ok else "FAIL"
        lines.append(f"{svc.service}_mount_ok = {str(svc.ok).lower()}  ({flag})")
        lines.append(f"  {svc.detail}")
    valid = "true" if report.runtime_evidence_valid else "false"
    lines.append(f"runtime_evidence_valid = {valid}")
    return "\n".join(lines)
