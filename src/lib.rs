//! BatchArchive Rust shell.
//!
//! `app` is the egui front end, `runner` owns process/queue execution,
//! `manifest` holds the typed manifest contract and retry policy, `houdini`
//! discovers installed builds. The `hou` facing work stays in
//! `scripts/package_houdini.py` and is embedded into the binary.

pub mod app;
pub mod houdini;
pub mod manifest;
pub mod runner;
