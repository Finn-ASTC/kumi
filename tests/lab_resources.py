"""Cooperative reservations for development experiments, not OS isolation."""

import ipaddress
from pathlib import Path
from typing import Any

import e2e_runner  # noqa: F401  Adds the packaged helpers to sys.path.
import jobs
import protocol
import watch


def normalize(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize an explicit local path or numeric bind address before comparison."""
    protocol.require(isinstance(value, dict), "resource must be an object")
    jobs.require_text(value.get("owner"), "resource owner")
    kind = value.get("kind")
    if kind == "path":
        protocol.require(set(value) == {"kind", "owner", "path", "purpose"}, "invalid path resource")
        protocol.require(value["purpose"] in ("workspace", "build", "database"), "invalid path purpose")
        protocol.require(isinstance(value["path"], str) and Path(value["path"]).is_absolute(),
                         "absolute resource path required")
        return {**value, "path": str(Path(value["path"]).resolve())}
    protocol.require(kind == "port" and set(value) == {"kind", "owner", "address", "port", "protocol"},
                     "invalid port resource")
    protocol.require(value["protocol"] in ("tcp", "udp") and type(value["port"]) is int
                     and 1 <= value["port"] <= 65535, "invalid port/protocol")
    address = ipaddress.ip_address(value["address"])
    return {**value, "address": str(address)}


def overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Conservatively detect directory ancestry and wildcard socket conflicts."""
    if left["kind"] != right["kind"]:
        return False
    if left["kind"] == "path":
        a, b = Path(left["path"]), Path(right["path"])
        return a.is_relative_to(b) or b.is_relative_to(a)
    if (left["protocol"], left["port"]) != (right["protocol"], right["port"]):
        return False
    a, b = ipaddress.ip_address(left["address"]), ipaddress.ip_address(right["address"])
    a = getattr(a, "ipv4_mapped", None) or a
    b = getattr(b, "ipv4_mapped", None) or b
    return a == b or a.is_unspecified or b.is_unspecified


def validate(resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Allow nested paths for one owner, reject competing owners in one recipe."""
    protocol.require(isinstance(resources, list), "resources must be an array")
    normalized = [normalize(item) for item in resources]
    for i, item in enumerate(normalized):
        for previous in normalized[:i]:
            protocol.require(item != previous, "duplicate resource declaration")
            protocol.require(item["owner"] == previous["owner"] or not overlaps(item, previous),
                             "conflicting resource owners in experiment")
    return normalized


def directory(path: str | Path) -> Path:
    """Reject redirected registry roots; all cooperating labs share this directory."""
    root = Path(path).absolute()
    protocol.require(root.is_dir() and root == root.resolve(), "existing canonical registry directory required")
    protocol.require(not (root / ".lock").is_symlink(), "registry lock redirected")
    return root


def read(root: Path) -> dict[str, Any]:
    """Read committed claims; missing initial state is the only empty case."""
    path = root / "reservations.json"
    if not path.exists() and not path.is_symlink():
        return {"version": 1, "claims": {}}
    data = protocol.read_json(path)
    protocol.require(set(data) == {"version", "claims"} and type(data["version"]) is int
                     and data["version"] == 1 and isinstance(data["claims"], dict), "invalid resource registry")
    for key, claim in data["claims"].items():
        protocol.require(jobs.ID.fullmatch(key) and set(claim) == {"manifest_path", "resources", "status", "release"}
                         and claim["status"] in ("held", "released"), "invalid resource claim")
        protocol.require(validate(claim["resources"]) == claim["resources"], "resource path redirected")
    return data


def reserve(root: str | Path, experiment_id: str, manifest: str,
            resources: list[dict[str, Any]]) -> dict[str, Any]:
    """Atomically claim all resources, retaining ownership across process crashes."""
    root = directory(root)
    protocol.require(jobs.ID.fullmatch(experiment_id) is not None, "invalid experiment ID")
    normalized = validate(resources)
    claim = {"manifest_path": manifest, "resources": normalized, "status": "held", "release": None}
    with jobs.locked(root):
        data = read(root)
        if experiment_id in data["claims"]:
            protocol.require(data["claims"][experiment_id] == claim, "reservation changed or was released")
            return claim
        for existing in data["claims"].values():
            if existing["status"] == "released":
                continue
            for item in normalized:
                protocol.require(not any(overlaps(item, other) for other in existing["resources"]),
                                 "resource already reserved by another experiment")
        data["claims"][experiment_id] = claim
        watch.atomic_save(root / "reservations.json", data)
        jobs.sync_directory(root)
    return claim


def check(root: str | Path, experiment_id: str, manifest: str,
          resources: list[dict[str, Any]]) -> None:
    """An expired controller does not automatically free experiment resources."""
    data = read(directory(root))
    expected = {"manifest_path": manifest, "resources": validate(resources), "status": "held", "release": None}
    protocol.require(data["claims"].get(experiment_id) == expected, "resource reservation missing or changed")


def release(root: str | Path, experiment_id: str, evidence: dict[str, Any]) -> None:
    """Record an explicit verified release; never delete a claim or expire it by TTL."""
    root = directory(root)
    with jobs.locked(root):
        data = read(root)
        claim = data["claims"][experiment_id]
        if claim["status"] == "released":
            protocol.require(claim["release"] == evidence, "release evidence changed")
            return
        claim.update(status="released", release=evidence)
        watch.atomic_save(root / "reservations.json", data)
        jobs.sync_directory(root)
