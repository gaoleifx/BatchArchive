//! Discover installed Houdini builds and pick a sensible default `hython.exe`.
//!
//! The Python version sorted `"Houdini 20.9"` and `"Houdini 20.10"` as plain
//! strings, which orders them backwards. Here the version is compared as a
//! list of numbers instead.

use std::path::{Path, PathBuf};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HoudiniInstall {
    pub label: String,
    pub hython: PathBuf,
}

impl HoudiniInstall {
    pub fn display(&self) -> String {
        format!("Houdini {}", self.label)
    }
}

/// Turn `"20.5.445"` into `[20, 5, 445]`; unparsable segments count as 0 so a
/// weird folder name never panics.
pub fn version_key(label: &str) -> Vec<u32> {
    label
        .split(['.', '-', '_'])
        .map(|part| {
            part.chars()
                .take_while(|c| c.is_ascii_digit())
                .collect::<String>()
                .parse::<u32>()
                .unwrap_or(0)
        })
        .collect()
}

/// Root folders Houdini may live under, in probe order.
fn install_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if cfg!(windows) {
        if let Ok(program_files) = std::env::var("ProgramFiles") {
            roots.push(Path::new(&program_files).join("Side Effects Software"));
        }
        roots.push(PathBuf::from(r"C:\Program Files\Side Effects Software"));
        if let Ok(program_files_x86) = std::env::var("ProgramFiles(x86)") {
            roots.push(Path::new(&program_files_x86).join("Side Effects Software"));
        }
    } else if cfg!(target_os = "macos") {
        roots.push(PathBuf::from("/Applications/Houdini"));
    } else {
        roots.push(PathBuf::from("/opt/hfs"));
    }
    roots.dedup();
    roots
}

fn scan_root(root: &Path, found: &mut Vec<HoudiniInstall>) {
    let Ok(entries) = std::fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let Some(folder) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        let label = folder
            .trim_start_matches("Houdini ")
            .trim_start_matches("houdini")
            .trim_start_matches(['-', '_', ' '])
            .to_owned();
        for candidate in [
            path.join("bin").join("hython.exe"),
            path.join("bin").join("hython"),
        ] {
            if candidate.is_file() {
                found.push(HoudiniInstall {
                    label: label.clone(),
                    hython: candidate,
                });
                break;
            }
        }
    }
}

/// All detected installs, newest first.
pub fn find_installs() -> Vec<HoudiniInstall> {
    let mut found = Vec::new();
    for root in install_roots() {
        scan_root(&root, &mut found);
    }
    found.sort_by(|a, b| version_key(&b.label).cmp(&version_key(&a.label)));
    found.dedup_by(|a, b| a.hython == b.hython);
    found
}

pub fn default_hython() -> Option<PathBuf> {
    find_installs().into_iter().next().map(|i| i.hython)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_order_is_numeric_not_lexicographic() {
        let mut labels = vec!["20.9.100", "20.10.200", "19.5.700", "21.0.300"];
        labels.sort_by(|a, b| version_key(b).cmp(&version_key(a)));
        assert_eq!(labels, vec!["21.0.300", "20.10.200", "20.9.100", "19.5.700"]);
    }

    #[test]
    fn unparsable_label_does_not_panic() {
        assert_eq!(version_key("dev-build"), vec![0, 0]);
    }
}
