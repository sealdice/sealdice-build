#!/usr/bin/env python3
"""Pack and publish generated SealDice npm packages in a recoverable order."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA_VERSION = 1
MIN_NODE_VERSION_FOR_OIDC = (22, 14, 0)
MIN_NPM_VERSION_FOR_OIDC = (11, 5, 1)


class PublishError(RuntimeError):
    """Generated packages are invalid or cannot be published safely."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": check,
    }
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name == "nt":
        return subprocess.run(subprocess.list2cmdline(command), shell=True, **kwargs)
    return subprocess.run(command, **kwargs)


def run_npm(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = True,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return run_command(["npm", *arguments], cwd=cwd, capture=capture, check=check)


def parse_json_output(output: str, description: str) -> Any:
    stripped = output.strip()
    if not stripped:
        raise PublishError(f"{description} returned no JSON")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as error:
        array_start = stripped.find("[")
        array_end = stripped.rfind("]")
        object_start = stripped.find("{")
        object_end = stripped.rfind("}")
        candidates = []
        if array_start >= 0 and array_end > array_start:
            candidates.append(stripped[array_start : array_end + 1])
        if object_start >= 0 and object_end > object_start:
            candidates.append(stripped[object_start : object_end + 1])
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        raise PublishError(f"Could not parse {description} JSON: {error}") from error


def load_build_manifest(dist_dir: Path) -> dict[str, Any]:
    path = dist_dir / "build-manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublishError(f"Failed to read {path}: {error}") from error
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        raise PublishError("Unsupported npm build manifest schema")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(manifest.get("version", ""))):
        raise PublishError("Only stable X.Y.Z npm versions can be published")
    packages = manifest.get("packages")
    order = manifest.get("publishOrder")
    if not isinstance(packages, list) or not isinstance(order, list):
        raise PublishError("Build manifest is missing packages or publishOrder")
    by_name = {record.get("name"): record for record in packages if isinstance(record, dict)}
    if len(by_name) != len(packages) or set(order) != set(by_name):
        raise PublishError("Build manifest package order is incomplete or contains duplicates")
    if not order or order[-1] != "sealdice":
        raise PublishError("The sealdice main package must be published last")
    return manifest


def package_records_in_order(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    by_name = {record["name"]: record for record in manifest["packages"]}
    return [by_name[name] for name in manifest["publishOrder"]]


def package_sources_sha256(dist_dir: Path, manifest: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for record in package_records_in_order(manifest):
        package_dir = (dist_dir / record["path"]).resolve()
        for file_path in sorted(path for path in package_dir.rglob("*") if path.is_file()):
            relative = file_path.relative_to(dist_dir).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            with file_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def load_package_json(dist_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    package_dir = (dist_dir / record["path"]).resolve()
    if dist_dir.resolve() not in package_dir.parents:
        raise PublishError(f"Package path escapes build directory: {record['path']}")
    try:
        package_json = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PublishError(f"Failed to read package.json for {record['name']}: {error}") from error
    if package_json.get("name") != record["name"] or package_json.get("version") != record["version"]:
        raise PublishError(f"Package metadata does not match build manifest: {record['name']}")
    if not isinstance(package_json.get("sealdiceRelease"), dict):
        raise PublishError(f"Package has no SealDice release provenance: {record['name']}")
    return package_json


def validate_pack_files(record: dict[str, Any], result: dict[str, Any]) -> None:
    files = result.get("files")
    paths = {entry.get("path") for entry in files or [] if isinstance(entry, dict)}
    if record["kind"] == "main":
        required = {"bin/sealdice.js", "README.md", "LICENSE", "package.json"}
    else:
        binary = "sealdice-core.exe" if record["platform"] == "win32-x64" else "sealdice-core"
        required = {
            "payload-manifest.json",
            f"payload/{binary}",
            "README.md",
            "LICENSE",
            "package.json",
        }
    missing = sorted(required - paths)
    if missing:
        raise PublishError(f"Packed {record['name']} is missing: {', '.join(missing)}")


def create_tarballs(dist_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    tarball_dir = dist_dir / "tarballs"
    if tarball_dir.exists():
        shutil.rmtree(tarball_dir)
    tarball_dir.mkdir()

    packed: list[dict[str, Any]] = []
    for record in package_records_in_order(manifest):
        package_dir = (dist_dir / record["path"]).resolve()
        load_package_json(dist_dir, record)
        print(f"Packing {record['name']}@{record['version']}...", flush=True)
        result = run_npm(
            ["pack", "--json", "--pack-destination", str(tarball_dir), str(package_dir)],
            cwd=dist_dir,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PublishError(f"npm pack failed for {record['name']}: {detail}")
        pack_result = parse_json_output(result.stdout, f"npm pack for {record['name']}")
        if not isinstance(pack_result, list) or len(pack_result) != 1:
            raise PublishError(f"npm pack returned an unexpected result for {record['name']}")
        item = pack_result[0]
        if item.get("name") != record["name"] or item.get("version") != record["version"]:
            raise PublishError(f"npm pack metadata mismatch for {record['name']}")
        validate_pack_files(record, item)
        tarball = tarball_dir / item["filename"]
        if not tarball.is_file():
            raise PublishError(f"npm pack did not create {tarball}")
        packed.append(
            {
                "name": record["name"],
                "version": record["version"],
                "kind": record["kind"],
                "tarball": tarball.relative_to(dist_dir).as_posix(),
                "size": tarball.stat().st_size,
                "sha256": sha256_file(tarball),
                "npmIntegrity": item.get("integrity"),
            }
        )

    pack_manifest = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "buildManifestSha256": sha256_file(dist_dir / "build-manifest.json"),
        "packageSourcesSha256": package_sources_sha256(dist_dir, manifest),
        "packages": packed,
    }
    write_json(dist_dir / "pack-manifest.json", pack_manifest)
    return pack_manifest


def load_reusable_tarballs(
    dist_dir: Path, build_manifest: dict[str, Any]
) -> dict[str, Any] | None:
    path = dist_dir / "pack-manifest.json"
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        return None
    if manifest.get("buildManifestSha256") != sha256_file(dist_dir / "build-manifest.json"):
        return None
    if manifest.get("packageSourcesSha256") != package_sources_sha256(dist_dir, build_manifest):
        return None
    packages = manifest.get("packages")
    if not isinstance(packages, list):
        return None
    for record in packages:
        tarball = dist_dir / str(record.get("tarball", ""))
        if not tarball.is_file() or sha256_file(tarball) != record.get("sha256"):
            return None
    return manifest


def parse_version(value: str) -> tuple[int, int, int]:
    cleaned = value.strip().lstrip("v").split("-", 1)[0]
    numbers = []
    for part in cleaned.split(".")[:3]:
        try:
            numbers.append(int(part))
        except ValueError:
            numbers.append(0)
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def command_version(command: list[str]) -> tuple[str, tuple[int, int, int]]:
    result = run_command(command)
    if result.returncode != 0:
        raise PublishError(f"Could not run {' '.join(command)}: {(result.stderr or result.stdout).strip()}")
    text = result.stdout.strip()
    return text, parse_version(text)


def check_authentication() -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        if not os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN") or not os.environ.get(
            "ACTIONS_ID_TOKEN_REQUEST_URL"
        ):
            raise PublishError("GitHub Actions OIDC is unavailable; grant id-token: write")
        node_text, node_version = command_version(["node", "--version"])
        npm_text, npm_version = command_version(["npm", "--version"])
        if node_version < MIN_NODE_VERSION_FOR_OIDC:
            raise PublishError(f"npm OIDC requires Node.js >= 22.14.0, got {node_text}")
        if npm_version < MIN_NPM_VERSION_FOR_OIDC:
            raise PublishError(f"npm OIDC requires npm >= 11.5.1, got {npm_text}")
        print(f"Using npm Trusted Publishing with Node.js {node_text} and npm {npm_text}")
        return

    result = run_npm(["whoami"])
    if result.returncode != 0:
        raise PublishError("npm authentication failed; run npm login before the bootstrap publish")
    print(f"Publishing as npm user {result.stdout.strip()}")


def npm_view_json(spec: str, field: str) -> tuple[bool, Any]:
    result = run_npm(["view", spec, field, "--json"])
    if result.returncode == 0:
        output = result.stdout.strip()
        return True, json.loads(output) if output else None
    detail = f"{result.stdout}\n{result.stderr}"
    if "E404" in detail or "404 Not Found" in detail or "is not in this registry" in detail:
        return False, None
    raise PublishError(f"npm view failed for {spec}: {detail.strip()}")


def preflight_registry(
    dist_dir: Path, build_manifest: dict[str, Any]
) -> dict[str, str]:
    status: dict[str, str] = {}
    for record in package_records_in_order(build_manifest):
        package_json = load_package_json(dist_dir, record)
        spec = f"{record['name']}@{record['version']}"
        exists, version = npm_view_json(spec, "version")
        if not exists:
            status[record["name"]] = "publish"
            continue
        if version != record["version"]:
            raise PublishError(f"Registry returned unexpected version metadata for {spec}: {version!r}")
        _, remote_source = npm_view_json(spec, "sealdiceRelease")
        if remote_source != package_json["sealdiceRelease"]:
            raise PublishError(
                f"{spec} already exists but its SealDice release provenance does not match this build"
            )
        status[record["name"]] = "skip"
    return status


def append_github_summary(
    build_manifest: dict[str, Any], pack_manifest: dict[str, Any], status: dict[str, str]
) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    sizes = {record["name"]: record["size"] for record in pack_manifest["packages"]}
    lines = [
        "## SealDice published to npm",
        "",
        f"- Release: [{build_manifest['releaseTag']}]({build_manifest['releaseUrl']})",
        f"- Version: `{build_manifest['version']}`",
        f"- Install: `npm install --global sealdice@{build_manifest['version']}`",
        "",
        "| Package | Result | Tarball size |",
        "| --- | --- | ---: |",
    ]
    for name in build_manifest["publishOrder"]:
        size_mib = sizes[name] / (1024 * 1024)
        result = "already present" if status[name] == "skip" else "published"
        lines.append(f"| `{name}` | {result} | {size_mib:.1f} MiB |")
    lines.append("")
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines))


def publish(
    dist_dir: Path,
    build_manifest: dict[str, Any],
    pack_manifest: dict[str, Any],
    dist_tag: str,
) -> None:
    status = preflight_registry(dist_dir, build_manifest)
    if any(value == "publish" for value in status.values()):
        check_authentication()
    packed_by_name = {record["name"]: record for record in pack_manifest["packages"]}
    for record in package_records_in_order(build_manifest):
        name = record["name"]
        if status[name] == "skip":
            print(f"Skipping existing {name}@{record['version']} (release provenance matches)")
            continue
        tarball = (dist_dir / packed_by_name[name]["tarball"]).resolve()
        print(f"Publishing {name}@{record['version']}...", flush=True)
        result = run_npm(
            ["publish", str(tarball), "--access", "public", "--tag", dist_tag],
            cwd=dist_dir,
            capture=False,
        )
        if result.returncode != 0:
            raise PublishError(f"npm publish failed for {name}@{record['version']}")
    append_github_summary(build_manifest, pack_manifest, status)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pack and publish generated SealDice npm packages")
    parser.add_argument(
        "--dist-dir",
        type=Path,
        help="Generated package directory (default: .npm-dist)",
    )
    parser.add_argument("--tag", choices=("latest",), default="latest", help="npm dist-tag")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Create and validate tarballs without checking auth or publishing",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    dist_dir = (args.dist_dir or repository_root / ".npm-dist").resolve()
    try:
        build_manifest = load_build_manifest(dist_dir)
        pack_manifest = load_reusable_tarballs(dist_dir, build_manifest)
        if pack_manifest is None:
            pack_manifest = create_tarballs(dist_dir, build_manifest)
        else:
            print("Reusing validated npm tarballs from .npm-dist/tarballs")
        if args.dry_run:
            total = sum(record["size"] for record in pack_manifest["packages"])
            print(
                f"Validated {len(pack_manifest['packages'])} npm tarballs "
                f"({total / (1024 * 1024):.1f} MiB total)"
            )
            return 0
        publish(dist_dir, build_manifest, pack_manifest, args.tag)
    except (PublishError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
