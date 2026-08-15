"""Generate a deterministic CycloneDX SBOM from committed dependency locks."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import tomllib
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

LOCKFILE_NAMES = frozenset({"Cargo.lock", "package-lock.json", "uv.lock"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _tracked_lockfiles(repo_root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--cached", "-z"],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not enumerate committed lockfiles: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"could not enumerate committed lockfiles: {detail or 'git ls-files failed'}"
        )
    lockfiles = sorted(
        (
            repo_root / Path(value.decode("utf-8", errors="surrogateescape"))
            for value in result.stdout.split(b"\0")
            if value
            and Path(value.decode("utf-8", errors="surrogateescape")).name
            in LOCKFILE_NAMES
        ),
        key=lambda path: path.relative_to(repo_root).as_posix(),
    )
    if not lockfiles:
        raise RuntimeError("no committed dependency lockfiles were found")
    missing = [
        path.relative_to(repo_root).as_posix() for path in lockfiles if not path.is_file()
    ]
    if missing:
        raise RuntimeError(
            f"committed lockfile is missing from checkout: {', '.join(missing)}"
        )
    return lockfiles


def _hash_entry(algorithm: str, content: str) -> dict[str, str]:
    return {"alg": algorithm, "content": content.lower()}


def _component(
    *,
    ecosystem: str,
    name: str,
    version: str,
    lockfile: str,
    hashes: list[dict[str, str]] | None = None,
    license_name: str | None = None,
) -> dict[str, Any]:
    encoded_name = quote(name, safe="/")
    purl = f"pkg:{ecosystem}/{encoded_name}@{quote(version, safe='')}"
    component: dict[str, Any] = {
        "type": "library",
        "bom-ref": purl,
        "name": name,
        "version": version,
        "purl": purl,
        "properties": [{"name": "ace:lockfile", "value": lockfile}],
    }
    if hashes:
        component["hashes"] = hashes
    if license_name:
        component["licenses"] = [{"license": {"name": license_name}}]
    return component


def _uv_components(path: Path, relative: str) -> list[dict[str, Any]]:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    components: list[dict[str, Any]] = []
    for package in document.get("package", []):
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        hashes: list[dict[str, str]] = []
        sdist = package.get("sdist")
        if isinstance(sdist, dict):
            digest = sdist.get("hash")
            if isinstance(digest, str) and digest.startswith("sha256:"):
                hashes.append(_hash_entry("SHA-256", digest.removeprefix("sha256:")))
        components.append(
            _component(
                ecosystem="pypi",
                name=name,
                version=version,
                lockfile=relative,
                hashes=hashes,
            )
        )
    return components


def _cargo_components(path: Path, relative: str) -> list[dict[str, Any]]:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    components: list[dict[str, Any]] = []
    for package in document.get("package", []):
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        checksum = package.get("checksum")
        hashes = (
            [_hash_entry("SHA-256", checksum)]
            if isinstance(checksum, str) and len(checksum) == 64
            else []
        )
        components.append(
            _component(
                ecosystem="cargo",
                name=name,
                version=version,
                lockfile=relative,
                hashes=hashes,
            )
        )
    return components


def _npm_name(location: str, package: dict[str, Any]) -> str | None:
    declared = package.get("name")
    if isinstance(declared, str) and declared:
        return declared
    marker = "node_modules/"
    if marker not in location:
        return None
    return location.rsplit(marker, 1)[1]


def _npm_integrity_hash(value: object) -> list[dict[str, str]]:
    if not isinstance(value, str) or "-" not in value:
        return []
    algorithm, encoded = value.split("-", 1)
    if algorithm not in {"sha256", "sha384", "sha512"}:
        return []
    try:
        digest = base64.b64decode(encoded, validate=True).hex()
    except (ValueError, TypeError):
        return []
    return [_hash_entry(algorithm.upper().replace("SHA", "SHA-"), digest)]


def _npm_components(path: Path, relative: str) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    packages = document.get("packages", {})
    if not isinstance(packages, dict):
        raise RuntimeError(f"{relative} does not contain an npm packages map")
    components: list[dict[str, Any]] = []
    for location, raw_package in packages.items():
        if not location or not isinstance(raw_package, dict):
            continue
        name = _npm_name(location, raw_package)
        version = raw_package.get("version")
        if not name or not isinstance(version, str):
            continue
        license_name = raw_package.get("license")
        component = _component(
            ecosystem="npm",
            name=name,
            version=version,
            lockfile=relative,
            hashes=_npm_integrity_hash(raw_package.get("integrity")),
            license_name=license_name if isinstance(license_name, str) else None,
        )
        if raw_package.get("dev") is True:
            component["properties"].append(
                {"name": "ace:npm:development-dependency", "value": "true"}
            )
        components.append(component)
    return components


def _merge_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for component in components:
        purl = component["purl"]
        existing = merged.get(purl)
        if existing is None:
            merged[purl] = component
            continue
        properties = {
            (item["name"], item["value"])
            for item in existing.get("properties", [])
            + component.get("properties", [])
        }
        existing["properties"] = [
            {"name": name, "value": value} for name, value in sorted(properties)
        ]
        hashes = {
            (item["alg"], item["content"])
            for item in existing.get("hashes", []) + component.get("hashes", [])
        }
        if hashes:
            existing["hashes"] = [
                {"alg": algorithm, "content": content}
                for algorithm, content in sorted(hashes)
            ]
    return [merged[purl] for purl in sorted(merged)]


def _runtime_manifest_components(
    manifest_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("runtime manifest is unavailable or is a symlink")
    if manifest_path.stat().st_size > 1024 * 1024:
        raise RuntimeError("runtime manifest exceeds the size limit")
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != 2:
        raise RuntimeError("runtime manifest schema is not supported")
    platform = document.get("platform")
    arch = document.get("arch")
    files = document.get("files")
    if (
        not isinstance(platform, str)
        or not platform
        or not isinstance(arch, str)
        or not arch
        or not isinstance(files, list)
        or not files
    ):
        raise RuntimeError("runtime manifest identity or files are missing")

    provenance = document.get("bwrap_provenance")
    components: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise RuntimeError("runtime manifest contains an invalid file record")
        name = item.get("name")
        digest = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in names
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise RuntimeError("runtime manifest contains unsafe file metadata")
        names.add(name)
        properties = [
            {"name": "ace:runtime-manifest", "value": manifest_path.name},
            {"name": "ace:runtime:platform", "value": platform},
            {"name": "ace:runtime:arch", "value": arch},
            {"name": "ace:file:size", "value": str(size)},
        ]
        if name == "bwrap":
            if not isinstance(provenance, dict):
                raise RuntimeError("bundled bwrap provenance is missing")
            for field in ("source", "version", "license_file"):
                value = provenance.get(field)
                if not isinstance(value, str) or not value or value == "unrecorded":
                    raise RuntimeError("bundled bwrap provenance is incomplete")
                properties.append(
                    {"name": f"ace:bwrap:{field.replace('_', '-')}", "value": value}
                )
        component = {
            "type": "file",
            "bom-ref": (
                f"urn:ace:runtime-file:{quote(platform, safe='')}:"
                f"{quote(arch, safe='')}:{quote(name, safe='')}:sha256:{digest}"
            ),
            "name": name,
            "hashes": [_hash_entry("SHA-256", digest)],
            "properties": properties,
        }
        if name == "bwrap":
            version = str(provenance["version"])
            package_arch = {"x64": "amd64", "arm64": "arm64"}.get(arch, arch)
            component["purl"] = (
                "pkg:deb/ubuntu/bubblewrap@"
                f"{quote(version, safe='')}?arch={quote(package_arch, safe='')}"
                "&distro=ubuntu-24.04"
            )
        components.append(component)

    binary_name = document.get("binary_name")
    binary_hash = document.get("binary_sha256")
    matching_binary = [
        component
        for component in components
        if component["name"] == binary_name
        and component["hashes"][0]["content"] == binary_hash
    ]
    if len(matching_binary) != 1:
        raise RuntimeError("runtime manifest does not bind its declared binary")
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return sorted(components, key=lambda item: item["bom-ref"]), {
        "name": "ace:runtime-manifest:sha256",
        "value": f"sha256:{manifest_digest}",
    }


def generate_sbom(
    repo_root: Path,
    *,
    runtime_manifest: Path | None = None,
) -> dict[str, Any]:
    """Return a deterministic CycloneDX SBOM for locks and staged native files."""
    repo_root = repo_root.resolve()
    lockfiles = _tracked_lockfiles(repo_root)
    components: list[dict[str, Any]] = []
    lock_properties: list[dict[str, str]] = []
    identity_parts: list[str] = []
    for path in lockfiles:
        relative = path.relative_to(repo_root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lock_properties.append(
            {"name": f"ace:lockfile:{relative}", "value": f"sha256:{digest}"}
        )
        identity_parts.append(f"{relative}:{digest}")
        if path.name == "uv.lock":
            components.extend(_uv_components(path, relative))
        elif path.name == "Cargo.lock":
            components.extend(_cargo_components(path, relative))
        else:
            components.extend(_npm_components(path, relative))
    runtime_components: list[dict[str, Any]] = []
    if runtime_manifest is not None:
        runtime_components, manifest_property = _runtime_manifest_components(
            runtime_manifest
        )
        lock_properties.append(manifest_property)
        identity_parts.append(manifest_property["value"])
    serial = uuid.uuid5(uuid.NAMESPACE_URL, "\n".join(identity_parts))
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": "pkg:generic/ace",
                "name": "Ace",
            },
            "properties": lock_properties,
        },
        "components": _merge_components(components) + runtime_components,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path)
    args = parser.parse_args()
    try:
        sbom = generate_sbom(
            args.repo_root,
            runtime_manifest=args.runtime_manifest,
        )
    except (OSError, ValueError, RuntimeError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"could not generate release SBOM: {exc}") from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(sbom, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
