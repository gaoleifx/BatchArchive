"""Identify the Houdini build a .hip/.hiplc/.hipnc was last saved with, and pick
the hython that should open it.

Deliberately dependency-free: no `hou`, no PyQt5. That keeps it usable from the
GUI process, from the hython-side packager, and from plain unit tests.

How the version is stored inside a HIP
--------------------------------------
A HIP file is a cpio(odc) archive. Its `.variables` member carries Houdini's
built-in globals, including:

    set -g _HIP_SAVEVERSION = '21.0.596'

That lives within the first few KB, so detection costs one small read no matter
how big the scene is (measured: 0.5 ms on an 11 MB shot). When the variable is
missing, every node's `userdata` block also starts with `___Version___` followed
by the same build string; that is used as a fallback.

Do NOT try to read the version out of paths such as
`$HOME/houdini21.0/poselib` or `.../HOUDIN~1.596/...`: those are leftovers from
the machine that saved the file and go stale as soon as $HOME or the install
directory changes.
"""

import os
import re

HIP_EXTENSIONS = (".hip", ".hiplc", ".hipnc")

_VARIABLES_PATTERN = re.compile(rb"_HIP_SAVEVERSION\s*=\s*'([0-9]+\.[0-9]+\.[0-9]+)'")
_NODE_DATA_PATTERN = re.compile(rb"___Version___\x00\x00\x00\x03\x00\x08([0-9]+\.[0-9]+\.[0-9]+)")

HEAD_BYTES = 1 << 20          # 1 MiB is far more than `.variables` ever needs
FALLBACK_SCAN_BYTES = 4 << 20  # node data can sit further in; 4 MiB is enough

FOUND_SAVE_VARIABLE = "save-variable"
FOUND_NODE_DATA = "node-data"
FOUND_NOTHING = "not-found"

# Outcome of matching a saved version against the installed hython builds.
MATCH_EXACT = "exact"                # same major.minor.build
MATCH_BUILD = "build-mismatch"       # same major.minor, different build
MATCH_NEWER = "newer"                # closest build that is newer
MATCH_OLDER_ONLY = "older-only"      # nothing newer installed, fell back
MATCH_UNKNOWN = "unknown"            # saved version could not be read
MATCH_NONE = "none"                  # no hython installed at all


def parse_version(text):
    """'21.0.596' -> (21, 0, 596). Returns None for anything unexpected."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def flatten_version(version):
    """(21, 0, 596) -> '21.0.596'."""
    return ".".join(str(part) for part in version)


def read_version(path, fallback_scan_bytes=FALLBACK_SCAN_BYTES):
    """Return (version, method) for a HIP file.

    `version` is a string like '21.0.596', or None when nothing was found.
    `method` is one of FOUND_SAVE_VARIABLE / FOUND_NODE_DATA / FOUND_NOTHING.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(HEAD_BYTES)
            match = _VARIABLES_PATTERN.search(head)
            if match:
                return match.group(1).decode("ascii"), FOUND_SAVE_VARIABLE
            if fallback_scan_bytes > len(head):
                head += handle.read(fallback_scan_bytes - len(head))
                match = _VARIABLES_PATTERN.search(head)
                if match:
                    return match.group(1).decode("ascii"), FOUND_SAVE_VARIABLE
            match = _NODE_DATA_PATTERN.search(head)
            if match:
                return match.group(1).decode("ascii"), FOUND_NODE_DATA
    except (OSError, ValueError):
        return None, FOUND_NOTHING
    return None, FOUND_NOTHING


def pick_hython(saved_version, installed):
    """Choose which installed hython should open a scene saved by `saved_version`.

    `installed` is a list of (version, hython_path) pairs, as returned by the
    GUI's Houdini scan. Strategy, in order:

    1. identical build                -> MATCH_EXACT
    2. same major.minor, other build  -> MATCH_BUILD
    3. closest newer build            -> MATCH_NEWER
    4. newest older build (nothing newer available) -> MATCH_OLDER_ONLY

    Returns (chosen_version, chosen_path, status). Every field is None / a
    status string when there is nothing to choose from.
    """
    candidates = []
    for version, path in installed or ():
        parsed = parse_version(version)
        if parsed is not None and path:
            candidates.append((parsed, version, path))
    if not candidates:
        return None, None, MATCH_NONE

    candidates.sort()
    target = parse_version(saved_version)
    if target is None:
        newest = candidates[-1]
        return newest[1], newest[2], MATCH_UNKNOWN

    for parsed, version, path in candidates:
        if parsed == target:
            return version, path, MATCH_EXACT
    for parsed, version, path in candidates:
        if parsed[:2] == target[:2]:
            return version, path, MATCH_BUILD
    newer = [entry for entry in candidates if entry[0] > target]
    if newer:
        parsed, version, path = newer[0]  # closest newer build
        return version, path, MATCH_NEWER
    parsed, version, path = candidates[-1]
    return version, path, MATCH_OLDER_ONLY


def major_minor(version):
    """'21.0.596' -> '21.0'; None in, None out."""
    parsed = parse_version(version)
    return None if parsed is None else "%d.%d" % parsed[:2]


def is_hip_file(path):
    return os.path.splitext(str(path))[1].lower() in HIP_EXTENSIONS
