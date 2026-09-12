import os
import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

from app import ArchiveWorker, DropList, MainWindow


class DropListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_display_status_does_not_change_stored_source_path(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "scene.hip"
            source.write_bytes(b"hip")
            widget = DropList()
            widget.add_path(str(source))
            widget.item(0).setText(str(source) + "    [完成]")

            self.assertEqual(widget.paths(), [str(source)])
            self.assertEqual(widget.item(0).data(QtCore.Qt.UserRole), str(source))

    def test_clear_button_action_clears_queue_log_and_completed_progress(self):
        window = MainWindow()
        window.list.addItem("scene.hip")
        window.log.setPlainText("existing run log")
        window.progress.setValue(100)

        window._clear_all()

        self.assertEqual(window.list.count(), 0)
        self.assertEqual(window.log.toPlainText(), "")
        self.assertEqual(window.progress.value(), 0)
        window.close()

    def test_packaging_button_state_swaps_label_and_primary_emphasis(self):
        window = MainWindow()

        window._set_packaging_ui(True)

        self.assertEqual(window.start_btn.text(), "打包中")
        self.assertFalse(window.start_btn.isEnabled())
        self.assertTrue(window.stop_btn.isEnabled())
        self.assertTrue(window.stop_btn.property("packagingActive"))
        self.assertIn('QPushButton#stopButton[packagingActive="true"]', window.styleSheet())
        self.assertIn("QPushButton#primaryButton:disabled", window.styleSheet())

        window._set_packaging_ui(False)

        self.assertEqual(window.start_btn.text(), "开始依次打包")
        self.assertTrue(window.start_btn.isEnabled())
        self.assertFalse(window.stop_btn.isEnabled())
        self.assertFalse(window.stop_btn.property("packagingActive"))
        window.close()


class ArchiveWorkerTests(unittest.TestCase):
    @staticmethod
    def _make_worker(root, source, events):
        return ArchiveWorker(
            [str(source)], sys.executable, str(root / "archive"), True, True,
            {"Geometry"}, False, False, events,
        )

    @staticmethod
    def _event_text(events):
        records = []
        while not events.empty():
            records.append(events.get_nowait())
        return records, "\n".join(str(record) for record in records)

    def test_not_found_manifest_stops_without_repeating_the_whole_job(self):
        helper_source = """\
import json
import pathlib
import sys

package_dir = pathlib.Path(sys.argv[sys.argv.index('--package-dir') + 1])
package_dir.mkdir(parents=True, exist_ok=True)
counter = package_dir / 'attempts.txt'
attempt = int(counter.read_text() or '0') + 1 if counter.exists() else 1
counter.write_text(str(attempt))
(package_dir / 'kept.bin').write_bytes(b'keep')
manifest = {'status': 'failed', 'failures': [{'reason': 'not_found'}]}
(package_dir / 'package_manifest.json').write_text(json.dumps(manifest))
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scene.hip"
            source.write_bytes(b"hip")
            helper = root / "fake_packager.py"
            helper.write_text(helper_source, encoding="utf-8")
            events = queue.Queue()
            worker = self._make_worker(root, source, events)

            with mock.patch("app.PACKAGE_SCRIPT", helper):
                worker.run()

            package_dir = root / "archive" / "scene"
            self.assertEqual((package_dir / "attempts.txt").read_text(), "1")
            self.assertEqual((package_dir / "kept.bin").read_bytes(), b"keep")

    def test_transient_failure_retries_in_place_then_completes(self):
        helper_source = """\
import json
import pathlib
import sys

package_dir = pathlib.Path(sys.argv[sys.argv.index('--package-dir') + 1])
package_dir.mkdir(parents=True, exist_ok=True)
counter = package_dir / 'attempts.txt'
attempt = int(counter.read_text() or '0') + 1 if counter.exists() else 1
counter.write_text(str(attempt))
sentinel = package_dir / 'copied_on_first_attempt.bin'
if attempt == 1:
    sentinel.write_bytes(b'preserved')
    manifest = {'status': 'failed', 'failures': [{'reason': 'copy_failed'}]}
else:
    package = package_dir / pathlib.Path(sys.argv[1]).name
    package.write_bytes(b'packaged hip')
    manifest = {'status': 'success', 'failures': [], 'source': str(pathlib.Path(sys.argv[1]).resolve()), 'package': str(package.resolve())} if sentinel.exists() else {'status': 'failed', 'failures': [{'reason': 'target_missing'}]}
(package_dir / 'package_manifest.json').write_text(json.dumps(manifest))
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scene.hip"
            source.write_bytes(b"hip")
            helper = root / "fake_packager.py"
            helper.write_text(helper_source, encoding="utf-8")
            events = queue.Queue()
            worker = self._make_worker(root, source, events)

            with mock.patch("app.PACKAGE_SCRIPT", helper):
                worker.run()

            package_dir = root / "archive" / "scene"
            records, event_text = self._event_text(events)
            self.assertEqual((package_dir / "attempts.txt").read_text(), "2")
            self.assertEqual((package_dir / "copied_on_first_attempt.bin").read_bytes(), b"preserved")
            self.assertIn("第 2/3 次重试", event_text)
            self.assertIn(("item", 0, "完成"), records)

    def test_missing_manifest_cannot_reuse_previous_attempt_result(self):
        helper_source = """\
import json
import pathlib
import sys

package_dir = pathlib.Path(sys.argv[sys.argv.index('--package-dir') + 1])
package_dir.mkdir(parents=True, exist_ok=True)
counter = package_dir / 'attempts.txt'
attempt = int(counter.read_text() or '0') + 1 if counter.exists() else 1
counter.write_text(str(attempt))
if attempt == 1:
    manifest = {'status': 'failed', 'failures': [{'reason': 'copy_failed'}]}
    (package_dir / 'package_manifest.json').write_text(json.dumps(manifest))
elif attempt == 3:
    package = package_dir / pathlib.Path(sys.argv[1]).name
    package.write_bytes(b'packaged hip')
    manifest = {'status': 'success', 'failures': [], 'source': str(pathlib.Path(sys.argv[1]).resolve()), 'package': str(package.resolve())}
    (package_dir / 'package_manifest.json').write_text(json.dumps(manifest))
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scene.hip"
            source.write_bytes(b"hip")
            helper = root / "fake_packager.py"
            helper.write_text(helper_source, encoding="utf-8")
            events = queue.Queue()
            worker = self._make_worker(root, source, events)

            with mock.patch("app.PACKAGE_SCRIPT", helper):
                worker.run()

            package_dir = root / "archive" / "scene"
            records, _ = self._event_text(events)
            self.assertEqual((package_dir / "attempts.txt").read_text(), "3")
            self.assertIn(("item", 0, "完成"), records)

    def test_three_transient_failures_end_as_failed(self):
        helper_source = """\
import json
import pathlib
import sys

package_dir = pathlib.Path(sys.argv[sys.argv.index('--package-dir') + 1])
package_dir.mkdir(parents=True, exist_ok=True)
counter = package_dir / 'attempts.txt'
attempt = int(counter.read_text() or '0') + 1 if counter.exists() else 1
counter.write_text(str(attempt))
manifest = {'status': 'failed', 'failures': [{'reason': 'copy_failed'}]}
(package_dir / 'package_manifest.json').write_text(json.dumps(manifest))
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scene.hip"
            source.write_bytes(b"hip")
            helper = root / "fake_packager.py"
            helper.write_text(helper_source, encoding="utf-8")
            events = queue.Queue()
            worker = self._make_worker(root, source, events)

            with mock.patch("app.PACKAGE_SCRIPT", helper):
                worker.run()

            package_dir = root / "archive" / "scene"
            records, event_text = self._event_text(events)
            self.assertEqual((package_dir / "attempts.txt").read_text(), "3")
            self.assertIn("已达到最多 3 次尝试", event_text)
            self.assertIn(("item", 0, "失败"), records)

    def test_stop_during_attempt_does_not_announce_another_retry(self):
        helper_source = """\
import pathlib
import sys
import time

package_dir = pathlib.Path(sys.argv[sys.argv.index('--package-dir') + 1])
package_dir.mkdir(parents=True, exist_ok=True)
time.sleep(10)
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scene.hip"
            source.write_bytes(b"hip")
            helper = root / "fake_packager.py"
            helper.write_text(helper_source, encoding="utf-8")
            events = queue.Queue()
            worker = self._make_worker(root, source, events)

            with mock.patch("app.PACKAGE_SCRIPT", helper):
                worker.start()
                for _ in range(200):
                    if worker.process is not None:
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(worker.process)
                worker.stop()
                worker.join(5)

            records, event_text = self._event_text(events)
            self.assertFalse(worker.is_alive())
            self.assertNotIn("准备重试", event_text)
            self.assertIn(("item", 0, "已停止"), records)

    def test_locked_old_manifest_never_launches_or_accepts_stale_success(self):
        helper_source = """\
import pathlib
import sys

package_dir = pathlib.Path(sys.argv[sys.argv.index('--package-dir') + 1])
package_dir.mkdir(parents=True, exist_ok=True)
(package_dir / 'unexpected_launch').write_text('launched')
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "scene.hip"
            source.write_bytes(b"hip")
            helper = root / "fake_packager.py"
            helper.write_text(helper_source, encoding="utf-8")
            events = queue.Queue()
            worker = self._make_worker(root, source, events)

            with mock.patch("app.PACKAGE_SCRIPT", helper), mock.patch("pathlib.Path.unlink", side_effect=PermissionError("locked")):
                worker.run()

            package_dir = root / "archive" / "scene"
            records, event_text = self._event_text(events)
            self.assertFalse((package_dir / "unexpected_launch").exists())
            self.assertIn("无法安全更新校验文件", event_text)
            self.assertIn(("item", 0, "失败"), records)


if __name__ == "__main__":
    unittest.main()
