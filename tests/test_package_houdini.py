import importlib.util
import json
import os
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
    def __init__(self, name="alembic"):
        self._name = name

    def name(self):
        return self._name

    def category(self):
        return _Category()


class _ReferenceNode:
    def __init__(self, path, type_name="alembic", parent=None, parms=None):
        self._path = path
        self._type_name = type_name
        self._parent = parent
        # Real HDAs expose their parameters through node.parm(name); the
        # packager uses that to read the frame token a File Cache keeps on
        # sibling parameters.
        self._parms = dict(parms or {})

    def path(self):
        return self._path

    def type(self):
        return _NodeType(self._type_name)

    def name(self):
        return self._path.rsplit("/", 1)[-1]

    def color(self):
        return _Color()

    def parent(self):
        return self._parent

    def parm(self, name):
        return self._parms.get(name)

    def set_parm(self, name, parm):
        self._parms[name] = parm
        return parm


class _Parm:
    def __init__(self, path, value, evaluated=None, type_name="alembic", parent=None, node=None, locked=False):
        self._path = path
        self._value = value
        # Real parms expand $F/backticks on eval, so `raw` may be a sequence
        # while evalAsString() returns one concrete frame.
        self._evaluated = value if evaluated is None else evaluated
        self._node = node or _ReferenceNode(path.rsplit("/", 1)[0], type_name, parent)
        # Parameters inside a locked HDA accept set() without changing what
        # Houdini evaluates; `locked` reproduces that trap.
        self._locked = locked

    def path(self):
        return self._path

    def name(self):
        return self._path.rsplit("/", 1)[-1]

    def evalAsString(self):
        return self._evaluated

    def unexpandedString(self):
        return self._value

    def node(self):
        return self._node

    def set(self, value):
        if self._locked:
            # A locked asset parm swallows the write; evaluation is unchanged.
            return
        self._value = value
        self._evaluated = value

    def eval(self):
        return float(self._evaluated)

    def keyframes(self):
        return () if not self._locked else (object(),)

    def expression(self):
        if self._locked:
            return "chs('cachename')"
        raise RuntimeError("Parameter is not animated")

    def getReferencedParm(self):
        return None


def load_packager(refs=None, houdini_version="21.0.596", hip_dir=None):
    def expand_string(value):
        if not hip_dir:
            return value
        return value.replace("$HIP", str(hip_dir)).replace("$JOB", str(hip_dir))

    fake_hou = types.SimpleNamespace(
        hipFile=_HipFile(),
        fileReferences=lambda: refs or [],
        node=lambda path: _RootNode(),
        text=types.SimpleNamespace(expandString=expand_string),
        applicationVersionString=lambda: houdini_version,
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


class HoudiniVersionRecordTests(unittest.TestCase):
    """The manifest must state which build saved and which build opened the scene."""

    def _run(self, temp, expect_version):
        root = Path(temp)
        source = root / "scene.hip"
        source.write_bytes(b"source hip")
        package_dir = root / "archive" / "scene"
        packager = load_packager()
        packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir), expect_version=expect_version)
        manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
        archive_log = (package_dir / "archive_log.txt").read_text(encoding="utf-8-sig")
        return manifest, archive_log

    def test_matching_version_is_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, archive_log = self._run(temp, "21.0.596")
            self.assertEqual(manifest["houdini"]["saved_with"], "21.0.596")
            self.assertEqual(manifest["houdini"]["opened_with"], "21.0.596")
            self.assertTrue(manifest["houdini"]["version_match"])
            self.assertTrue(manifest["houdini"]["minor_match"])
            self.assertIn("Houdini：保存 21.0.596 / 打开 21.0.596", archive_log)
            self.assertNotIn("版本不一致", archive_log)

    def test_mismatched_version_is_recorded_but_still_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, archive_log = self._run(temp, "22.0.368")
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["houdini"]["saved_with"], "22.0.368")
            self.assertEqual(manifest["houdini"]["opened_with"], "21.0.596")
            self.assertFalse(manifest["houdini"]["version_match"])
            self.assertFalse(manifest["houdini"]["minor_match"])
            self.assertIn("版本不一致", archive_log)

    def test_unknown_saved_version_is_not_reported_as_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest, _ = self._run(temp, "")
            self.assertIsNone(manifest["houdini"]["saved_with"])
            self.assertFalse(manifest["houdini"]["version_match"])
            self.assertIsNone(manifest["houdini"]["minor_match"])


class SequencePackagingTests(unittest.TestCase):
    """A `$F`/backtick reference is a sequence, never a single frame."""

    def _run(self, source, refs, hip_dir=None, **options):
        packager = load_packager(refs, hip_dir=hip_dir)
        package_dir = source.parent / "archive" / "scene"
        packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir), **options)
        manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
        return package_dir, manifest

    def _archived(self, package_dir, item):
        return package_dir.joinpath(*item["storage_directory"].split("/"))

    def _archived_target(self, package_dir, item):
        """Folder a sequence was written to (item["target"] is the full path)."""
        return package_dir.joinpath(*item["target"].split("/"))

    def test_frame_variable_sequence_archives_every_frame(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            cache = root / "s330_c0012_fx_cache" / "geo" / "ashesPartsSim1"
            cache.mkdir(parents=True)
            frames = (100, 101, 149)
            for frame in frames:
                (cache / ("ashesPartsSim1.%d.bgeo.sc" % frame)).write_bytes(b"geo %d" % frame)
            raw = str(cache / "ashesPartsSim1.$F.bgeo.sc")
            evaluated = str(cache / "ashesPartsSim1.149.bgeo.sc")
            parm = _Parm("/obj/ashes_pts/file3/file", raw, evaluated)

            package_dir, manifest = self._run(source, [(parm, raw)])

            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["summary"]["resources_copied"], len(frames))
            item = next(item for item in manifest["resources"] if item.get("parm") == parm.path())
            self.assertEqual(item["sequence_matches"], len(frames))
            self.assertEqual(item["rewritten"], "$HIP/%s/ashesPartsSim1.$F.bgeo.sc" % item["storage_directory"])
            archived = sorted(p.name for p in self._archived_target(package_dir, item).iterdir())
            self.assertEqual(archived, ["ashesPartsSim1.%d.bgeo.sc" % frame for frame in frames])
            # The parameter must stay a sequence after packaging.
            self.assertTrue(parm.evalAsString().endswith("/ashesPartsSim1.$F.bgeo.sc"))

    def test_padded_frame_variable_keeps_its_token(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            cache = root / "cache_geo"
            cache.mkdir()
            for frame in ("0100", "0149"):
                (cache / ("fx_chen_filecache1_v1.%s.bgeo.sc" % frame)).write_bytes(b"geo")
            raw = str(cache / "fx_chen_filecache1_v1.$F4.bgeo.sc")
            evaluated = str(cache / "fx_chen_filecache1_v1.0149.bgeo.sc")
            parm = _Parm("/obj/fx_chen/read_back/file", raw, evaluated)

            package_dir, manifest = self._run(source, [(parm, raw)])

            item = next(item for item in manifest["resources"] if item.get("parm") == parm.path())
            self.assertEqual(item["sequence_matches"], 2)
            self.assertTrue(item["rewritten"].endswith("fx_chen_filecache1_v1.$F4.bgeo.sc"))
            self.assertEqual(len(list(self._archived(package_dir, item).iterdir())), 2)

    def test_backtick_sequence_keeps_the_expression_after_rewrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            cache = root / "cache_geo"
            cache.mkdir()
            for frame in ("0100", "0149"):
                (cache / ("ashes.%s.bgeo.sc" % frame)).write_bytes(b"geo")
            raw = str(cache / "ashes.`padzero(4, $F)`.bgeo.sc")
            evaluated = str(cache / "ashes.0149.bgeo.sc")
            parm = _Parm("/obj/geo1/file1/file", raw, evaluated)

            _package_dir, manifest = self._run(source, [(parm, raw)])

            item = next(item for item in manifest["resources"] if item.get("parm") == parm.path())
            self.assertEqual(item["sequence_matches"], 2)
            self.assertTrue(item["rewritten"].endswith("ashes.`padzero(4, $F)`.bgeo.sc"))

    def test_project_internal_sequence_keeps_hip_relative_expression(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            cache = source_dir / "geo" / "fx_cache"
            cache.mkdir(parents=True)
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            for frame in (100, 149):
                (cache / ("fx.%d.bgeo.sc" % frame)).write_bytes(b"geo")
            raw = "$HIP/geo/fx_cache/fx.$F.bgeo.sc"
            evaluated = str(cache / "fx.149.bgeo.sc")
            parm = _Parm("/obj/geo1/file2/file", raw, evaluated)

            package_dir, manifest = self._run(source, [(parm, raw)], hip_dir=source_dir)

            item = next(item for item in manifest["resources"] if item.get("parm") == parm.path())
            self.assertEqual(item["sequence_matches"], 2)
            self.assertEqual(item["rewritten"], "$HIP/geo/fx_cache/fx.$F.bgeo.sc")
            copied = sorted(p.name for p in (package_dir / "geo" / "fx_cache").iterdir())
            self.assertEqual(copied, ["fx.100.bgeo.sc", "fx.149.bgeo.sc"])

    def test_sequence_without_any_match_falls_back_to_single_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            cache = root / "cache_geo"
            cache.mkdir()
            (cache / "ashes.149.bgeo.sc").write_bytes(b"geo")
            raw = str(cache / "gone.$F.bgeo.sc")
            evaluated = str(cache / "ashes.149.bgeo.sc")
            parm = _Parm("/obj/geo1/file3/file", raw, evaluated)

            _package_dir, manifest = self._run(source, [(parm, raw)])

            item = next(item for item in manifest["resources"] if item.get("parm") == parm.path())
            self.assertNotIn("sequence_matches", item)
            self.assertTrue(item["rewritten"].endswith("ashes.149.bgeo.sc"))

    def test_expression_only_reference_never_globs_the_working_directory(self):
        # File Cache 2.0's read_back node holds `chs("../sopoutput")`. Turned
        # into a wildcard that becomes "*", which would glob the process cwd.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            cache = source_dir / "geo" / "fx_chen_filecache1" / "v1"
            cache.mkdir(parents=True)
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            evaluated = cache / "fx_chen_filecache1_v1.0149.bgeo.sc"
            evaluated.write_bytes(b"geo")
            raw = '`chs("../sopoutput")`'
            parm = _Parm("/obj/fx_chen/filecache1/read_back/file", raw, str(evaluated))

            package_dir, manifest = self._run(source, [(parm, raw)], hip_dir=source_dir)

            item = next(item for item in manifest["resources"] if item.get("parm") == parm.path())
            self.assertNotIn("sequence_matches", item)
            self.assertEqual(item["rewritten"], "$HIP/geo/fx_chen_filecache1/v1/fx_chen_filecache1_v1.0149.bgeo.sc")
            copied = sorted(p.name for p in (package_dir / "geo" / "fx_chen_filecache1" / "v1").iterdir())
            self.assertEqual(copied, ["fx_chen_filecache1_v1.0149.bgeo.sc"])

    def test_file_cache_read_back_follows_the_cache_output_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            cache = source_dir / "geo" / "fx_chen_filecache1" / "v1"
            cache.mkdir(parents=True)
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            cached = cache / "fx_chen_filecache1_v1.0149.bgeo.sc"
            cached.write_bytes(b"geo")
            hda = _ReferenceNode("/obj/fx_chen/filecache1", "filecache::2.0")
            raw = '`chs("../sopoutput")`'
            parm = _Parm("/obj/fx_chen/filecache1/read_back/file", raw, str(cached), "file", hda)

            package_dir, manifest = self._run(source, [(parm, raw)], hip_dir=source_dir)

            self.assertEqual(manifest["summary"]["filtered"]["cache_outputs"], 1)
            self.assertEqual(manifest["summary"]["resources_copied"], 0)
            self.assertFalse((package_dir / "geo").exists())

            # Opting into cache outputs keeps it as a normal dependency.
            keep_dir = source_dir / "archive" / "scene_keep"
            packager = load_packager([(parm, raw)], hip_dir=source_dir)
            packager.package(str(source), str(keep_dir.parent), fixed_package_dir=str(keep_dir), skip_cache_outputs=False)
            kept = json.loads((keep_dir / "package_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(kept["summary"]["resources_copied"], 1)
            self.assertTrue((keep_dir / "geo" / "fx_chen_filecache1" / "v1" / cached.name).exists())

    def _file_cache_output(self, source_dir, frames=("0101", "0149", "0546"), label='`opname("..")`_$OS_v1.$F4.bgeo.sc', locked=False):
        """A File Cache 2.0 whose sopoutput already evaluated to the current frame."""
        cache = source_dir / "geo" / "fx_chen_filecache1" / "v1"
        cache.mkdir(parents=True, exist_ok=True)
        for frame in frames:
            (cache / ("fx_chen_filecache1_v1.%s.bgeo.sc" % frame)).write_bytes(b"geo " + frame.encode())
        hda = _ReferenceNode("/obj/fx_chen/filecache1", "filecache::2.0")
        hda.set_parm(
            "basedir",
            _Parm("/obj/fx_chen/filecache1/basedir", "$HIP/geo", "$HIP/geo", "filecache::2.0", node=hda),
        )
        if label is not None:
            hda.set_parm(
                "descriptivelabel",
                _Parm(
                    "/obj/fx_chen/filecache1/descriptivelabel",
                    label,
                    "fx_chen_filecache1_v1.0149.bgeo.sc",
                    "filecache::2.0",
                    node=hda,
                ),
            )
        current = cache / "fx_chen_filecache1_v1.0149.bgeo.sc"
        output = _Parm("/obj/fx_chen/filecache1/sopoutput", str(current), str(current), "filecache::2.0", node=hda, locked=locked)
        return hda, current, output

    def test_file_cache_output_resolved_to_one_frame_recovers_the_sequence(self):
        # File Cache 2.0 builds sopoutput from basedir + descriptivelabel, and
        # hou.fileReferences() reports the already resolved value: a concrete
        # `...0149.bgeo.sc` with no token left. Archiving only that frame
        # produced a package whose cache is missing 445 of 446 frames.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            _hda, current, output = self._file_cache_output(source_dir)

            package_dir, manifest = self._run(
                source, [(output, str(current))], hip_dir=source_dir, skip_cache_outputs=False
            )

            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["summary"]["resources_copied"], 3)
            item = next(item for item in manifest["resources"] if item.get("parm") == output.path())
            self.assertEqual(item["sequence_matches"], 3)
            self.assertTrue(item["sequence_inferred"])
            self.assertEqual(item["rewritten"], "$HIP/geo/fx_chen_filecache1/v1/fx_chen_filecache1_v1.$F4.bgeo.sc")
            archived = sorted(p.name for p in self._archived_target(package_dir, item).iterdir())
            self.assertEqual(
                archived,
                ["fx_chen_filecache1_v1.%s.bgeo.sc" % frame for frame in ("0101", "0149", "0546")],
            )

    def test_file_cache_read_back_recovers_the_sequence_from_its_hda(self):
        # read_back only holds `chs("../sopoutput")`, so the token has to come
        # from the enclosing HDA; otherwise the package keeps one stale frame.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            hda, current, _output = self._file_cache_output(source_dir)
            raw = '`chs("../sopoutput")`'
            parm = _Parm("/obj/fx_chen/filecache1/read_back/file", raw, str(current), "file", hda)

            package_dir, manifest = self._run(source, [(parm, raw)], hip_dir=source_dir, skip_cache_outputs=False)

            self.assertEqual(manifest["summary"]["resources_copied"], 3)
            item = next(item for item in manifest["resources"] if item.get("parm") == parm.path())
            self.assertEqual(item["sequence_matches"], 3)
            self.assertTrue(item["rewritten"].endswith("fx_chen_filecache1_v1.$F4.bgeo.sc"))
            self.assertEqual(len(list(self._archived_target(package_dir, item).iterdir())), 3)

    def test_per_frame_neighbours_are_not_swept_in_without_a_frame_token(self):
        # Without any token evidence the reference really is one file. Widening
        # it would copy unrelated frames that nothing in the scene points at.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            hda, current, _output = self._file_cache_output(source_dir, label=None)
            raw = '`chs("../sopoutput")`'
            parm = _Parm("/obj/fx_chen/filecache1/read_back/file", raw, str(current), "file", hda)

            package_dir, manifest = self._run(source, [(parm, raw)], hip_dir=source_dir, skip_cache_outputs=False)

            self.assertEqual(manifest["summary"]["resources_copied"], 1)
            item = next(item for item in manifest["resources"] if item.get("parm") == parm.path())
            self.assertNotIn("sequence_matches", item)
            self.assertTrue(self._archived_target(package_dir, item).is_file())
            siblings = sorted(p.name for p in self._archived_target(package_dir, item).parent.iterdir())
            self.assertEqual(siblings, [current.name])

    def test_plain_static_reference_stays_static_next_to_a_sequence(self):
        # A SOP File node pointing at one frame is a dependency on that frame
        # only; the numbered siblings must not be dragged in.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            cache = source_dir / "geo" / "fx_cache"
            cache.mkdir(parents=True)
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            for frame in ("0100", "0149"):
                (cache / ("fx.%s.bgeo.sc" % frame)).write_bytes(b"geo")
            single = cache / "fx.0149.bgeo.sc"
            parm = _Parm("/obj/geo1/file1/file", str(single), str(single), "file")

            package_dir, manifest = self._run(source, [(parm, str(single))], hip_dir=source_dir)

            self.assertEqual(manifest["summary"]["resources_copied"], 1)
            self.assertEqual(sorted(p.name for p in (package_dir / "geo" / "fx_cache").iterdir()), ["fx.0149.bgeo.sc"])

    def test_one_frame_of_another_sequence_inside_a_cache_is_not_widened(self):
        # The HDA's label still spells `$F4`, but this node reads one plate
        # frame that only resembles the cache name: same folder, different
        # body. Borrowing the cache's token here would sweep a whole sequence
        # that nothing in the scene points at.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            hda, _current, _output = self._file_cache_output(source_dir)
            cache = source_dir / "geo" / "fx_chen_filecache1" / "v1"
            for frame in ("0101", "0149", "0546"):
                (cache / ("plate.%s.bgeo.sc" % frame)).write_bytes(b"plate")
            single = cache / "plate.0149.bgeo.sc"
            parm = _Parm("/obj/fx_chen/filecache1/plate/file", str(single), str(single), "file", hda)

            package_dir, manifest = self._run(
                source, [(parm, str(single))], hip_dir=source_dir, skip_cache_outputs=False
            )

            self.assertEqual(manifest["summary"]["resources_copied"], 1)
            item = next(item for item in manifest["resources"] if item.get("parm") == parm.path())
            self.assertNotIn("sequence_matches", item)
            self.assertNotIn("sequence_inferred", item)
            self.assertEqual(
                sorted(path.name for path in package_dir.rglob("plate.*.bgeo.sc")), ["plate.0149.bgeo.sc"]
            )

    def test_single_frame_cache_range_is_not_widened(self):
        # File Cache 2.0's own range is 149-149: the cache was written as a
        # still, so look-alike neighbours in the folder are somebody else's.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            hda, current, output = self._file_cache_output(source_dir)
            for name in ("f1", "f2"):
                hda.set_parm(
                    name, _Parm("/obj/fx_chen/filecache1/" + name, "149", "149", "filecache::2.0", node=hda)
                )

            package_dir, manifest = self._run(
                source, [(output, str(current))], hip_dir=source_dir, skip_cache_outputs=False
            )

            self.assertEqual(manifest["summary"]["resources_copied"], 1)
            item = next(item for item in manifest["resources"] if item.get("parm") == output.path())
            self.assertNotIn("sequence_matches", item)
            archived = sorted(path.name for path in self._archived(package_dir, item).iterdir())
            self.assertEqual(archived, ["fx_chen_filecache1_v1.0149.bgeo.sc"])

    def test_computed_cache_parameter_is_not_claimed_as_rewritten(self):
        # File Cache 2.0 lives in a locked asset: set() returns without raising
        # and the evaluated path never changes. The frames still land where the
        # untouched $HIP expression resolves, so the run succeeds — but the
        # manifest must not pretend the parameter was rewritten.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            _hda, current, output = self._file_cache_output(source_dir, locked=True)

            package_dir, manifest = self._run(
                source, [(output, str(current))], hip_dir=source_dir, skip_cache_outputs=False
            )

            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["summary"]["resources_copied"], 3)
            self.assertEqual(manifest["summary"]["parameters_rewritten"], 0)
            item = next(item for item in manifest["resources"] if item.get("parm") == output.path())
            self.assertEqual(item["sequence_matches"], 3)
            self.assertEqual(item["rewrite_skipped"], "computed_parameter")
            self.assertNotIn("rewritten", item)
            self.assertEqual(len(list(self._archived_target(package_dir, item).iterdir())), 3)
            self.assertEqual(manifest["summary"]["rewrites_skipped"], 1)
            log = (package_dir / "archive_log.txt").read_text(encoding="utf-8-sig")
            self.assertIn("保留原表达式的参数：1 个", log)

    def test_locked_external_reference_that_cannot_be_rewritten_fails(self):
        # Nothing can relocate that path into the package, so saying "success"
        # would hand over an archive that still points at the source share.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            external = root / "shared_geo" / "baked.bgeo.sc"
            external.parent.mkdir()
            external.write_bytes(b"geo")
            parm = _Parm("/obj/geo1/file1/file", str(external), str(external), "file", locked=True)

            package_dir, manifest = self._run(source, [(parm, str(external))])

            self.assertEqual(manifest["status"], "failed")
            self.assertEqual([item["reason"] for item in manifest["failures"]], ["rewrite_failed"])
            self.assertTrue(any(path.name == "baked.bgeo.sc" for path in package_dir.rglob("*")))

    def test_file_cache_internal_parameters_keep_their_own_handling(self):
        # Only the read_back file SOP follows the cache switch; the HDA's
        # scheduler/script parameters must stay auxiliary / project-dir.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            topnet = _ReferenceNode("/obj/fx_chen/filecache1/topnet1", "topnet")
            script = source_dir / "scene.taskgraph.149.py"
            script.write_bytes(b"print(1)")
            script_parm = _Parm("/obj/fx_chen/filecache1/topnet1/taskgraphfile", str(script), str(script), "topnet", topnet)
            dir_parm = _Parm("/obj/fx_chen/filecache1/topnet1/pdg_workingdir", str(source_dir), str(source_dir), "topnet", topnet)

            _package_dir, manifest = self._run(source, [(script_parm, str(script)), (dir_parm, str(source_dir))], hip_dir=source_dir)

            self.assertEqual(manifest["summary"]["filtered"]["auxiliary_files"], 1)
            self.assertEqual(manifest["summary"]["filtered"]["project_dirs"], 1)
            self.assertEqual(manifest["summary"]["filtered"]["cache_outputs"], 0)

    def test_node_name_variables_are_not_mistaken_for_frame_tokens(self):
        packager = load_packager()
        self.assertIsNotNone(packager.SEQUENCE_TOKEN.search("$HIPNAME.file3.$F4.bgeo.sc"))
        for text in ("$HIPNAME.file3.bgeo.sc", "$HIP/geo/$OS/file.bgeo.sc", "$HIP/geo/fx0100.bgeo.sc"):
            self.assertIsNone(packager.SEQUENCE_TOKEN.search(text))

    def test_expression_source_is_never_treated_as_a_resource_path(self):
        packager = load_packager()
        self.assertFalse(packager.looks_like_path("{\n    if(ch('filemethod'))\n    {\n        return chs('file');\n    }\n}"))
        self.assertFalse(packager.looks_like_path("return chs('cachedir') + '/' + chs('cachename');"))
        self.assertTrue(packager.looks_like_path("$HIP/geo/$HIPNAME.$OS.$F4.bgeo.sc"))
        self.assertTrue(packager.looks_like_path(r"Y:\2026\shot\geo\cache (v2)\fx.100.bgeo.sc"))


class SharedReferenceTests(unittest.TestCase):
    """Several nodes can reference one cache; the bytes are written once."""

    def _run(self, source, refs, package_dir=None, **options):
        packager = load_packager(refs)
        package_dir = package_dir or source.parent / "archive" / "scene"
        package_dir.mkdir(parents=True, exist_ok=True)
        packager.package(str(source), str(package_dir.parent), fixed_package_dir=str(package_dir), **options)
        manifest = json.loads((package_dir / "package_manifest.json").read_text(encoding="utf-8"))
        return package_dir, manifest

    def test_two_nodes_on_one_cache_write_it_once(self):
        with tempfile.TemporaryDirectory() as temp:
            source_dir = Path(temp) / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            (source_dir / "geo").mkdir()
            cache = source_dir / "geo" / "cache.bgeo.sc"
            cache.write_bytes(b"geo data")
            first = _Parm("/obj/geo1/file1/file", str(cache), str(cache), "file")
            second = _Parm("/obj/geo2/file2/file", str(cache), str(cache), "file")

            package_dir, manifest = self._run(source, [(first, str(cache)), (second, str(cache))])

            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["summary"]["resources_copied"], 1)
            self.assertEqual(manifest["summary"]["references_reused"], 1)
            self.assertEqual(manifest["summary"]["duplicate_archive"], 0)
            self.assertEqual(manifest["duplicates"], [])
            self.assertEqual((package_dir / "geo" / "cache.bgeo.sc").read_bytes(), b"geo data")
            self.assertEqual(sorted(p.name for p in (package_dir / "geo").iterdir()), ["cache.bgeo.sc"])
            targets = {
                item["target"]
                for item in manifest["resources"]
                if item.get("parm") in {first.path(), second.path()}
            }
            self.assertEqual(targets, {"geo/cache.bgeo.sc"})
            self.assertEqual(first.evalAsString(), "$HIP/geo/cache.bgeo.sc")
            self.assertEqual(second.evalAsString(), "$HIP/geo/cache.bgeo.sc")

    def test_folder_reference_and_file_reference_do_not_hide_a_second_copy(self):
        # A folder parameter with no recognised category lands in external/,
        # while the File node pointing at one of its images lands in
        # textures/. Both copies are needed, but the same bytes were written
        # twice, and the run must report it instead of staying silent.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            texture_dir = root / "tex_shared"
            texture_dir.mkdir()
            plate = texture_dir / "basecolor.exr"
            plate.write_bytes(b"exr data")
            folder_parm = _Parm("/obj/tex/python1/root", str(texture_dir), str(texture_dir), "python")
            file_parm = _Parm("/obj/geo1/file1/file", str(plate), str(plate), "file")

            package_dir, manifest = self._run(
                source,
                [(folder_parm, str(texture_dir)), (file_parm, str(plate))],
            )

            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["summary"]["resources_copied"], 2)
            self.assertEqual(manifest["summary"]["duplicate_archive"], 1)
            duplicate = manifest["duplicates"][0]
            self.assertEqual(Path(duplicate["source"]), plate)
            self.assertEqual(len(duplicate["targets"]), 2)
            self.assertEqual(len(list(package_dir.rglob("basecolor.exr"))), 2)
            self.assertIn("重复归档", (package_dir / "archive_log.txt").read_text(encoding="utf-8-sig"))

    def test_unchanged_copy_in_package_directory_counts_as_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            source_dir = Path(temp) / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            (source_dir / "geo").mkdir()
            cache = source_dir / "geo" / "cache.bgeo.sc"
            cache.write_bytes(b"geo data")
            package_dir = source_dir / "archive" / "scene"
            archived = package_dir / "geo" / "cache.bgeo.sc"
            archived.parent.mkdir(parents=True)
            archived.write_bytes(b"geo data")
            os.utime(archived, (cache.stat().st_atime, cache.stat().st_mtime))
            parm = _Parm("/obj/geo1/file1/file", str(cache), str(cache), "file")

            _package_dir, manifest = self._run(source, [(parm, str(cache))], package_dir=package_dir)

            self.assertEqual(manifest["summary"]["resources_copied"], 0)
            self.assertEqual(manifest["summary"]["references_reused"], 1)

    def test_rewritten_cache_is_recopied_even_when_size_matches(self):
        # A retry reuses the package directory. Judging only by size kept a
        # stale cache in place when the source was rewritten to the same byte
        # count, so mtime has to be part of the check.
        with tempfile.TemporaryDirectory() as temp:
            source_dir = Path(temp) / "project"
            source_dir.mkdir()
            source = source_dir / "scene.hip"
            source.write_bytes(b"source hip")
            (source_dir / "geo").mkdir()
            cache = source_dir / "geo" / "cache.bgeo.sc"
            cache.write_bytes(b"new")
            package_dir = source_dir / "archive" / "scene"
            archived = package_dir / "geo" / "cache.bgeo.sc"
            archived.parent.mkdir(parents=True)
            archived.write_bytes(b"old")
            stale = cache.stat().st_mtime - 3600
            os.utime(archived, (stale, stale))
            parm = _Parm("/obj/geo1/file1/file", str(cache), str(cache), "file")

            _package_dir, manifest = self._run(source, [(parm, str(cache))], package_dir=package_dir)

            self.assertEqual(archived.read_bytes(), b"new")
            self.assertEqual(manifest["summary"]["resources_copied"], 1)
            self.assertEqual(manifest["summary"]["references_reused"], 0)


if __name__ == "__main__":
    unittest.main()
