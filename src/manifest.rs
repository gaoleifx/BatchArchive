//! Typed view of `package_manifest.json` plus the two retry/acceptance
//! decisions that used to live in `archive_policy.py`.
//!
//! Keeping these here (instead of in the UI code) means the same rules are
//! unit-testable without spinning up a GUI or a Houdini process.

use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// Failure reasons that can plausibly recover on a retry without the user
/// changing anything. Everything else is deterministic and must not be
/// retried (it would just waste minutes on a large scene).
const TRANSIENT_REASONS: [&str; 2] = ["copy_failed", "target_missing"];

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Summary {
    #[serde(default)]
    pub references_seen: u64,
    #[serde(default)]
    pub resources_copied: u64,
    #[serde(default)]
    pub parameters_rewritten: u64,
    #[serde(default)]
    pub verified: u64,
    #[serde(default)]
    pub failed: u64,
    #[serde(default)]
    pub not_found: u64,
    #[serde(default)]
    pub category_counts: BTreeMap<String, u64>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Failure {
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub resource: Option<String>,
    #[serde(default)]
    pub parameter: Option<String>,
    #[serde(default)]
    pub node: Option<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Manifest {
    #[serde(default)]
    pub version: u32,
    #[serde(default)]
    pub source: Option<String>,
    #[serde(default)]
    pub package: Option<String>,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub summary: Summary,
    #[serde(default)]
    pub failures: Vec<Failure>,
}

impl Manifest {
    pub fn parse(text: &str) -> Option<Self> {
        serde_json::from_str::<Manifest>(text).ok()
    }

    pub fn is_success(&self) -> bool {
        self.status == "success" && self.failures.is_empty()
    }

    /// True when at least one failure looks like a transient IO problem.
    pub fn has_transient_failure(&self) -> bool {
        self.failures
            .iter()
            .any(|f| TRANSIENT_REASONS.contains(&f.reason.as_str()))
    }

    /// Short human-readable reason for the log pane.
    pub fn failure_summary(&self) -> String {
        match self.failures.len() {
            0 => "无失败项".to_owned(),
            n => format!("{n} 个失败项：{}", self.first_failure_text()),
        }
    }

    fn first_failure_text(&self) -> String {
        self.failures
            .first()
            .map(|f| {
                let subject = f
                    .resource
                    .as_deref()
                    .or(f.parameter.as_deref())
                    .or(f.node.as_deref())
                    .unwrap_or("未知项目");
                format!("{subject}（{}）", if f.reason.is_empty() { "unknown" } else { &f.reason })
            })
            .unwrap_or_else(|| "未知项目".to_owned())
    }
}

/// Decide whether another attempt is worth the disk time.
pub fn should_retry(exit_code: Option<i32>, manifest: Option<&Manifest>) -> bool {
    let Some(manifest) = manifest else {
        return true; // process died, JSON never written: usually recoverable
    };
    if exit_code != Some(0) {
        return true;
    }
    if manifest.is_success() {
        return false;
    }
    manifest.has_transient_failure()
}

/// Accept a package only when the manifest belongs to *this* task and the
/// archived HIP is really on disk. Guards against a stale manifest from an
/// earlier run being mistaken for success.
pub fn is_valid_success(
    exit_code: Option<i32>,
    manifest: Option<&Manifest>,
    expected_source: &Path,
    expected_package_dir: &Path,
) -> bool {
    if exit_code != Some(0) {
        return false;
    }
    let Some(manifest) = manifest else {
        return false;
    };
    if !manifest.is_success() {
        return false;
    }
    let (Some(source), Some(package)) = (manifest.source.as_deref(), manifest.package.as_deref()) else {
        return false;
    };
    let package_path = PathBuf::from(package);
    if !package_path.is_file() {
        return false;
    }
    let same_source = norm_path(Path::new(source)) == norm_path(expected_source);
    let same_parent = package_path
        .parent()
        .map(|p| norm_path(p) == norm_path(expected_package_dir))
        .unwrap_or(false);
    same_source && same_parent
}

/// Case-insensitive on Windows, and tolerant of the `\\?\` prefix that
/// `canonicalize` adds there. Missing files fall back to a plain absolute path
/// so comparisons still work before the target exists.
pub fn norm_path(path: &Path) -> String {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_default()
            .join(path)
    };
    let resolved = std::fs::canonicalize(&absolute).unwrap_or(absolute);
    let mut text = resolved.to_string_lossy().replace('\\', "/");
    if let Some(stripped) = text.strip_prefix("//?/") {
        text = stripped.to_owned();
    }
    if cfg!(windows) {
        text.make_ascii_lowercase();
    }
    while text.len() > 1 && text.ends_with('/') {
        text.pop();
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn manifest_json(source: &Path, package: &Path, failures: &str) -> String {
        format!(
            r#"{{"version":2,"source":{},"package":{},"status":"success","summary":{{"resources_copied":3}},"failures":[{failures}]}}"#,
            serde_json::to_string(&source.to_string_lossy()).unwrap(),
            serde_json::to_string(&package.to_string_lossy()).unwrap()
        )
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("ba-rs-test-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn accepts_matching_manifest() {
        let dir = scratch("accept");
        let src = dir.join("scene.hip");
        fs::write(&src, b"hip").unwrap();
        let pkg_dir = dir.join("archive").join("scene");
        fs::create_dir_all(&pkg_dir).unwrap();
        let pkg = pkg_dir.join("scene.hip");
        fs::write(&pkg, b"hip").unwrap();

        let text = manifest_json(&src, &pkg, "");
        let m = Manifest::parse(&text).unwrap();
        assert!(is_valid_success(Some(0), Some(&m), &src, &pkg_dir));
    }

    #[test]
    fn rejects_manifest_from_another_scene() {
        let dir = scratch("other");
        let src = dir.join("a.hip");
        let other = dir.join("b.hip");
        fs::write(&src, b"hip").unwrap();
        fs::write(&other, b"hip").unwrap();
        let pkg_dir = dir.join("archive").join("a");
        fs::create_dir_all(&pkg_dir).unwrap();
        let pkg = pkg_dir.join("a.hip");
        fs::write(&pkg, b"hip").unwrap();

        let m = Manifest::parse(&manifest_json(&other, &pkg, "")).unwrap();
        assert!(!is_valid_success(Some(0), Some(&m), &src, &pkg_dir));
    }

    #[test]
    fn rejects_missing_archived_hip() {
        let dir = scratch("missing");
        let src = dir.join("scene.hip");
        fs::write(&src, b"hip").unwrap();
        let pkg_dir = dir.join("archive").join("scene");
        fs::create_dir_all(&pkg_dir).unwrap();
        let pkg = pkg_dir.join("scene.hip");

        let m = Manifest::parse(&manifest_json(&src, &pkg, "")).unwrap();
        assert!(!is_valid_success(Some(0), Some(&m), &src, &pkg_dir));
    }

    #[test]
    fn retries_transient_failures_only() {
        let transient = Manifest::parse(r#"{"status":"failed","failures":[{"reason":"copy_failed"}]}"#).unwrap();
        let deterministic = Manifest::parse(r#"{"status":"failed","failures":[{"reason":"not_found"}]}"#).unwrap();
        let success = Manifest::parse(r#"{"status":"success","failures":[]}"#).unwrap();

        assert!(should_retry(Some(0), Some(&transient)));
        assert!(!should_retry(Some(0), Some(&deterministic)));
        assert!(!should_retry(Some(0), Some(&success)));
        assert!(should_retry(Some(1), Some(&deterministic)));
        assert!(should_retry(Some(0), None));
    }
}
