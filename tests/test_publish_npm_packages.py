from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str, module_name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


publish = load_script("publish-npm-packages.py", "publish_npm_packages")


class BuildManifestTests(unittest.TestCase):
    def test_requires_main_package_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            dist = Path(temporary)
            manifest = {
                "schemaVersion": 1,
                "version": "1.5.1",
                "packages": [
                    {"name": "sealdice", "version": "1.5.1", "path": "main"},
                    {"name": "@sealtrpg/test", "version": "1.5.1", "path": "platform"},
                ],
                "publishOrder": ["sealdice", "@sealtrpg/test"],
            }
            (dist / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(publish.PublishError, "main package must be published last"):
                publish.load_build_manifest(dist)


class RegistryPreflightTests(unittest.TestCase):
    def make_dist(self, root: Path):
        package_dir = root / "main"
        package_dir.mkdir()
        source = {"repository": "sealdice/sealdice-build", "tag": "v1.5.1", "assets": {}}
        (package_dir / "package.json").write_text(
            json.dumps({"name": "sealdice", "version": "1.5.1", "sealdiceRelease": source}),
            encoding="utf-8",
        )
        manifest = {
            "schemaVersion": 1,
            "version": "1.5.1",
            "packages": [
                {
                    "name": "sealdice",
                    "version": "1.5.1",
                    "kind": "main",
                    "path": "main",
                }
            ],
            "publishOrder": ["sealdice"],
        }
        return manifest, source

    def test_matching_existing_package_is_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, source = self.make_dist(root)
            with mock.patch.object(
                publish,
                "npm_view_json",
                side_effect=[(True, "1.5.1"), (True, source)],
            ):
                self.assertEqual(publish.preflight_registry(root, manifest), {"sealdice": "skip"})

    def test_mismatched_existing_package_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = self.make_dist(root)
            with mock.patch.object(
                publish,
                "npm_view_json",
                side_effect=[(True, "1.5.1"), (True, {"tag": "v0.0.0"})],
            ):
                with self.assertRaisesRegex(publish.PublishError, "does not match"):
                    publish.preflight_registry(root, manifest)


if __name__ == "__main__":
    unittest.main()
