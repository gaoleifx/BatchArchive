import tempfile
import unittest
from pathlib import Path

import hip_version
from hip_version import (
    MATCH_BUILD,
    MATCH_EXACT,
    MATCH_NEWER,
    MATCH_NONE,
    MATCH_OLDER_ONLY,
    MATCH_UNKNOWN,
    major_minor,
    parse_version,
    pick_hython,
    read_version,
)


def build_hip(save_version=None, node_version=None, padding=b"", trailer=b""):
    """Minimal stand-in for a HIP: a cpio header plus the bytes we care about."""
    body = b"0707070000010000000006660000000000000000015244160255000000700000000255.start"
    if save_version:
        body += b"\nset -g HIP = 'D:/test'\nset -g _HIP_SAVEVERSION = '" + save_version.encode() + b"'\n"
    body += padding
    if node_version:
        body += b"\x00\x01\x00\r___Version___\x00\x00\x00\x03\x00\x08" + node_version.encode()
    return body + trailer


class ReadVersionTests(unittest.TestCase):
    def _write(self, directory, data, name="scene.hip"):
        path = Path(directory) / name
        path.write_bytes(data)
        return path

    def test_reads_save_version_variable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, build_hip(save_version="21.0.596"))
            version, method = read_version(path)
            self.assertEqual(version, "21.0.596")
            self.assertEqual(method, hip_version.FOUND_SAVE_VARIABLE)

    def test_falls_back_to_node_userdata(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, build_hip(node_version="22.0.368"))
            version, method = read_version(path)
            self.assertEqual(version, "22.0.368")
            self.assertEqual(method, hip_version.FOUND_NODE_DATA)

    def test_prefers_save_variable_over_node_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, build_hip(save_version="20.5.684", node_version="19.5.303"))
            self.assertEqual(read_version(path)[0], "20.5.684")

    def test_reports_missing_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, b"not a hip file at all")
            self.assertEqual(read_version(path), (None, hip_version.FOUND_NOTHING))

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(read_version("Z:/definitely/not/here.hip"), (None, hip_version.FOUND_NOTHING))

    def test_ignores_version_lookalikes_in_paths(self):
        # $HOME and install-dir leftovers must not be mistaken for the build.
        payload = b"set -g POSE = 'C:/Users/x/Documents/houdini21.0/poselib'\nVop/a C:/PROGRA~1/HOUDIN~1.596/otls/a.hda\n"
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, build_hip(padding=payload))
            self.assertEqual(read_version(path), (None, hip_version.FOUND_NOTHING))

    def test_handles_hiplc_and_hipnc_names(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("scene.hiplc", "scene.hipnc"):
                path = self._write(temp, build_hip(save_version="22.0.429"), name=name)
                self.assertEqual(read_version(path)[0], "22.0.429")

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(temp, b"")
            self.assertEqual(read_version(path), (None, hip_version.FOUND_NOTHING))


class ParseVersionTests(unittest.TestCase):
    def test_parses_three_part_version(self):
        self.assertEqual(parse_version("21.0.596"), (21, 0, 596))
        self.assertEqual(parse_version(" 19.5.303 "), (19, 5, 303))

    def test_rejects_anything_else(self):
        for value in (None, "", "21.0", "21.0.596.1", "21.x.596", 21):
            self.assertIsNone(parse_version(value), value)

    def test_major_minor(self):
        self.assertEqual(major_minor("21.0.596"), "21.0")
        self.assertIsNone(major_minor("nope"))


class PickHythonTests(unittest.TestCase):
    INSTALLED = [
        ("22.0.429", r"C:\H\22.0.429\bin\hython.exe"),
        ("21.0.596", r"C:\H\21.0.596\bin\hython.exe"),
        ("20.5.684", r"C:\H\20.5.684\bin\hython.exe"),
        ("19.0.720", r"C:\H\19.0.720\bin\hython.exe"),
    ]

    def test_exact_build_wins(self):
        version, path, status = pick_hython("21.0.596", self.INSTALLED)
        self.assertEqual((version, status), ("21.0.596", MATCH_EXACT))
        self.assertTrue(path.endswith("21.0.596\\bin\\hython.exe"))

    def test_same_minor_other_build(self):
        version, _, status = pick_hython("22.0.368", self.INSTALLED)
        self.assertEqual((version, status), ("22.0.429", MATCH_BUILD))

    def test_closest_newer_when_minor_missing(self):
        version, _, status = pick_hython("21.5.100", self.INSTALLED)
        self.assertEqual((version, status), ("22.0.429", MATCH_NEWER))

    def test_older_only_when_nothing_newer(self):
        version, _, status = pick_hython("23.0.100", self.INSTALLED)
        self.assertEqual((version, status), ("22.0.429", MATCH_OLDER_ONLY))

    def test_unknown_saved_version_uses_newest(self):
        version, _, status = pick_hython(None, self.INSTALLED)
        self.assertEqual((version, status), ("22.0.429", MATCH_UNKNOWN))

    def test_no_install(self):
        self.assertEqual(pick_hython("21.0.596", []), (None, None, MATCH_NONE))

    def test_ignores_junk_installed_entries(self):
        installed = [("not-a-version", "x"), ("21.0.596", r"C:\H\21\bin\hython.exe")]
        self.assertEqual(pick_hython("21.0.596", installed)[1], r"C:\H\21\bin\hython.exe")


if __name__ == "__main__":
    unittest.main()
