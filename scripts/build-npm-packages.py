#!/usr/bin/env python3
"""Build npm packages from an existing stable SealDice GitHub Release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


REPOSITORY = "sealdice/sealdice-build"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
GITHUB_API_URL = "https://api.github.com"
MANIFEST_SCHEMA_VERSION = 1
STABLE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class BuildError(RuntimeError):
    """A release cannot be converted into valid npm packages."""


@dataclass(frozen=True)
class Platform:
    key: str
    package_name: str
    npm_os: str
    npm_cpu: str
    release_os: str
    release_arch: str
    archive_suffix: str
    binary_name: str

    def asset_name(self, version: str) -> str:
        return f"sealdice-core_{version}_{self.release_os}_{self.release_arch}{self.archive_suffix}"


PLATFORMS = (
    Platform(
        "win32-x64",
        "@sealtrpg/sealdice-win32-x64",
        "win32",
        "x64",
        "windows",
        "amd64",
        ".zip",
        "sealdice-core.exe",
    ),
    Platform(
        "darwin-x64",
        "@sealtrpg/sealdice-darwin-x64",
        "darwin",
        "x64",
        "darwin",
        "amd64",
        ".tar.gz",
        "sealdice-core",
    ),
    Platform(
        "darwin-arm64",
        "@sealtrpg/sealdice-darwin-arm64",
        "darwin",
        "arm64",
        "darwin",
        "arm64",
        ".tar.gz",
        "sealdice-core",
    ),
    Platform(
        "linux-x64",
        "@sealtrpg/sealdice-linux-x64",
        "linux",
        "x64",
        "linux",
        "amd64",
        ".tar.gz",
        "sealdice-core",
    ),
    Platform(
        "linux-arm64",
        "@sealtrpg/sealdice-linux-arm64",
        "linux",
        "arm64",
        "linux",
        "arm64",
        ".tar.gz",
        "sealdice-core",
    ),
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def request_json(url: str, token: str | None = None) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sealdice-npm-release-builder",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise BuildError(f"GitHub API request failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        raise BuildError(f"GitHub API request failed: {error.reason}") from error


def resolve_release(release_tag: str | None, token: str | None = None) -> dict[str, Any]:
    if release_tag:
        encoded_tag = urllib.parse.quote(release_tag, safe="")
        endpoint = f"{GITHUB_API_URL}/repos/{REPOSITORY}/releases/tags/{encoded_tag}"
    else:
        endpoint = f"{GITHUB_API_URL}/repos/{REPOSITORY}/releases/latest"
    return request_json(endpoint, token)


def validate_release(
    release: dict[str, Any], requested_tag: str | None = None
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not STABLE_TAG.fullmatch(tag):
        raise BuildError(f"Release tag must be stable SemVer in vX.Y.Z form, got: {tag!r}")
    if requested_tag and tag != requested_tag:
        raise BuildError(f"GitHub returned release {tag}, expected {requested_tag}")
    if release.get("draft"):
        raise BuildError(f"Release {tag} is a draft")
    if release.get("prerelease"):
        raise BuildError(f"Release {tag} is a prerelease; npm publishing is stable-only")

    version = tag[1:]
    assets_by_name: dict[str, dict[str, Any]] = {}
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise BuildError(f"Release {tag} has no asset list")
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            continue
        name = asset["name"]
        if name in assets_by_name:
            raise BuildError(f"Release {tag} contains duplicate asset {name}")
        assets_by_name[name] = asset

    expected_names = {platform.asset_name(version) for platform in PLATFORMS}
    missing = sorted(expected_names - assets_by_name.keys())
    if missing:
        raise BuildError(f"Release {tag} is missing npm platform assets: {', '.join(missing)}")
    return tag, version, assets_by_name


def download_asset(asset: dict[str, Any], destination: Path) -> None:
    url = asset.get("browser_download_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BuildError(f"Release asset has an invalid download URL: {asset.get('name')}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "sealdice-npm-release-builder"})
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            expected_size = asset.get("size")
            if isinstance(expected_size, int) and expected_size > 0 and temporary.stat().st_size != expected_size:
                raise BuildError(
                    f"Downloaded size for {asset['name']} is {temporary.stat().st_size}, expected {expected_size}"
                )
            temporary.replace(destination)
            return
        except (OSError, urllib.error.URLError, BuildError) as error:
            last_error = error
            temporary.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(attempt)
    raise BuildError(f"Failed to download {asset.get('name')}: {last_error}")


def normalize_archive_path(value: str) -> PurePosixPath | None:
    if "\0" in value:
        raise BuildError("Archive contains a NUL byte in a path")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise BuildError(f"Archive contains an absolute path: {value}")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise BuildError(f"Archive path escapes extraction root: {value}")
    if not parts:
        return None
    return PurePosixPath(*parts)


def checked_destination(root: Path, relative: PurePosixPath) -> Path:
    resolved_root = root.resolve()
    destination = (resolved_root / Path(*relative.parts)).resolve()
    if destination != resolved_root and resolved_root not in destination.parents:
        raise BuildError(f"Archive path escapes extraction root: {relative}")
    return destination


def copy_stream(source: BinaryIO, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
    try:
        destination.chmod(mode)
    except OSError:
        pass


def extract_zip(archive: Path, destination: Path) -> dict[str, int]:
    modes: dict[str, int] = {}
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as source:
            for info in source.infolist():
                relative = normalize_archive_path(info.filename)
                if relative is None:
                    continue
                key = relative.as_posix().lower()
                if key in seen:
                    raise BuildError(f"Archive contains duplicate path: {relative}")
                seen.add(key)
                raw_mode = (info.external_attr >> 16) & 0xFFFF
                if raw_mode and stat.S_ISLNK(raw_mode):
                    raise BuildError(f"Archive contains a symbolic link: {relative}")
                target = checked_destination(destination, relative)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                mode = stat.S_IMODE(raw_mode) if raw_mode else 0o644
                if mode == 0:
                    mode = 0o644
                with source.open(info) as input_file:
                    copy_stream(input_file, target, mode)
                modes[relative.as_posix()] = mode
    except (OSError, zipfile.BadZipFile) as error:
        raise BuildError(f"Invalid zip archive {archive.name}: {error}") from error
    return modes


def extract_tar(archive: Path, destination: Path) -> dict[str, int]:
    modes: dict[str, int] = {}
    seen: set[str] = set()
    try:
        with tarfile.open(archive, mode="r:gz") as source:
            for member in source:
                relative = normalize_archive_path(member.name)
                if relative is None:
                    continue
                key = relative.as_posix().lower()
                if key in seen:
                    raise BuildError(f"Archive contains duplicate path: {relative}")
                seen.add(key)
                target = checked_destination(destination, relative)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise BuildError(f"Archive contains unsupported special entry: {relative}")
                input_file = source.extractfile(member)
                if input_file is None:
                    raise BuildError(f"Could not read archive entry: {relative}")
                mode = member.mode & 0o777 or 0o644
                with input_file:
                    copy_stream(input_file, target, mode)
                modes[relative.as_posix()] = mode
    except (OSError, tarfile.TarError) as error:
        raise BuildError(f"Invalid tar archive {archive.name}: {error}") from error
    return modes


def extract_archive(archive: Path, destination: Path) -> dict[str, int]:
    destination.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        return extract_zip(archive, destination)
    if archive.name.endswith(".tar.gz"):
        return extract_tar(archive, destination)
    raise BuildError(f"Unsupported release archive: {archive.name}")


def validate_binary_format(binary: Path, platform: Platform) -> None:
    with binary.open("rb") as source:
        header = source.read(4096)
    if platform.npm_os == "win32":
        if len(header) < 64 or header[:2] != b"MZ":
            raise BuildError(f"{binary} is not a Windows PE executable")
        pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
        if pe_offset + 6 > len(header) or header[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise BuildError(f"{binary} has an invalid PE header")
        machine = struct.unpack_from("<H", header, pe_offset + 4)[0]
        if machine != 0x8664:
            raise BuildError(f"{binary} is not a Windows x64 executable")
        return

    if platform.npm_os == "linux":
        if len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2:
            raise BuildError(f"{binary} is not a 64-bit ELF executable")
        endian = "<" if header[5] == 1 else ">" if header[5] == 2 else None
        if endian is None:
            raise BuildError(f"{binary} has an invalid ELF byte order")
        machine = struct.unpack_from(f"{endian}H", header, 18)[0]
        expected = 62 if platform.npm_cpu == "x64" else 183
        if machine != expected:
            raise BuildError(f"{binary} has ELF machine {machine}, expected {expected}")
        return

    if len(header) < 8:
        raise BuildError(f"{binary} is not a Mach-O executable")
    if header[:4] == b"\xcf\xfa\xed\xfe":
        cpu_type = struct.unpack_from("<I", header, 4)[0]
    elif header[:4] == b"\xfe\xed\xfa\xcf":
        cpu_type = struct.unpack_from(">I", header, 4)[0]
    else:
        raise BuildError(f"{binary} is not a 64-bit Mach-O executable")
    expected_cpu = 0x01000007 if platform.npm_cpu == "x64" else 0x0100000C
    if cpu_type != expected_cpu:
        raise BuildError(f"{binary} has Mach-O CPU type {cpu_type:#x}, expected {expected_cpu:#x}")


def payload_manifest(
    payload: Path,
    modes: dict[str, int],
    platform: Platform,
    version: str,
    tag: str,
    release_url: str,
    asset_name: str,
    asset_sha256: str,
) -> dict[str, Any]:
    binary = payload / platform.binary_name
    if not binary.is_file():
        raise BuildError(f"{asset_name} does not contain {platform.binary_name} at its root")
    validate_binary_format(binary, platform)

    files: list[dict[str, Any]] = []
    for file_path in sorted(path for path in payload.rglob("*") if path.is_file()):
        relative = file_path.relative_to(payload).as_posix()
        desired_mode = modes.get(relative, 0o644)
        if relative == platform.binary_name and platform.npm_os != "win32":
            desired_mode = 0o755
        files.append(
            {
                "path": relative,
                "sha256": sha256_file(file_path),
                "size": file_path.stat().st_size,
                "mode": f"{desired_mode & 0o777:04o}",
            }
        )

    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "packageVersion": version,
        "platform": {
            "key": platform.key,
            "os": platform.npm_os,
            "cpu": platform.npm_cpu,
            "binary": platform.binary_name,
        },
        "release": {
            "repository": REPOSITORY,
            "tag": tag,
            "url": release_url,
            "asset": asset_name,
            "assetSha256": asset_sha256,
        },
        "files": files,
    }


def platform_package_json(
    platform: Platform,
    version: str,
    tag: str,
    asset_name: str,
    asset_sha256: str,
) -> dict[str, Any]:
    return {
        "name": platform.package_name,
        "version": version,
        "description": f"Official SealDice runtime for {platform.key}. Install sealdice instead.",
        "os": [platform.npm_os],
        "cpu": [platform.npm_cpu],
        "files": ["payload", "payload-manifest.json", "README.md", "LICENSE"],
        "author": "SealDice Team",
        "license": "MIT",
        "repository": {"type": "git", "url": f"{REPOSITORY_URL}.git"},
        "homepage": "https://sealdice.com",
        "publishConfig": {"access": "public"},
        "sealdiceRelease": {
            "repository": REPOSITORY,
            "tag": tag,
            "asset": asset_name,
            "assetSha256": asset_sha256,
        },
    }


def prepare_output(output_dir: Path, repository_root: Path) -> None:
    if output_dir.is_symlink():
        raise BuildError(f"Refusing to clean symlinked output directory: {output_dir}")
    resolved = output_dir.resolve()
    repository_root = repository_root.resolve()
    filesystem_root = Path(resolved.anchor).resolve()
    source_directories = {
        repository_root / "npm",
        repository_root / "scripts",
        repository_root / "tests",
        repository_root / ".github",
    }
    protected = {
        repository_root,
        filesystem_root,
        *source_directories,
    }
    if resolved in protected or any(source_dir in resolved.parents for source_dir in source_directories):
        raise BuildError(f"Refusing to clean unsafe output directory: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def build_packages(
    release: dict[str, Any],
    output_dir: Path,
    repository_root: Path,
    asset_dir: Path | None = None,
    requested_tag: str | None = None,
) -> dict[str, Any]:
    tag, version, assets_by_name = validate_release(release, requested_tag)
    prepare_output(output_dir, repository_root)
    npm_source = repository_root / "npm"
    release_url = release.get("html_url") or f"{REPOSITORY_URL}/releases/tag/{tag}"
    package_records: list[dict[str, Any]] = []
    release_sources: dict[str, dict[str, str]] = {}

    with tempfile.TemporaryDirectory(prefix="sealdice-npm-assets-") as temporary:
        download_dir = Path(temporary)
        for platform in PLATFORMS:
            asset_name = platform.asset_name(version)
            asset = assets_by_name[asset_name]
            if asset_dir is not None:
                archive = asset_dir / asset_name
                if not archive.is_file():
                    raise BuildError(f"Local asset is missing: {archive}")
            else:
                archive = download_dir / asset_name
                print(f"Downloading {asset_name}...", flush=True)
                download_asset(asset, archive)

            asset_sha256 = sha256_file(archive)
            package_dir = output_dir / "packages" / platform.key
            payload = package_dir / "payload"
            modes = extract_archive(archive, payload)
            manifest = payload_manifest(
                payload,
                modes,
                platform,
                version,
                tag,
                str(release_url),
                asset_name,
                asset_sha256,
            )
            write_json(package_dir / "payload-manifest.json", manifest)
            package_json = platform_package_json(platform, version, tag, asset_name, asset_sha256)
            write_json(package_dir / "package.json", package_json)
            shutil.copy2(npm_source / "README.md", package_dir / "README.md")
            shutil.copy2(npm_source / "LICENSE", package_dir / "LICENSE")
            package_records.append(
                {
                    "name": platform.package_name,
                    "version": version,
                    "kind": "platform",
                    "platform": platform.key,
                    "path": package_dir.relative_to(output_dir).as_posix(),
                }
            )
            release_sources[platform.package_name] = package_json["sealdiceRelease"]
            print(f"Prepared {platform.package_name}@{version}", flush=True)

    main_dir = output_dir / "main"
    (main_dir / "bin").mkdir(parents=True)
    shutil.copy2(npm_source / "bin" / "sealdice.js", main_dir / "bin" / "sealdice.js")
    shutil.copy2(npm_source / "README.md", main_dir / "README.md")
    shutil.copy2(npm_source / "LICENSE", main_dir / "LICENSE")
    main_package = json.loads((npm_source / "package.json").read_text(encoding="utf-8"))
    main_package.pop("private", None)
    main_package["version"] = version
    main_package["optionalDependencies"] = {
        platform.package_name: version for platform in PLATFORMS
    }
    main_package["sealdiceRelease"] = {
        "repository": REPOSITORY,
        "tag": tag,
        "assets": release_sources,
    }
    main_package.pop("scripts", None)
    write_json(main_dir / "package.json", main_package)
    package_records.append(
        {
            "name": "sealdice",
            "version": version,
            "kind": "main",
            "path": main_dir.relative_to(output_dir).as_posix(),
        }
    )

    build_manifest = {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "repository": REPOSITORY,
        "releaseTag": tag,
        "releaseUrl": release_url,
        "version": version,
        "packages": package_records,
        "publishOrder": [platform.package_name for platform in PLATFORMS] + ["sealdice"],
    }
    write_json(output_dir / "build-manifest.json", build_manifest)
    return build_manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build SealDice npm packages from an existing stable GitHub Release"
    )
    parser.add_argument(
        "--release-tag",
        default="",
        help="Release tag such as v1.5.1; blank selects the latest stable release",
    )
    parser.add_argument(
        "--release-metadata",
        type=Path,
        help="Read GitHub Release JSON from a local file instead of the API",
    )
    parser.add_argument(
        "--asset-dir",
        type=Path,
        help="Use already-downloaded release assets from this directory",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Generated package directory (default: .npm-dist)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    output_dir = (args.output_dir or repository_root / ".npm-dist").resolve()
    requested_tag = args.release_tag.strip() or None
    try:
        if args.release_metadata:
            release = json.loads(args.release_metadata.read_text(encoding="utf-8"))
        else:
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            release = resolve_release(requested_tag, token)
        manifest = build_packages(
            release,
            output_dir,
            repository_root,
            asset_dir=args.asset_dir.resolve() if args.asset_dir else None,
            requested_tag=requested_tag,
        )
    except (BuildError, OSError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as output:
            output.write(f"version={manifest['version']}\n")
            output.write(f"release_tag={manifest['releaseTag']}\n")
            output.write(f"release_url={manifest['releaseUrl']}\n")
    print(
        f"Built {len(manifest['packages'])} npm packages for {manifest['releaseTag']} in {output_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
