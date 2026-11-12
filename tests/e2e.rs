//! End-to-end check without Houdini: a stand-in "hython" emits the same JSON
//! lines as `package_houdini.py` and writes a manifest. Proves the Rust side
//! really spawns a process, consumes the stream and accepts the result.
#![cfg(unix)]

use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::mpsc;
use std::time::Duration;

use batcharchive::runner::{self, Event, ItemState, RunConfig};

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("ba-rs-e2e-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

const MOCK: &str = r#"#!/bin/sh
# argv: -u <script> <hip> <archive_root> --package-dir <dir> [flags...]
shift 2
hip="$1"; shift
shift            # archive root, unused by the mock
pkg=""
while [ $# -gt 0 ]; do
  case "$1" in
    --package-dir) pkg="$2"; shift 2 ;;
    --categories) shift 2 ;;
    *) shift ;;
  esac
done
echo '{"event":"status","message":"加载 HIP（mock）"}'
mkdir -p "$pkg"
cp "$hip" "$pkg/$(basename "$hip")"
printf '{"version":2,"source":"%s","package":"%s","status":"success","summary":{"resources_copied":1},"failures":[]}' \
  "$hip" "$pkg/$(basename "$hip")" > "$pkg/package_manifest.json"
echo '{"event":"done","status":"success","failures":0,"resources_copied":1,"parameters_rewritten":0}'
"#;

fn config(hython: PathBuf, output: PathBuf) -> RunConfig {
    RunConfig {
        hython,
        output_dir: Some(output),
        skip_cache: true,
        skip_render: true,
        skip_auxiliary: true,
        categories: vec!["Geometry".into()],
        filter_red: false,
        filter_external: false,
    }
}

#[test]
fn packages_a_hip_through_the_child_process_contract() {
    let dir = scratch("happy");
    let hip = dir.join("scene.hip");
    std::fs::write(&hip, b"hip").unwrap();
    let mock = dir.join("mock_hython.sh");
    std::fs::write(&mock, MOCK).unwrap();
    std::fs::set_permissions(&mock, std::fs::Permissions::from_mode(0o755)).unwrap();

    let output = dir.join("archive");
    let (tx, rx) = mpsc::channel();
    let _handle = runner::spawn(vec![hip], config(mock, output.clone()), tx);

    let mut done_state = None;
    let mut logs = Vec::new();
    loop {
        match rx.recv_timeout(Duration::from_secs(30)) {
            Ok(Event::Finished) => break,
            Ok(Event::Log(line)) => logs.push(line),
            Ok(Event::Item(index, state)) => {
                if index == 0 {
                    done_state = Some(state);
                }
            }
            Ok(Event::Progress(_)) => {}
            Err(err) => panic!("队列没有结束：{err}"),
        }
    }

    assert_eq!(done_state, Some(ItemState::Done), "日志：{logs:#?}");
    assert!(output.join("scene").join("package_manifest.json").is_file());
    assert!(output.join("scene").join("scene.hip").is_file());
    assert!(logs.iter().any(|l| l.contains("加载 HIP（mock）")));
}

#[test]
fn failing_scene_is_marked_failed_and_not_retried_forever() {
    let dir = scratch("fail");
    let hip = dir.join("broken.hip");
    std::fs::write(&hip, b"hip").unwrap();
    let mock = dir.join("mock_hython.sh");
    std::fs::write(
        &mock,
        r#"#!/bin/sh
echo '{"event":"status","message":"未找到资源"}'
exit 3
"#,
    )
    .unwrap();
    std::fs::set_permissions(&mock, std::fs::Permissions::from_mode(0o755)).unwrap();

    let (tx, rx) = mpsc::channel();
    let _handle = runner::spawn(vec![hip], config(mock, dir.join("archive")), tx);

    let mut state = None;
    let mut retries = 0;
    loop {
        match rx.recv_timeout(Duration::from_secs(30)) {
            Ok(Event::Finished) => break,
            Ok(Event::Item(_, s)) => state = Some(s),
            Ok(Event::Log(line)) => {
                if line.contains("次重试") {
                    retries += 1;
                }
            }
            _ => {}
        }
    }

    assert_eq!(state, Some(ItemState::Failed));
    // No manifest at all means "maybe transient", so it retries up to the cap.
    assert_eq!(retries, 2, "应重试 2 次（共 3 次尝试）");
}
