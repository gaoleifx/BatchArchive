//! Queue execution: one HIP at a time, `hython.exe` as a child process, JSON
//! lines on stdout, retry only when the failure looks transient.
//!
//! The `hou`-facing work stays in Python on purpose: `hou.fileReferences()`,
//! parameter evaluation and `hipFile.save()` only exist inside a Houdini
//! process. Rust owns the outer shell (queue, process, IO, validation).

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::Deserialize;

use crate::manifest::{is_valid_success, should_retry, Manifest};

pub const MAX_ATTEMPTS: u32 = 3;

/// Bundled so the shipped binary is genuinely self-contained. An external
/// `scripts/package_houdini.py` next to the exe still wins, which keeps the
/// script hackable during development.
const EMBEDDED_SCRIPT: &str = include_str!("../scripts/package_houdini.py");

#[derive(Debug, Clone)]
pub struct RunConfig {
    pub hython: PathBuf,
    pub output_dir: Option<PathBuf>,
    pub skip_cache: bool,
    pub skip_render: bool,
    pub skip_auxiliary: bool,
    pub categories: Vec<String>,
    pub filter_red: bool,
    pub filter_external: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ItemState {
    Waiting,
    Running,
    Done,
    Failed,
    Stopped,
}

impl ItemState {
    pub fn label(self) -> &'static str {
        match self {
            ItemState::Waiting => "等待",
            ItemState::Running => "处理中",
            ItemState::Done => "完成",
            ItemState::Failed => "失败",
            ItemState::Stopped => "已停止",
        }
    }
}

#[derive(Debug, Clone)]
pub enum Event {
    Log(String),
    Item(usize, ItemState),
    Progress(f32),
    Finished,
}

pub struct QueueHandle {
    stop: Arc<AtomicBool>,
    pid: Arc<Mutex<Option<u32>>>,
}

impl QueueHandle {
    /// Cooperative flag plus a hard kill of the whole process tree: `hython`
    /// can spawn children of its own, and `Child::kill()` would leave them
    /// writing into the archive after the user pressed stop.
    pub fn request_stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
        self.kill_tree();
    }

    fn kill_tree(&self) {
        let pid = *self.pid.lock().unwrap_or_else(|e| e.into_inner());
        let Some(pid) = pid else { return };
        #[cfg(windows)]
        {
            let _ = Command::new("taskkill")
                .args(["/PID", &pid.to_string(), "/T", "/F"])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        #[cfg(not(windows))]
        {
            let _ = Command::new("kill").args(["-9", &pid.to_string()]).status();
        }
    }
}

pub fn spawn(tasks: Vec<PathBuf>, config: RunConfig, tx: std::sync::mpsc::Sender<Event>) -> QueueHandle {
    let stop = Arc::new(AtomicBool::new(false));
    let pid = Arc::new(Mutex::new(None::<u32>));
    let handle = QueueHandle {
        stop: Arc::clone(&stop),
        pid: Arc::clone(&pid),
    };
    thread::spawn(move || {
        let script = match resolve_script() {
            Ok(path) => path,
            Err(err) => {
                let _ = tx.send(Event::Log(format!("无法准备打包脚本：{err}")));
                let _ = tx.send(Event::Finished);
                return;
            }
        };
        run_queue(&tasks, &config, &script, &stop, &pid, &tx);
        let _ = tx.send(Event::Finished);
    });
    handle
}

fn resolve_script() -> std::io::Result<PathBuf> {
    let app_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."));
    let external = app_dir.join("scripts").join("package_houdini.py");
    if external.is_file() {
        return Ok(external);
    }
    let dir = std::env::temp_dir()
        .join("BatchArchive")
        .join(format!("{:08x}", fnv1a(EMBEDDED_SCRIPT)));
    std::fs::create_dir_all(&dir)?;
    let path = dir.join("package_houdini.py");
    if !path.is_file() {
        std::fs::write(&path, EMBEDDED_SCRIPT)?;
    }
    Ok(path)
}

fn fnv1a(text: &str) -> u32 {
    let mut hash: u32 = 0x811c_9dc5;
    for byte in text.as_bytes() {
        hash ^= *byte as u32;
        hash = hash.wrapping_mul(0x0100_0193);
    }
    hash
}

#[derive(Deserialize)]
struct ScriptEvent {
    event: String,
    #[serde(default)]
    message: Option<String>,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    failures: Option<u64>,
    #[serde(default)]
    resources_copied: Option<u64>,
    #[serde(default)]
    parameters_rewritten: Option<u64>,
}

fn unique_package_dir(archive_root: &Path, stem: &str) -> PathBuf {
    let mut candidate = archive_root.join(stem);
    let mut index = 2;
    while candidate.exists() {
        candidate = archive_root.join(format!("{stem}_{index:02}"));
        index += 1;
    }
    candidate
}

fn run_queue(
    tasks: &[PathBuf],
    config: &RunConfig,
    script: &Path,
    stop: &AtomicBool,
    pid: &Mutex<Option<u32>>,
    tx: &std::sync::mpsc::Sender<Event>,
) {
    let total = tasks.len().max(1);
    for (index, hip) in tasks.iter().enumerate() {
        if stop.load(Ordering::SeqCst) {
            let _ = tx.send(Event::Item(index, ItemState::Stopped));
            break;
        }
        let _ = tx.send(Event::Item(index, ItemState::Running));
        let _ = tx.send(Event::Log(format!("[{}/{}] 开始：{}", index + 1, total, hip.display())));

        let archive_root = config
            .output_dir
            .clone()
            .or_else(|| hip.parent().map(|p| p.join("archive")))
            .unwrap_or_else(|| PathBuf::from("archive"));
        if let Err(err) = std::fs::create_dir_all(&archive_root) {
            let _ = tx.send(Event::Log(format!("无法创建输出目录：{err}")));
            let _ = tx.send(Event::Item(index, ItemState::Failed));
            continue;
        }
        let stem = hip.file_stem().and_then(|s| s.to_str()).unwrap_or("scene");
        let package_dir = unique_package_dir(&archive_root, stem);
        let manifest_path = package_dir.join("package_manifest.json");

        let mut exit_code: Option<i32> = None;
        let mut manifest: Option<Manifest> = None;

        for attempt in 1..=MAX_ATTEMPTS {
            if stop.load(Ordering::SeqCst) {
                break;
            }
            if attempt > 1 {
                let _ = tx.send(Event::Log(format!("第 {attempt}/{MAX_ATTEMPTS} 次重试：{}", hip.display())));
            }
            // Never trust a manifest from a previous attempt.
            let _ = std::fs::remove_file(&manifest_path);
            let _ = std::fs::remove_file(package_dir.join("archive_log.txt"));

            match run_once(hip, &archive_root, &package_dir, config, script, stop, pid, tx) {
                Ok(code) => exit_code = Some(code),
                Err(err) => {
                    exit_code = None;
                    let _ = tx.send(Event::Log(format!("启动失败：{err}")));
                }
            }
            manifest = std::fs::read_to_string(&manifest_path)
                .ok()
                .and_then(|text| Manifest::parse(&text));
            if stop.load(Ordering::SeqCst) {
                break;
            }
            if is_valid_success(exit_code, manifest.as_ref(), hip, &package_dir) {
                break;
            }
            let reason = match manifest.as_ref() {
                Some(m) => m.failure_summary(),
                None => format!("进程退出码 {}", exit_code.unwrap_or(-1)),
            };
            if attempt < MAX_ATTEMPTS && should_retry(exit_code, manifest.as_ref()) {
                let _ = tx.send(Event::Log(format!("第 {attempt} 次未通过校验：{reason}，准备重试。")));
                continue;
            }
            let _ = tx.send(Event::Log(format!("停止重试：{reason}")));
            break;
        }

        if stop.load(Ordering::SeqCst) {
            let _ = tx.send(Event::Item(index, ItemState::Stopped));
            break;
        }
        let ok = is_valid_success(exit_code, manifest.as_ref(), hip, &package_dir);
        let _ = tx.send(Event::Item(index, if ok { ItemState::Done } else { ItemState::Failed }));
        let _ = tx.send(Event::Progress(((index + 1) as f32 / total as f32) * 100.0));
    }
}

fn build_command(
    hip: &Path,
    archive_root: &Path,
    package_dir: &Path,
    config: &RunConfig,
    script: &Path,
) -> Command {
    let mut cmd = Command::new(&config.hython);
    cmd.arg("-u")
        .arg(script)
        .arg(hip)
        .arg(archive_root)
        .arg("--package-dir")
        .arg(package_dir)
        .arg("--categories")
        .arg(config.categories.join(","))
        // Houdini's Python defaults to the system code page on Windows;
        // forcing UTF-8 keeps Chinese status lines readable.
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null());
    if let Some(parent) = hip.parent() {
        cmd.current_dir(parent);
    }
    if !config.skip_cache {
        cmd.arg("--include-cache-outputs");
    }
    if !config.skip_render {
        cmd.arg("--include-render-outputs");
    }
    if !config.skip_auxiliary {
        cmd.arg("--include-auxiliary-files");
    }
    if config.filter_red {
        cmd.arg("--filter-red-nodes");
    }
    if config.filter_external {
        cmd.arg("--filter-external");
    }
    cmd
}

#[allow(clippy::too_many_arguments)]
fn run_once(
    hip: &Path,
    archive_root: &Path,
    package_dir: &Path,
    config: &RunConfig,
    script: &Path,
    stop: &AtomicBool,
    pid: &Mutex<Option<u32>>,
    tx: &std::sync::mpsc::Sender<Event>,
) -> std::io::Result<i32> {
    let mut child = build_command(hip, archive_root, package_dir, config, script).spawn()?;
    *pid.lock().unwrap_or_else(|e| e.into_inner()) = Some(child.id());
    let _ = tx.send(Event::Log(format!("Houdini 子进程已启动，PID={}", child.id())));

    if let Some(stderr) = child.stderr.take() {
        let tx_err = tx.clone();
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                if !line.trim().is_empty() {
                    let _ = tx_err.send(Event::Log(format!("stderr: {line}")));
                }
            }
        });
    }

    if let Some(stdout) = child.stdout.take() {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if stop.load(Ordering::SeqCst) {
                let _ = child.kill();
                break;
            }
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str::<ScriptEvent>(&line) {
                Ok(event) if event.event == "status" => {
                    if let Some(message) = event.message {
                        let _ = tx.send(Event::Log(message));
                    }
                }
                Ok(event) if event.event == "done" => {
                    let _ = tx.send(Event::Log(format!(
                        "JSON 校验：{} | 失败 {} | 资源 {} | 改写 {}",
                        event.status.unwrap_or_else(|| "unknown".into()),
                        event.failures.unwrap_or(0),
                        event.resources_copied.unwrap_or(0),
                        event.parameters_rewritten.unwrap_or(0),
                    )));
                }
                _ => {
                    let _ = tx.send(Event::Log(line));
                }
            }
        }
    }

    let status = child.wait()?;
    *pid.lock().unwrap_or_else(|e| e.into_inner()) = None;
    // Give a dying stderr reader a moment to flush before the manifest is read.
    thread::sleep(Duration::from_millis(10));
    Ok(status.code().unwrap_or(-1))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn package_dir_suffixes_on_collision() {
        let root = std::env::temp_dir().join(format!("ba-rs-uniq-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("scene")).unwrap();
        assert_eq!(unique_package_dir(&root, "scene").file_name().unwrap(), "scene_02");
        std::fs::create_dir_all(root.join("scene_02")).unwrap();
        assert_eq!(unique_package_dir(&root, "scene").file_name().unwrap(), "scene_03");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn embedded_script_is_real_python() {
        assert!(EMBEDDED_SCRIPT.contains("def package("));
        assert_ne!(fnv1a(EMBEDDED_SCRIPT), 0);
    }
}
