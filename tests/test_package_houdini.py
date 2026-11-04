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

    def test_external_alembic_copies_only_the_referenced_file(self):
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
            self.assertFalse((archived_folder / second.name).exists())
            self.assertEqual(manifest["summary"]["resources_copied"], 1)
            self.assertTrue(file_parm.evalAsString().endswith("/character_a.abc"))
            self.assertTrue(root_parm.evalAsString().startswith("$HIP/abc/"))
            self.assertFalse(any(item.get("copied_parent_directory") for item in resources))
            archive_log = (package_dir / "archive_log.txt").read_text(encoding="utf-8-sig")
            self.assertIn("分类：Alembics 2", archive_log)

    def test_project_directory_reference_never_copies_other_scene_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            (source_dir / "lookdev_v003.hip").write_bytes(b"another scene")
            (source_dir / "scene.hip.bak").write_bytes(b"backup scene")
            (source_dir / "geo").mkdir()
            (source_dir / "geo" / "cache.bgeo.sc").write_bytes(b"cache")
            project_parm = _Parm("/obj/ARCHIVE/localscheduler/pdg_workingdir", str(source_dir))
            parent_parm = _Parm("/obj/ARCHIVE/localscheduler/jobdir", str(root))
            packager = load_packager([(project_parm, str(source_dir)), (parent_parm, str(root))])
            package_dir = source_dir / "archive" / "scene"

            packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir))

            self.assertFalse((package_dir / "lookdev_v003.hip").exists())
            self.assertFalse((package_dir / "scene.hip.bak").exists())
            self.assertFalse((package_dir / "geo").exists())
            self.assertEqual(project_parm.evalAsString(), str(source_dir))
            self.assertEqual(parent_parm.evalAsString(), str(root))
            manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["summary"]["resources_copied"], 0)
            self.assertEqual(manifest["summary"]["filtered"]["project_dirs"], 2)
            archive_log = (package_dir / "archive_log.txt").read_text(encoding="utf-8-sig")
            self.assertIn("跳过 2", archive_log)

    def test_alembic_directory_without_file_reference_fails_loudly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            cache_dir = root / "cache_abc"
            cache_dir.mkdir()
            (cache_dir / "a.abc").write_bytes(b"a")
            root_parm = _Parm("/obj/alembic1/python1/root", str(cache_dir))
            packager = load_packager([(root_parm, str(cache_dir))])
            package_dir = source_dir / "archive" / "scene"

            packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir))

            manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual([item["reason"] for item in manifest["failures"]], ["empty_alembic_directory"])
            self.assertFalse((package_dir / "abc").exists())

    def test_auxiliary_files_are_skipped_by_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            checkpoint = source_dir / "scene.checkpoint.json"
            checkpoint.write_bytes(b"{}")
            script = source_dir / "taskgraph.1001.py"
            script.write_bytes(b"print(1)")
            json_parm = _Parm("/obj/top/checkpointfile", str(checkpoint))
            py_parm = _Parm("/obj/top/taskgraphfile", str(script))
            packager = load_packager([(json_parm, str(checkpoint)), (py_parm, str(script))])

            package_dir = source_dir / "archive" / "scene"
            packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir))

            manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertFalse((package_dir / checkpoint.name).exists())
            self.assertFalse((package_dir / script.name).exists())
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["summary"]["filtered"]["auxiliary_files"], 2)
            self.assertEqual(manifest["summary"]["failed"], 0)

            keep_dir = source_dir / "archive" / "scene_keep"
            packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(keep_dir), skip_auxiliary_files=False)
            self.assertTrue((keep_dir / checkpoint.name).exists())
            self.assertTrue((keep_dir / script.name).exists())

    def test_auxiliary_files_inside_a_copied_folder_are_skipped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            cache_dir = root / "external_cache"
            cache_dir.mkdir()
            (cache_dir / "frame.1.bgeo.sc").write_bytes(b"geo")
            (cache_dir / "meta.json").write_bytes(b"{}")
            (cache_dir / "render.py").write_bytes(b"print(1)")
            folder_parm = _Parm("/obj/cache/root", str(cache_dir))
            packager = load_packager([(folder_parm, str(cache_dir))])

            package_dir = source_dir / "archive" / "scene"
            packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir))

            copied = sorted(p.name for p in package_dir.rglob("*") if p.is_file())
            self.assertIn("frame.1.bgeo.sc", copied)
            self.assertNotIn("meta.json", copied)
            self.assertNotIn("render.py", copied)
            manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["summary"]["filtered"]["auxiliary_files"], 2)

    def test_missing_resource_is_not_retried(self):
        manifest = {"status": "failed", "failures": [{"reason": "not_found"}]}
        self.assertFalse(should_retry(0, manifest))

    def test_unresolved_variable_path_is_reported_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            unresolved = "$MY_ASSET_ROOT/shared/character.abc"
            parm = _Parm("/obj/alembic1/fileName", unresolved)
            packager = load_packager([(parm, unresolved)])
            package_dir = source_dir / "archive" / "scene"

            packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir))

            manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual([item["reason"] for item in manifest["failures"]], ["unresolved_path"])
            self.assertEqual(manifest["summary"]["failed"], 1)
            # The unresolved path is a deterministic gap, not a transient copy
            # error, so it must not trigger a retry.
            self.assertFalse(should_retry(0, manifest))

    def test_project_relative_path_is_still_skipped_without_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            relative = "geo/cache.bgeo.sc"
            parm = _Parm("/obj/geo1/file", relative)
            packager = load_packager([(parm, relative)])
            package_dir = source_dir / "archive" / "scene"

            packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir))

            manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["summary"]["failed"], 0)
            self.assertFalse(any(item.get("kind") == "unresolved_path" for item in manifest["resources"]))

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
