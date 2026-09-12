"""Pure retry decisions shared by the GUI and its tests."""

import os


TRANSIENT_FAILURE_REASONS = {"copy_failed", "target_missing"}


def should_retry(exit_code, manifest):
    """Retry only failures that can plausibly recover without user changes."""
    if exit_code != 0 or manifest is None:
        return True
    if manifest.get("status") == "success" and not manifest.get("failures"):
        return False
    failures = manifest.get("failures") or []
    return any(item.get("reason") in TRANSIENT_FAILURE_REASONS for item in failures)


def is_valid_success(exit_code, manifest, expected_source, expected_package_dir):
    """Accept success only when the manifest and archived HIP match this task."""
    if exit_code != 0 or not isinstance(manifest, dict):
        return False
    if manifest.get("status") != "success" or manifest.get("failures"):
        return False
    source = manifest.get("source")
    package = manifest.get("package")
    if not source or not package:
        return False
    expected_source = os.path.normcase(os.path.abspath(expected_source))
    actual_source = os.path.normcase(os.path.abspath(source))
    expected_package_dir = os.path.normcase(os.path.abspath(expected_package_dir))
    actual_package = os.path.normcase(os.path.abspath(package))
    return (
        actual_source == expected_source
        and os.path.dirname(actual_package) == expected_package_dir
        and os.path.isfile(actual_package)
    )
