from __future__ import annotations

import importlib.util
import io
import json
import shutil
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, module_name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


build = load_script("build-npm-packages.py", "build_npm_packages")
publish = load_script("publish-npm-packages.py", "publish_npm_packages_for_build_tests")


def executable_bytes(platform) -> bytes:
    data = bytearray(4096)
    if platform.npm_os == "win32":
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 0x80)
        data[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", data, 0x84, 0x8664)
    elif platform.npm_os == "linux":
        data[:4] = b"\x7fELF"
        data[4] = 2
        data[5] = 1
        struct.pack_into("<H", data, 18, 62 if platform.npm_cpu == "x64" else 183)
    else:
        data[:4] = b"\xcf\xfa\xed\xfe"
        cpu = 0x01000007 if platform.npm_cpu == "x64" else 0x0100000C
        struct.pack_into("<I", data, 4, cpu)
    return bytes(data)


def add_tar_file(archive: tarfile.TarFile, name: str, contents: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(contents)
    info.mode = mode
    archive.addfile(info, io.BytesIO(contents))


def create_assets(asset_dir: Path, version: str = "1.5.1") -> list[dict]:
    assets = []
    for platform in build.PLATFORMS:
        name = platform.asset_name(version)
        path = asset_dir / name
        if name.endswith(".zip"):
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(platform.binary_name, executable_bytes(platform))
                archive.writestr("data/decks/seal.json", "{}\n")
        else:
            with tarfile.open(path, "w:gz") as archive:
                add_tar_file(archive, f"./{platform.binary_name}", executable_bytes(platform), 0o755)
                add_tar_file(archive, "./data/decks/seal.json", b"{}\n", 0o644)
        assets.append(
            {
                "name": name,
                "size": path.stat().st_size,
                "browser_download_url": f"https://example.invalid/{name}",
            }
        )
    return assets


def release_with_assets(assets: list[dict], **overrides):
    release = {
        "tag_name": "v1.5.1",
        "draft": False,
        "prerelease": False,
        "html_url": "https://github.com/sealdice/sealdice-build/releases/tag/v1.5.1",
        "assets": assets,
    }
    release.update(overrides)
    return release


class ReleaseValidationTests(unittest.TestCase):
    def test_rejects_prerelease(self):
        with self.assertRaisesRegex(build.BuildError, "prerelease"):
            build.validate_release(release_with_assets([], prerelease=True))

    def test_rejects_non_stable_tag(self):
        with self.assertRaisesRegex(build.BuildError, "stable SemVer"):
            build.validate_release(release_with_assets([], tag_name="v1.6.0-rc.1"))

    def test_reports_all_missing_platform_assets(self):
        with self.assertRaisesRegex(build.BuildError, "windows_amd64"):
            build.validate_release(release_with_assets([]))


class ArchiveSafetyTests(unittest.TestCase):
    def test_zip_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "unsafe.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("../outside.txt", "no")
            with self.assertRaisesRegex(build.BuildError, "escapes"):
                build.extract_archive(archive_path, root / "output")
            self.assertFalse((root / "outside.txt").exists())

    def test_tar_symbolic_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "unsafe.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                info = tarfile.TarInfo("link")
                info.type = tarfile.SYMTYPE
                info.linkname = "target"
                archive.addfile(info)
            with self.assertRaisesRegex(build.BuildError, "special entry"):
                build.extract_archive(archive_path, root / "output")


class PackageBuildTests(unittest.TestCase):
    def test_builds_main_and_five_platform_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            assets.mkdir()
            release = release_with_assets(create_assets(assets))
            output = root / "dist"

            manifest = build.build_packages(
                release,
                output,
                ROOT,
                asset_dir=assets,
                requested_tag="v1.5.1",
            )

            self.assertEqual(manifest["version"], "1.5.1")
            self.assertEqual(len(manifest["packages"]), 6)
            self.assertEqual(manifest["publishOrder"][-1], "sealdice")
            main = json.loads((output / "main" / "package.json").read_text(encoding="utf-8"))
            self.assertNotIn("private", main)
            self.assertEqual(main["name"], "sealdice")
            self.assertEqual(
                main["optionalDependencies"]["@sealtrpg/sealdice-linux-arm64"], "1.5.1"
            )
            linux_manifest = json.loads(
                (output / "packages" / "linux-x64" / "payload-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            binary = next(item for item in linux_manifest["files"] if item["path"] == "sealdice-core")
            self.assertEqual(binary["mode"], "0755")
            self.assertEqual(linux_manifest["release"]["tag"], "v1.5.1")
            windows_manifest = json.loads(
                (output / "packages" / "win32-x64" / "payload-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            windows_binary = next(
                item for item in windows_manifest["files"] if item["path"] == "sealdice-core.exe"
            )
            self.assertNotEqual(windows_binary["mode"], "0755")

    @unittest.skipUnless(shutil.which("npm"), "npm is required for package integration testing")
    def test_generated_packages_pass_npm_pack_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = root / "assets"
            assets.mkdir()
            output = root / "dist"
            build.build_packages(
                release_with_assets(create_assets(assets)),
                output,
                ROOT,
                asset_dir=assets,
                requested_tag="v1.5.1",
            )

            manifest = publish.load_build_manifest(output)
            pack_manifest = publish.create_tarballs(output, manifest)

            self.assertEqual(len(pack_manifest["packages"]), 6)
            self.assertTrue(all((output / item["tarball"]).is_file() for item in pack_manifest["packages"]))
            self.assertIsNotNone(publish.load_reusable_tarballs(output, manifest))
            with (output / "main" / "README.md").open("a", encoding="utf-8") as readme:
                readme.write("changed\n")
            self.assertIsNone(publish.load_reusable_tarballs(output, manifest))


if __name__ == "__main__":
    unittest.main()
