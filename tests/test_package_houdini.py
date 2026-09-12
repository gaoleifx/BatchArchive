import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

from archive_policy import is_valid_success, should_retry


ROOT = Path(__file__).resolve().parents[1]


class _HipFile:
    @staticmethod
    def load(source, suppress_save_prompt=True, ignore_load_warnings=True):
        return None

    @staticmethod
    def save(destination, save_to_recent_files=False):
        Path(destination).write_bytes(b"packaged hip")


class _RootNode:
    @staticmethod
    def allSubChildren():
        return []


class _Color:
    @staticmethod
    def rgb():
        return (0.2, 0.2, 0.2)


class _Category:
    @staticmethod
    def name():
        return "Sop"


class _NodeType:
    @staticmethod
    def name():
        return "alembic"

    @staticmethod
    def category():
        return _Category()


class _ReferenceNode:
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path

    def type(self):
        return _NodeType()

    def name(self):
        return self._path.rsplit("/", 1)[-1]

    def color(self):
        return _Color()


class _Parm:
    def __init__(self, path, value):
        self._path = path
        self._value = value
        self._node = _ReferenceNode(path.rsplit("/", 1)[0])

    def path(self):
        return self._path

    def name(self):
        return self._path.rsplit("/", 1)[-1]

    def evalAsString(self):
        return self._value

    def node(self):
        return self._node

    def set(self, value):
        self._value = value


def load_packager(refs=None):
    fake_hou = types.SimpleNamespace(
        hipFile=_HipFile(),
        fileReferences=lambda: refs or [],
        node=lambda path: _RootNode(),
        text=types.SimpleNamespace(expandString=lambda value: value),
    )
    sys.modules["hou"] = fake_hou
    spec = importlib.util.spec_from_file_location("package_houdini_under_test", ROOT / "package_houdini.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackageDirectoryTests(unittest.TestCase):
    def test_retry_keeps_files_already_copied_to_fixed_package_directory(self):
        packager = load_packager()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scene.hip"
            source.write_bytes(b"source hip")
            package_dir = root / "archive" / "scene"
            package_dir.mkdir(parents=True)
            copied_resource = package_dir / "textures" / "large.exr"
            copied_resource.parent.mkdir()
            copied_resource.write_bytes(b"completed resource")

            packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir))

            self.assertEqual(copied_resource.read_bytes(), b"completed resource")
            manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertIn("resources", manifest)
            self.assertEqual(manifest["summary"]["resources_copied"], 0)
            archive_log = (package_dir / "archive_log.txt").read_text(encoding="utf-8-sig")
            self.assertIn("结果：成功", archive_log)
            self.assertIn("- scene.hip", archive_log)
            self.assertIn("- 无外部资源", archive_log)

    def test_external_alembic_file_copies_and_reuses_its_parent_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            cache_dir = root / "PPM_shot_anm_v003"
            cache_dir.mkdir()
            first = cache_dir / "character_a.abc"
            second = cache_dir / "character_b.abc"
            first.write_bytes(b"abc a")
            second.write_bytes(b"abc b")
            root_parm = _Parm("/obj/import/python/root", str(cache_dir))
            file_parm = _Parm("/obj/import/alembic/fileName", str(first))
            packager = load_packager([(root_parm, str(cache_dir)), (file_parm, str(first))])
            package_dir = source_dir / "archive" / "scene"

            packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir))

            manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
            resources = [item for item in manifest["resources"] if item.get("category") == "Alembics"]
            self.assertEqual(len(resources), 2)
            storage_directories = {item["storage_directory"] for item in resources}
            self.assertEqual(len(storage_directories), 1)
            archived_folder = package_dir / storage_directories.pop()
            self.assertEqual((archived_folder / first.name).read_bytes(), b"abc a")
            self.assertEqual((archived_folder / second.name).read_bytes(), b"abc b")
            self.assertEqual(manifest["summary"]["resources_copied"], 2)
            self.assertTrue(file_parm.evalAsString().endswith("/character_a.abc"))
            file_resource = next(item for item in resources if item.get("copied_parent_directory"))
            self.assertTrue(file_resource["folder_reused"])
            archive_log = (package_dir / "archive_log.txt").read_text(encoding="utf-8-sig")
            self.assertIn("分类：Alembics 2", archive_log)
            self.assertEqual(archive_log.count("- abc/"), 1)

    def test_missing_resource_is_not_retried(self):
        manifest = {"status": "failed", "failures": [{"reason": "not_found"}]}
        self.assertFalse(should_retry(0, manifest))

    def test_copy_failure_is_retried(self):
        manifest = {"status": "failed", "failures": [{"reason": "copy_failed"}]}
        self.assertTrue(should_retry(0, manifest))

    def test_crash_without_manifest_is_retried(self):
        self.assertTrue(should_retry(1, None))

    def test_success_manifest_requires_the_archived_hip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scene.hip"
            source.write_bytes(b"source")
            package_dir = root / "archive" / "scene"
            package_dir.mkdir(parents=True)
            manifest = {
                "status": "success",
                "failures": [],
                "source": str(source),
                "package": str(package_dir / "scene.hip"),
            }
            self.assertFalse(is_valid_success(0, manifest, source, package_dir))
            (package_dir / "scene.hip").write_bytes(b"package")
            self.assertTrue(is_valid_success(0, manifest, source, package_dir))


if __name__ == "__main__":
    unittest.main()
