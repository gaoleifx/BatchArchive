#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use batcharchive::app::App;

fn main() -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1280.0, 820.0])
            .with_min_inner_size([1000.0, 700.0])
            .with_title("浆果文化 · BatchArchive"),
        ..Default::default()
    };
    eframe::run_native(
        "BatchArchive",
        options,
        Box::new(|cc| Ok(Box::new(App::new(cc)))),
    )
}
