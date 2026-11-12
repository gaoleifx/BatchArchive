//! egui shell: queue, filters, log pane and the packaging state machine.
//!
//! The visual language is a direct port of the original PyQt5 build. Every
//! number below (`ROW_H`, `PANEL_W`, paddings, colours) is taken from that
//! build's stylesheet and from pixel measurements of its rendered window, so
//! the two shells stay interchangeable for users.
//!
//! Every decision that can be tested lives in `manifest`, `houdini` or
//! `runner`; this file only paints and forwards intents.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{Receiver, Sender};
use std::time::Duration;

use egui::{
    Align, Align2, Color32, CornerRadius, FontFamily, FontId, Layout, Pos2, Rect, RichText,
    Stroke, Vec2,
};

use crate::houdini::{self, HoudiniInstall};
use crate::manifest::norm_path;
use crate::runner::{self, Event, ItemState, QueueHandle, RunConfig};

// ---------------------------------------------------------------- palette --
const BG: Color32 = Color32::from_rgb(0x0D, 0x11, 0x17);
const SURFACE: Color32 = Color32::from_rgb(0x15, 0x1B, 0x23);
const INPUT_BG: Color32 = Color32::from_rgb(0x0F, 0x14, 0x1A);
const ROW_BG: Color32 = Color32::from_rgb(0x16, 0x1B, 0x22);
const ROW_LINE: Color32 = Color32::from_rgb(0x24, 0x2C, 0x35);
const ROW_HOVER: Color32 = Color32::from_rgb(0x1A, 0x2C, 0x43);
const WIDGET_BG: Color32 = Color32::from_rgb(0x1B, 0x22, 0x2C);
const WIDGET_HOVER: Color32 = Color32::from_rgb(0x24, 0x2D, 0x38);
const WIDGET_DOWN: Color32 = Color32::from_rgb(0x11, 0x18, 0x20);
const BORDER: Color32 = Color32::from_rgb(0x30, 0x39, 0x44);
const BORDER_HOVER: Color32 = Color32::from_rgb(0x59, 0x67, 0x77);
const DASH: Color32 = Color32::from_rgb(0x3B, 0x46, 0x52);
const BRAND: Color32 = Color32::from_rgb(0x16, 0x77, 0xFF);
const BRAND_HOVER: Color32 = Color32::from_rgb(0x40, 0x96, 0xFF);
const BRAND_DOWN: Color32 = Color32::from_rgb(0x09, 0x58, 0xD9);
const TEXT: Color32 = Color32::from_rgb(0xF0, 0xF3, 0xF6);
const BTN_TEXT: Color32 = Color32::from_rgb(0xE6, 0xED, 0xF3);
const LABEL: Color32 = Color32::from_rgb(0xD0, 0xD7, 0xDE);
const FORM_LABEL: Color32 = Color32::from_rgb(0xC9, 0xD1, 0xD9);
const MUTED: Color32 = Color32::from_rgb(0x8B, 0x94, 0x9E);
const DISABLED: Color32 = Color32::from_rgb(0x68, 0x74, 0x82);
const OK: Color32 = Color32::from_rgb(0x52, 0xD2, 0x73);
const FAIL: Color32 = Color32::from_rgb(0xF2, 0x6D, 0x7A);
const SWITCH_ON: Color32 = Color32::from_rgb(0x1E, 0x5C, 0x3C);
const SWITCH_OFF: Color32 = Color32::from_rgb(0x6B, 0x20, 0x28);
const KNOB: Color32 = Color32::from_rgb(0xF7, 0xFA, 0xFC);

// ---------------------------------------------------------------- metrics --
/// Window margins of the original `root` layout: 24 / 22 / 24 / 20.
const MARGIN_X: f32 = 24.0;
const MARGIN_TOP: f32 = 22.0;
const MARGIN_BOTTOM: f32 = 20.0;
/// `root.setSpacing(12)`
const BLOCK_GAP: f32 = 12.0;
/// `left_layout.setSpacing(10)`
const STACK_GAP: f32 = 10.0;
/// Distance between two QGroupBox borders (their `margin-top` eats part of it).
const GROUP_GAP: f32 = 10.0;
/// Space between the panel header and the first `QGroupBox` widget.
const GROUP_LEAD: f32 = 11.0;

const BRAND_ROW_H: f32 = 52.0;
const SUBTITLE_H: f32 = 24.0;
const ROW_H: f32 = 38.0;
const ROW_GAP: f32 = 10.0;
const LABEL_W: f32 = 104.0;
const SECTION_H: f32 = 26.0;
const HINT_H: f32 = 26.0;
const QUEUE_MIN_H: f32 = 220.0;
const LOG_MIN_H: f32 = 112.0;
const PROGRESS_H: f32 = 22.0;
const ITEM_H: f32 = 36.0;
const SHORT_ROW_H: f32 = 32.0;
const ROW_H_SWITCH: f32 = 37.0;

/// `QSplitter.setSizes([820, 340])` resolved against a 1280px window: the
/// panel ends up 466px wide with a 12px gutter.
const PANEL_W: f32 = 466.0;
const PANEL_GUTTER: f32 = 12.0;
const PANEL_PAD_X: i8 = 16;
const PANEL_PAD_Y: i8 = 14;

const CATEGORIES: [&str; 5] = ["Image/Textures", "Geometry", "Alembics", "USDs", "HDAs"];
const HIP_EXTENSIONS: [&str; 3] = ["hip", "hiplc", "hipnc"];

/// Set when a bold CJK face could be loaded, so headings can match the
/// semibold weight the Qt build used.
static HAS_BOLD: AtomicBool = AtomicBool::new(false);

fn bold(size: f32) -> FontId {
    if HAS_BOLD.load(Ordering::Relaxed) {
        FontId::new(size, FontFamily::Name("cjk-bold".into()))
    } else {
        FontId::proportional(size)
    }
}

fn heading(text: &str, size: f32, color: Color32) -> RichText {
    RichText::new(text).font(bold(size)).color(color)
}

struct Task {
    path: PathBuf,
    state: ItemState,
}

pub struct App {
    hython: String,
    installs: Vec<HoudiniInstall>,
    output_dir: String,
    tasks: Vec<Task>,
    log: String,
    progress: f32,
    running: bool,
    queue: Option<QueueHandle>,
    tx: Sender<Event>,
    rx: Receiver<Event>,
    skip_cache: bool,
    skip_render: bool,
    skip_auxiliary: bool,
    categories: [bool; 5],
    filter_red: bool,
    filter_external: bool,
    show_filters: bool,
    notice: Option<String>,
}

impl App {
    pub fn new(cc: &eframe::CreationContext<'_>) -> Self {
        install_cjk_font(&cc.egui_ctx);
        style(&cc.egui_ctx);
        let installs = houdini::find_installs();
        let hython = houdini::default_hython()
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();
        let (tx, rx) = std::sync::mpsc::channel();
        let mut app = Self {
            hython,
            installs,
            output_dir: String::new(),
            tasks: Vec::new(),
            log: String::new(),
            progress: 0.0,
            running: false,
            queue: None,
            tx,
            rx,
            skip_cache: true,
            skip_render: true,
            skip_auxiliary: true,
            categories: [true; 5],
            filter_red: false,
            filter_external: false,
            show_filters: true,
            notice: None,
        };
        // Dropping HIPs onto the exe icon ("send to") is the fastest path for
        // a colleague: no dialog, straight into the queue.
        for argument in std::env::args_os().skip(1) {
            app.add_path(PathBuf::from(argument));
        }
        app
    }

    fn add_path(&mut self, path: PathBuf) {
        let extension_ok = path
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| HIP_EXTENSIONS.contains(&e.to_ascii_lowercase().as_str()))
            .unwrap_or(false);
        if !path.is_file() || !extension_ok {
            return;
        }
        let key = norm_path(&path);
        if self.tasks.iter().any(|t| norm_path(&t.path) == key) {
            return;
        }
        self.tasks.push(Task {
            path,
            state: ItemState::Waiting,
        });
    }

    fn refresh_installs(&mut self) {
        self.installs = houdini::find_installs();
        if self.hython.trim().is_empty()
            && let Some(path) = houdini::default_hython()
        {
            self.hython = path.to_string_lossy().into_owned();
        }
    }

    fn start(&mut self) {
        if self.running {
            return;
        }
        let hython = PathBuf::from(self.hython.trim());
        if !hython.is_file() {
            self.notice = Some("请指定有效的 hython.exe（Houdini 安装目录 bin 下）。".into());
            return;
        }
        if self.tasks.is_empty() {
            self.notice = Some("请先把 .hip / .hiplc / .hipnc 拖进窗口。".into());
            return;
        }
        self.notice = None;
        let categories = CATEGORIES
            .iter()
            .zip(self.categories.iter())
            .filter(|(_, on)| **on)
            .map(|(name, _)| (*name).to_owned())
            .collect::<Vec<_>>();
        let config = RunConfig {
            hython,
            output_dir: {
                let text = self.output_dir.trim();
                (!text.is_empty()).then(|| PathBuf::from(text))
            },
            skip_cache: self.skip_cache,
            skip_render: self.skip_render,
            skip_auxiliary: self.skip_auxiliary,
            categories,
            filter_red: self.filter_red,
            filter_external: self.filter_external,
        };
        let paths = self.tasks.iter().map(|t| t.path.clone()).collect::<Vec<_>>();
        for task in &mut self.tasks {
            task.state = ItemState::Waiting;
        }
        self.log.clear();
        self.progress = 0.0;
        self.running = true;
        self.log(format!("开始打包，共 {} 个 HIP。", paths.len()));
        self.queue = Some(runner::spawn(paths, config, self.tx.clone()));
    }

    fn stop(&mut self) {
        if let Some(queue) = &self.queue {
            queue.request_stop();
            self.log("已请求停止，正在结束 Houdini 进程树。".into());
        }
    }

    fn log(&mut self, line: String) {
        if !self.log.is_empty() {
            self.log.push('\n');
        }
        self.log.push_str(&line);
        // Keep the pane bounded on long queues.
        if self.log.len() > 200_000 {
            let cut = self.log.len() - 150_000;
            let boundary = self
                .log
                .get(cut..)
                .and_then(|tail| tail.find('\n'))
                .map(|offset| cut + offset + 1)
                .unwrap_or(cut);
            self.log.drain(..boundary);
        }
    }

    fn pump_events(&mut self) {
        while let Ok(event) = self.rx.try_recv() {
            match event {
                Event::Log(line) => self.log(line),
                Event::Item(index, state) => {
                    if let Some(task) = self.tasks.get_mut(index) {
                        task.state = state;
                    }
                }
                Event::Progress(value) => self.progress = value,
                Event::Finished => {
                    self.running = false;
                    self.queue = None;
                    let done = self.tasks.iter().filter(|t| t.state == ItemState::Done).count();
                    let failed = self.tasks.iter().filter(|t| t.state == ItemState::Failed).count();
                    self.log(format!("队列处理完成：成功 {done}，失败 {failed}。"));
                }
            }
        }
    }
}

// ------------------------------------------------------------------- view --
impl eframe::App for App {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        self.pump_events();
        // The brand orb animates at the same cadence as the original QTimer.
        ctx.request_repaint_after(Duration::from_millis(55));

        let dropped = ctx.input(|i| i.raw.dropped_files.clone());
        for file in dropped {
            if let Some(path) = file.path {
                self.add_path(path);
            }
        }

        // Brand block, subtitle and the global filter toggle all live above the
        // splitter and span the full window width.
        egui::Panel::top("brand")
            .frame(
                egui::Frame::NONE
                    .fill(BG)
                    .inner_margin(egui::Margin {
                        left: MARGIN_X as i8,
                        right: MARGIN_X as i8,
                        top: MARGIN_TOP as i8,
                        bottom: BLOCK_GAP as i8,
                    }),
            )
            .show(ui, |ui| self.top_bar(ui));

        if self.show_filters {
            egui::Panel::right("filters")
                .resizable(true)
                .default_size(PANEL_W + MARGIN_X)
                .min_size(300.0 + MARGIN_X)
                .frame(
                    egui::Frame::NONE.fill(BG).inner_margin(egui::Margin {
                        left: 0,
                        right: MARGIN_X as i8,
                        top: 2,
                        bottom: MARGIN_BOTTOM as i8,
                    }),
                )
                .show(ui, |ui| {
                    egui::Frame::NONE
                        .fill(SURFACE)
                        .stroke(Stroke::new(1.0, BORDER))
                        .corner_radius(CornerRadius::same(7))
                        .inner_margin(egui::Margin {
                            left: PANEL_PAD_X,
                            right: PANEL_PAD_X,
                            top: PANEL_PAD_Y,
                            bottom: PANEL_PAD_Y,
                        })
                        .show(ui, |ui| {
                            ui.set_min_size(ui.available_size());
                            self.filter_panel(ui);
                        });
                });
        }

        egui::CentralPanel::no_frame()
            .frame(
                egui::Frame::NONE
                    .fill(BG)
                    .inner_margin(egui::Margin {
                        left: MARGIN_X as i8,
                        right: PANEL_GUTTER as i8,
                        top: 2,
                        bottom: MARGIN_BOTTOM as i8,
                    }),
            )
            .show(ui, |ui| self.left_column(ui));
    }
}

impl App {
    fn top_bar(&mut self, ui: &mut egui::Ui) {
        // 26pt bold brand, 52px orb, 22pt divider, 17pt product, status right.
        ui.allocate_ui_with_layout(
            Vec2::new(ui.available_width(), BRAND_ROW_H),
            Layout::left_to_right(Align::Center),
            |ui| {
                ui.spacing_mut().item_spacing.x = 14.0;
                ui.label(heading("浆果文化", 34.5, Color32::from_rgb(0xF5, 0xF7, 0xFA)));
                brand_orb(ui);
                ui.label(
                    RichText::new("|")
                        .font(FontId::proportional(29.0))
                        .color(Color32::from_rgb(0x3D, 0x46, 0x52)),
                );
                ui.label(
                    RichText::new("BatchArchive")
                        .font(FontId::proportional(22.5))
                        .color(Color32::from_rgb(0xD8, 0xDE, 0xE6)),
                );
                ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
                    let (text, color) = if self.running {
                        ("● 打包中", BRAND)
                    } else {
                        ("● 就绪", OK)
                    };
                    ui.label(heading(text, 13.0, color));
                });
            },
        );
        ui.add_space(BLOCK_GAP);

        ui.allocate_ui_with_layout(
            Vec2::new(ui.available_width(), SUBTITLE_H),
            Layout::left_to_right(Align::Center),
            |ui| {
                ui.label(
                    RichText::new("把 HIP 文件拖进窗口，按列表顺序归档到各自工程目录的 archive 文件夹")
                        .size(13.0)
                        .color(MUTED),
                );
            },
        );
        ui.add_space(BLOCK_GAP);

        ui.allocate_ui_with_layout(
            Vec2::new(ui.available_width(), ROW_H),
            Layout::right_to_left(Align::Center),
            |ui| {
                let text = if self.show_filters {
                    "隐藏过滤规则"
                } else {
                    "显示过滤规则"
                };
                let icon = if self.show_filters {
                    Icon::ArrowDown
                } else {
                    Icon::ArrowUp
                };
                let spec = Btn::new(text).icon(icon).min_w(131.0);
                if button(ui, &spec, true).clicked() {
                    self.show_filters = !self.show_filters;
                }
            },
        );
    }

    fn left_column(&mut self, ui: &mut egui::Ui) {
        let total = ui.available_height();
        self.paths_form(ui);
        ui.add_space(STACK_GAP);

        ui.allocate_ui_with_layout(
            Vec2::new(ui.available_width(), SECTION_H),
            Layout::left_to_right(Align::Center),
            |ui| {
                ui.label(heading("HIP 队列（拖放区域）", 16.0, BTN_TEXT));
            },
        );
        ui.add_space(STACK_GAP);

        let fixed = ROW_H * 2.0 + ROW_GAP + SECTION_H + HINT_H + ROW_H + PROGRESS_H + SECTION_H
            + STACK_GAP * 5.0
            + 13.0
            + 16.0
            + 8.0;
        let extra = (total - fixed - QUEUE_MIN_H - LOG_MIN_H).max(0.0);
        let queue_h = QUEUE_MIN_H + extra * 0.5;
        self.queue_box(ui, queue_h);
        ui.add_space(STACK_GAP);

        ui.allocate_ui_with_layout(
            Vec2::new(ui.available_width(), HINT_H),
            Layout::left_to_right(Align::Center),
            |ui| {
                ui.label(
                    RichText::new("支持 .hip / .hiplc / .hipnc；列表顺序就是执行顺序，可多选后删除")
                        .size(13.0)
                        .color(MUTED),
                );
            },
        );
        ui.add_space(7.0);

        self.actions(ui);
        ui.add_space(12.0);
        progress_bar(ui, self.progress / 100.0);
        ui.add_space(12.0);

        ui.allocate_ui_with_layout(
            Vec2::new(ui.available_width(), 22.0),
            Layout::left_to_right(Align::Center),
            |ui| {
                ui.label(RichText::new("运行日志").size(13.0).color(TEXT));
            },
        );
        ui.add_space(6.0);
        let log_h = ui.available_height().max(0.0);
        self.log_box(ui, log_h);
    }

    fn filter_panel(&mut self, ui: &mut egui::Ui) {
        ui.allocate_ui_with_layout(
            Vec2::new(ui.available_width(), ROW_H),
            Layout::right_to_left(Align::Center),
            |ui| {
                if button(ui, &Btn::new("折叠").icon(Icon::ArrowUp).min_w(74.0), true).clicked() {
                    self.show_filters = false;
                }
                ui.with_layout(Layout::left_to_right(Align::Center), |ui| {
                    ui.label(heading("打包过滤规则", 16.0, BTN_TEXT));
                });
            },
        );
        ui.add_space(GROUP_LEAD);

        egui::ScrollArea::vertical()
            .id_salt("filter-groups")
            .auto_shrink([false, false])
            .show(ui, |ui| {
                group_box(ui, "规则一 · 资源大类", |ui| {
                    for index in 0..CATEGORIES.len() {
                        switch(
                            ui,
                            &mut self.categories[index],
                            CATEGORIES[index],
                            SHORT_ROW_H,
                        );
                    }
                });

                ui.add_space(GROUP_GAP);
                group_box(ui, "规则二 · 节点细分（AND）", |ui| {
                    switch(ui, &mut self.filter_red, "只打包节点颜色是红色的节点", ROW_H_SWITCH);
                    switch(
                        ui,
                        &mut self.filter_external,
                        "只打包引用外部资源的节点",
                        ROW_H_SWITCH,
                    );
                    ui.add_space(9.0);
                    ui.label(
                        RichText::new("不勾选时不增加节点限制；同时勾选时必须同时满足。")
                            .size(13.0)
                            .color(MUTED),
                    );
                });

                ui.add_space(GROUP_GAP);
                group_box(ui, "输出过滤", |ui| {
                    switch(
                        ui,
                        &mut self.skip_cache,
                        "跳过工程内部缓存输出（bgeo / sim / vdb 等）",
                        ROW_H_SWITCH,
                    );
                    switch(ui, &mut self.skip_render, "跳过工程内部渲染输出（exr / AOV 等）", ROW_H_SWITCH);
                    switch(
                        ui,
                        &mut self.skip_auxiliary,
                        "跳过脚本/配置文件（json / py / txt 等）",
                        ROW_H_SWITCH,
                    );
                });

                ui.add_space(GROUP_GAP);
                ui.label(
                    RichText::new("未分类资源保留原有打包行为。内部路径使用标准目录，外部资源进入 external。")
                        .size(13.0)
                        .color(MUTED),
                );
            });
    }

    fn paths_form(&mut self, ui: &mut egui::Ui) {
        let width = ui.available_width();
        let (combo_w, small_btn) = (152.0, 74.0);
        let refresh_w = 104.0;
        let gap = 8.0;

        // Row 1: label + combo + free-text hython + 刷新版本 + 浏览
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = gap;
            form_label(ui, "Houdini hython");
            let edit_w =
                (width - LABEL_W - gap * 4.0 - combo_w - refresh_w - small_btn).max(120.0);
            self.hython_combo(ui, combo_w);
            input(ui, &mut self.hython, "也可以手动指定 hython.exe", edit_w);
            if button(ui, &Btn::new("刷新版本").icon(Icon::Reload).min_w(refresh_w), true).clicked() {
                self.refresh_installs();
            }
            if button(ui, &Btn::new("浏览").icon(Icon::Folder).min_w(small_btn), true).clicked()
                && let Some(file) = rfd_pick_file()
            {
                self.hython = file;
            }
        });
        ui.add_space(ROW_GAP);

        // Row 2: label + output dir + 选择
        ui.horizontal(|ui| {
            ui.spacing_mut().item_spacing.x = gap;
            form_label(ui, "输出目录");
            let edit_w = (width - LABEL_W - gap * 2.0 - small_btn).max(160.0);
            input(
                ui,
                &mut self.output_dir,
                "留空：每个 HIP 使用所在目录\\archive；填写后所有任务共用此输出目录",
                edit_w,
            );
            if button(ui, &Btn::new("选择").icon(Icon::FolderOpen).min_w(small_btn), true).clicked()
                && let Some(dir) = rfd_pick_folder()
            {
                self.output_dir = dir;
            }
        });
    }

    fn hython_combo(&mut self, ui: &mut egui::Ui, width: f32) {
        let current = self
            .installs
            .iter()
            .find(|i| norm_path(&i.hython) == norm_path(Path::new(self.hython.trim())))
            .map(|i| i.display())
            .unwrap_or_else(|| {
                if self.hython.trim().is_empty() {
                    "未检测到 Houdini".to_owned()
                } else {
                    "自定义路径".to_owned()
                }
            });

        let (rect, response) =
            ui.allocate_exact_size(Vec2::new(width, ROW_H), egui::Sense::click());
        let hovered = response.hovered();
        let painter = ui.painter();
        field_frame(painter, rect, hovered);
        painter.text(
            Pos2::new(rect.left() + 8.0, rect.center().y),
            Align2::LEFT_CENTER,
            &current,
            FontId::proportional(13.0),
            BTN_TEXT,
        );

        egui::Popup::menu(&response).show(|ui| {
            ui.set_min_width(width);
            let installs = self.installs.clone();
            if installs.is_empty() {
                ui.label(RichText::new("未检测到 Houdini 安装").size(13.0).color(MUTED));
            }
            for install in installs {
                let selected =
                    norm_path(&install.hython) == norm_path(Path::new(self.hython.trim()));
                if ui.selectable_label(selected, install.display()).clicked() {
                    self.hython = install.hython.to_string_lossy().into_owned();
                }
            }
        });
    }

    fn queue_box(&mut self, ui: &mut egui::Ui, height: f32) {
        let frame = egui::Frame::NONE
            .fill(INPUT_BG)
            .inner_margin(egui::Margin::same(7));
        let response = frame.show(ui, |ui| {
            ui.set_min_width(ui.available_width());
            egui::ScrollArea::vertical()
                .id_salt("queue-list")
                .auto_shrink([false, false])
                .max_height(height - 16.0)
                .min_scrolled_height(height - 16.0)
                .show(ui, |ui| {
                    if self.tasks.is_empty() {
                        empty_state(ui, height - 16.0);
                        return;
                    }
                    let mut remove = None;
                    let count = self.tasks.len();
                    for index in 0..count {
                        let task = &self.tasks[index];
                        let (state_text, state_color) = match task.state {
                            ItemState::Done => ("完成", OK),
                            ItemState::Failed => ("失败", FAIL),
                            ItemState::Running => ("处理中", BRAND),
                            ItemState::Waiting => ("", MUTED),
                            ItemState::Stopped => ("已停止", Color32::from_gray(150)),
                        };
                        let (rect, row_response) = ui.allocate_exact_size(
                            Vec2::new(ui.available_width(), ITEM_H),
                            egui::Sense::hover(),
                        );
                        let close = Rect::from_center_size(
                            Pos2::new(rect.right() - 18.0, rect.center().y),
                            Vec2::new(22.0, 22.0),
                        );
                        let close_response = ui.interact(
                            close,
                            ui.id().with(("remove", index)),
                            egui::Sense::click(),
                        );
                        let painter = ui.painter();
                        painter.rect_filled(rect, CornerRadius::same(0), ROW_BG);
                        if row_response.hovered() || close_response.hovered() {
                            painter.rect_filled(rect, CornerRadius::same(0), ROW_HOVER);
                        }
                        painter.text(
                            Pos2::new(rect.left() + 8.0, rect.center().y),
                            Align2::LEFT_CENTER,
                            task.path.to_string_lossy(),
                            FontId::proportional(13.0),
                            BTN_TEXT,
                        );
                        painter.text(
                            Pos2::new(rect.right() - 34.0, rect.center().y),
                            Align2::RIGHT_CENTER,
                            state_text,
                            FontId::proportional(12.0),
                            state_color,
                        );
                        if row_response.hovered() || close_response.hovered() {
                            painter.text(
                                Pos2::new(rect.right() - 18.0, rect.center().y),
                                Align2::CENTER_CENTER,
                                "x",
                                FontId::proportional(12.0),
                                if close_response.hovered() { TEXT } else { MUTED },
                            );
                        }
                        painter.line_segment(
                            [
                                Pos2::new(rect.left(), rect.bottom()),
                                Pos2::new(rect.right(), rect.bottom()),
                            ],
                            Stroke::new(1.0, ROW_LINE),
                        );
                        if close_response.clicked() && !self.running {
                            remove = Some(index);
                        }
                    }
                    if let Some(index) = remove {
                        self.tasks.remove(index);
                    }
                });
        });
        dashed_box(ui.painter(), response.response.rect, DASH);
    }

    fn actions(&mut self, ui: &mut egui::Ui) {
        // The primary button is 4px taller in the Qt build, so the row is
        // allocated at that height and the rest are vertically centred.
        ui.allocate_ui_with_layout(
            Vec2::new(ui.available_width(), ROW_H + 4.0),
            Layout::left_to_right(Align::Center),
            |ui| self.action_buttons(ui),
        );
    }

    fn action_buttons(&mut self, ui: &mut egui::Ui) {
        ui.spacing_mut().item_spacing.x = 12.0;
        if button(ui, &Btn::new("添加文件").icon(Icon::Doc).min_w(102.0), true).clicked()
            && let Some(files) = rfd_pick_files()
        {
            for file in files {
                self.add_path(file);
            }
        }
        let can_clear = !self.running && !self.tasks.is_empty();
        if button(ui, &Btn::new("删除选中").icon(Icon::Trash).min_w(102.0), can_clear).clicked() {
            self.tasks.clear();
            self.progress = 0.0;
        }
        if button(ui, &Btn::new("清空").icon(Icon::Reset).min_w(74.0), can_clear).clicked() {
            self.tasks.clear();
            self.progress = 0.0;
        }

        ui.with_layout(Layout::right_to_left(Align::Center), |ui| {
            ui.spacing_mut().item_spacing.x = 12.0;
            if button(ui, &Btn::new("停止").icon(Icon::Stop).min_w(74.0), self.running).clicked() {
                self.stop();
            }
            let text = if self.running { "打包中" } else { "开始打包" };
            let start = Btn::new(text).icon(Icon::Play).min_w(110.0).primary();
            if button(ui, &start, !self.running).clicked() {
                self.start();
            }
        });
    }

    fn log_box(&mut self, ui: &mut egui::Ui, height: f32) {
        egui::Frame::NONE
            .fill(INPUT_BG)
            .stroke(Stroke::new(1.0, BORDER))
            .corner_radius(CornerRadius::same(6))
            .inner_margin(egui::Margin::same(8))
            .show(ui, |ui| {
                ui.set_min_height((height - 18.0).max(40.0));
                let text = std::mem::take(&mut self.log);
                egui::ScrollArea::vertical()
                    .id_salt("log-pane")
                    .stick_to_bottom(true)
                    .auto_shrink([false, false])
                    .show(ui, |ui| {
                        ui.add(
                            egui::Label::new(
                                RichText::new(text.as_str()).size(13.0).color(BTN_TEXT),
                            )
                            .wrap(),
                        );
                    });
                self.log = text;
            });
    }
}

// ---------------------------------------------------------------- widgets --
#[derive(Clone, Copy, PartialEq)]
enum Icon {
    Doc,
    Trash,
    Reset,
    Play,
    Stop,
    Reload,
    Folder,
    FolderOpen,
    ArrowDown,
    ArrowUp,
}

struct Btn {
    text: String,
    icon: Option<Icon>,
    min_w: f32,
    primary: bool,
}

impl Btn {
    fn new(text: &str) -> Self {
        Self {
            text: text.to_owned(),
            icon: None,
            min_w: 0.0,
            primary: false,
        }
    }

    fn icon(mut self, icon: Icon) -> Self {
        self.icon = Some(icon);
        self
    }

    fn min_w(mut self, width: f32) -> Self {
        self.min_w = width;
        self
    }

    fn primary(mut self) -> Self {
        self.primary = true;
        self
    }
}

/// Painted `QPushButton`: 38px tall, 6px radius, 1px border, centred
/// icon+label pair. Reimplemented instead of themed `egui::Button` so the
/// metrics match the Qt build exactly.
fn button(ui: &mut egui::Ui, spec: &Btn, enabled: bool) -> egui::Response {
    let font = if spec.primary {
        bold(13.0)
    } else {
        FontId::proportional(13.0)
    };
    let color = if spec.primary { Color32::WHITE } else { BTN_TEXT };
    let galley = ui.painter().layout_no_wrap(spec.text.clone(), font, color);
    let icon_w = if spec.icon.is_some() { 20.0 } else { 0.0 };
    let pad = if spec.primary { 16.0 } else { 13.0 };
    let width = spec.min_w.max(pad * 2.0 + icon_w + galley.size().x);

    let sense = if enabled {
        egui::Sense::click()
    } else {
        egui::Sense::hover()
    };
    let height = if spec.primary { ROW_H + 4.0 } else { ROW_H };
    let (rect, response) = ui.allocate_exact_size(Vec2::new(width, height), sense);

    let (fill, stroke, text_color) = if !enabled {
        if spec.primary {
            (
                Color32::from_rgb(0x20, 0x28, 0x33),
                Color32::from_rgb(0x30, 0x3A, 0x46),
                Color32::from_rgb(0x7B, 0x87, 0x94),
            )
        } else {
            (SURFACE, Color32::from_rgb(0x2A, 0x32, 0x3C), DISABLED)
        }
    } else if spec.primary {
        if response.is_pointer_button_down_on() {
            (BRAND_DOWN, BRAND_DOWN, Color32::WHITE)
        } else if response.hovered() {
            (BRAND_HOVER, BRAND_HOVER, Color32::WHITE)
        } else {
            (BRAND, BRAND, Color32::WHITE)
        }
    } else if response.is_pointer_button_down_on() {
        (WIDGET_DOWN, BORDER, BTN_TEXT)
    } else if response.hovered() {
        (WIDGET_HOVER, BORDER_HOVER, BTN_TEXT)
    } else {
        (WIDGET_BG, BORDER, BTN_TEXT)
    };

    if enabled && response.hovered() {
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
    }

    let painter = ui.painter();
    painter.rect_filled(rect, CornerRadius::same(6), fill);
    painter.rect_stroke(
        rect,
        CornerRadius::same(6),
        Stroke::new(1.0, stroke),
        egui::StrokeKind::Inside,
    );

    let content_w = icon_w + galley.size().x;
    let mut x = rect.center().x - content_w * 0.5;
    if let Some(icon) = spec.icon {
        paint_icon(painter, Pos2::new(x + 8.0, rect.center().y), icon, text_color);
        x += icon_w;
    }
    painter.galley(
        Pos2::new(x, rect.center().y - galley.size().y * 0.5),
        galley,
        text_color,
    );
    response
}

fn paint_icon(painter: &egui::Painter, center: Pos2, icon: Icon, color: Color32) {
    let stroke = Stroke::new(1.6, color);
    match icon {
        Icon::Doc => {
            // White page with the folded top-right corner of `SP_FileIcon`.
            let (left, top) = (center.x - 5.0, center.y - 7.0);
            let (w, h, fold) = (10.0, 14.0, 4.0);
            painter.add(egui::Shape::convex_polygon(
                vec![
                    Pos2::new(left, top),
                    Pos2::new(left + w - fold, top),
                    Pos2::new(left + w, top + fold),
                    Pos2::new(left + w, top + h),
                    Pos2::new(left, top + h),
                ],
                color,
                Stroke::NONE,
            ));
            painter.add(egui::Shape::convex_polygon(
                vec![
                    Pos2::new(left + w - fold, top),
                    Pos2::new(left + w, top + fold),
                    Pos2::new(left + w - fold, top + fold),
                ],
                Color32::from_rgb(0x8B, 0x96, 0xA2),
                Stroke::NONE,
            ));
        }
        Icon::Trash => {
            painter.line_segment(
                [
                    Pos2::new(center.x - 6.5, center.y - 4.5),
                    Pos2::new(center.x + 6.5, center.y - 4.5),
                ],
                stroke,
            );
            painter.line_segment(
                [
                    Pos2::new(center.x - 2.5, center.y - 7.0),
                    Pos2::new(center.x + 2.5, center.y - 7.0),
                ],
                stroke,
            );
            let body = Rect::from_min_max(
                Pos2::new(center.x - 4.8, center.y - 3.0),
                Pos2::new(center.x + 4.8, center.y + 7.0),
            );
            painter.rect_stroke(
                body,
                CornerRadius::same(1),
                stroke,
                egui::StrokeKind::Inside,
            );
            for dx in [-1.6, 1.6] {
                painter.line_segment(
                    [
                        Pos2::new(center.x + dx, center.y - 1.0),
                        Pos2::new(center.x + dx, center.y + 5.0),
                    ],
                    Stroke::new(1.2, color),
                );
            }
        }
        Icon::Reset => {
            // Paint bucket, matching Qt's `SP_DialogResetButton` artwork.
            let yellow = Color32::from_rgb(0xE8, 0xC9, 0x2E);
            let rim = Color32::from_rgb(0xC9, 0xCE, 0xD4);
            let handle = Color32::from_rgb(0xB5, 0x74, 0x1E);
            painter.rect_filled(
                Rect::from_min_size(Pos2::new(center.x - 1.2, center.y - 7.0), Vec2::new(2.4, 4.6)),
                CornerRadius::same(0),
                handle,
            );
            painter.rect_filled(
                Rect::from_min_size(Pos2::new(center.x - 5.6, center.y - 2.6), Vec2::new(11.2, 2.4)),
                CornerRadius::same(0),
                rim,
            );
            painter.add(egui::Shape::convex_polygon(
                vec![
                    Pos2::new(center.x - 5.0, center.y - 0.4),
                    Pos2::new(center.x + 5.0, center.y - 0.4),
                    Pos2::new(center.x + 4.2, center.y + 6.8),
                    Pos2::new(center.x - 4.2, center.y + 6.8),
                ],
                yellow,
                Stroke::NONE,
            ));
        }
        Icon::Play => {
            painter.add(egui::Shape::convex_polygon(
                vec![
                    Pos2::new(center.x - 4.5, center.y - 6.5),
                    Pos2::new(center.x + 6.0, center.y),
                    Pos2::new(center.x - 4.5, center.y + 6.5),
                ],
                color,
                Stroke::NONE,
            ));
        }
        Icon::Stop => {
            let r = Rect::from_center_size(center, Vec2::splat(10.0));
            painter.rect_filled(r, CornerRadius::same(1), color);
        }
        Icon::Reload => {
            let blue = Color32::from_rgb(0x4C, 0x92, 0xFF);
            arc(painter, center, 5.6, 0.6, 5.6, Stroke::new(1.8, blue));
            let tip = Pos2::new(center.x + 4.2, center.y - 4.4);
            painter.add(egui::Shape::convex_polygon(
                vec![
                    tip,
                    Pos2::new(tip.x + 3.6, tip.y - 1.0),
                    Pos2::new(tip.x + 1.2, tip.y + 3.0),
                ],
                blue,
                Stroke::NONE,
            ));
        }
        Icon::Folder | Icon::FolderOpen => {
            let amber = if icon == Icon::Folder {
                Color32::from_rgb(0xE3, 0xB3, 0x41)
            } else {
                Color32::from_rgb(0x4C, 0x92, 0xFF)
            };
            let tab = Rect::from_min_max(
                Pos2::new(center.x - 7.0, center.y - 6.0),
                Pos2::new(center.x - 1.0, center.y - 3.5),
            );
            painter.rect_filled(tab, CornerRadius::same(1), amber);
            let body = Rect::from_min_max(
                Pos2::new(center.x - 7.0, center.y - 4.5),
                Pos2::new(center.x + 7.0, center.y + 6.0),
            );
            painter.rect_filled(body, CornerRadius::same(1), amber);
            painter.line_segment(
                [
                    Pos2::new(center.x - 7.0, center.y - 1.0),
                    Pos2::new(center.x + 7.0, center.y - 1.0),
                ],
                Stroke::new(1.0, WIDGET_BG),
            );
        }
        Icon::ArrowDown | Icon::ArrowUp => {
            let dy = if icon == Icon::ArrowDown { 1.0 } else { -1.0 };
            painter.add(egui::Shape::convex_polygon(
                vec![
                    Pos2::new(center.x - 4.5, center.y - 2.5 * dy),
                    Pos2::new(center.x + 4.5, center.y - 2.5 * dy),
                    Pos2::new(center.x, center.y + 3.0 * dy),
                ],
                color,
                Stroke::NONE,
            ));
        }
    }
}

fn arc(painter: &egui::Painter, center: Pos2, radius: f32, from: f32, to: f32, stroke: Stroke) {
    let steps = 22;
    let mut points = Vec::with_capacity(steps + 1);
    for step in 0..=steps {
        let t = from + (to - from) * step as f32 / steps as f32;
        points.push(Pos2::new(
            center.x + radius * t.cos(),
            center.y + radius * t.sin(),
        ));
    }
    painter.add(egui::Shape::line(points, stroke));
}

/// Input field look: `QLineEdit { padding:7px; border:1px solid #303944 }`.
fn field_frame(painter: &egui::Painter, rect: Rect, hovered: bool) {
    painter.rect_filled(rect, CornerRadius::same(6), INPUT_BG);
    painter.rect_stroke(
        rect,
        CornerRadius::same(6),
        Stroke::new(1.0, if hovered { BORDER_HOVER } else { BORDER }),
        egui::StrokeKind::Inside,
    );
}

fn input(ui: &mut egui::Ui, value: &mut String, hint: &str, width: f32) -> egui::Response {
    let (rect, response) = ui.allocate_exact_size(Vec2::new(width, ROW_H), egui::Sense::hover());
    let painter = ui.painter();
    field_frame(painter, rect, response.hovered());
    let inner = Rect::from_min_max(
        Pos2::new(rect.left() + 8.0, rect.top() + 9.0),
        Pos2::new(rect.right() - 8.0, rect.bottom() - 9.0),
    );
    let mut child = ui.new_child(egui::UiBuilder::new().max_rect(inner).layout(
        Layout::left_to_right(Align::Center),
    ));
    child.spacing_mut().item_spacing.x = 0.0;
    child.add(
        egui::TextEdit::singleline(value)
            .frame(egui::Frame::NONE)
            .desired_width(inner.width())
            .hint_text(RichText::new(hint).size(13.0).color(MUTED))
            .text_color(BTN_TEXT)
            .font(FontId::proportional(13.0)),
    );
    response
}

fn form_label(ui: &mut egui::Ui, text: &str) {
    ui.allocate_ui_with_layout(
        Vec2::new(LABEL_W, ROW_H),
        Layout::left_to_right(Align::Center),
        |ui| {
            ui.label(heading(text, 13.0, FORM_LABEL));
        },
    );
}

/// QGroupBox look: rounded border, title sitting on the top line over a
/// `SURFACE` patch that breaks the border.
fn group_box(ui: &mut egui::Ui, title: &str, add: impl FnOnce(&mut egui::Ui)) {
    // `QGroupBox { margin-top:10px; padding-top:10px }`: the widget starts 10px
    // above its border, the title straddles the border and content starts 20px
    // below it. A shape placeholder lets the fill+border land behind the text.
    let top = ui.cursor().min.y;
    let background = ui.painter().add(egui::Shape::Noop);
    let response = egui::Frame::NONE
        .inner_margin(egui::Margin {
            left: 9,
            right: 9,
            top: 30,
            bottom: 3,
        })
        .show(ui, |ui| {
            ui.set_width(ui.available_width());
            add(ui);
        });
    let outer = response.response.rect;
    let border = Rect::from_min_max(Pos2::new(outer.left(), top + 10.0), outer.max);
    ui.painter().set(
        background,
        egui::epaint::RectShape::new(
            border,
            CornerRadius::same(6),
            BG,
            Stroke::new(1.0, BORDER),
            egui::StrokeKind::Inside,
        ),
    );

    let painter = ui.painter();
    let galley = painter.layout_no_wrap(title.to_owned(), bold(13.0), BTN_TEXT);
    let pos = Pos2::new(border.left() + 15.0, border.top() - galley.size().y * 0.5);
    painter.rect_filled(
        Rect::from_min_size(
            Pos2::new(pos.x - 5.0, pos.y - 1.0),
            galley.size() + Vec2::new(10.0, 2.0),
        ),
        CornerRadius::same(0),
        SURFACE,
    );
    painter.galley(pos, galley, BTN_TEXT);
}

/// On/off capsule switch, matching the PyQt5 `SwitchCheckBox` painting:
/// 52x24 track at y+2, 18px knob, 8pt `on`/`off` caption, label at x=62.
fn switch(ui: &mut egui::Ui, value: &mut bool, text: &str, row_h: f32) {
    let (rect, response) = ui.allocate_exact_size(
        Vec2::new(ui.available_width(), row_h),
        egui::Sense::click(),
    );
    if response.clicked() {
        *value = !*value;
    }
    if response.hovered() {
        ui.ctx().set_cursor_icon(egui::CursorIcon::PointingHand);
    }
    let painter = ui.painter();
    let track = Rect::from_min_size(
        Pos2::new(rect.left(), rect.top() + 2.0),
        Vec2::new(52.0, 24.0),
    );
    painter.rect_filled(
        track,
        CornerRadius::same(12),
        if *value { SWITCH_ON } else { SWITCH_OFF },
    );

    let knob_center_x = if *value {
        track.right() - 12.0
    } else {
        track.left() + 12.0
    };
    painter.circle_filled(
        Pos2::new(knob_center_x, track.center().y),
        9.0,
        KNOB,
    );

    // `on` is centred in (5,22) and `off` in (25,22) of the track.
    painter.text(
        Pos2::new(
            track.left() + if *value { 16.0 } else { 36.0 },
            track.center().y,
        ),
        Align2::CENTER_CENTER,
        if *value { "on" } else { "off" },
        FontId::proportional(10.5),
        Color32::WHITE,
    );
    painter.text(
        Pos2::new(track.right() + 10.0, rect.center().y),
        Align2::LEFT_CENTER,
        text,
        FontId::proportional(13.0),
        if response.hovered() {
            Color32::WHITE
        } else {
            LABEL
        },
    );
}

/// Blue brand orb with a soft glow and three orbiting dots.
fn brand_orb(ui: &mut egui::Ui) {
    let (rect, _) = ui.allocate_exact_size(Vec2::splat(BRAND_ROW_H), egui::Sense::hover());
    let painter = ui.painter();
    let center = rect.center();
    for step in (1..=8).rev() {
        let alpha = (13 - step).max(2) as u8;
        painter.circle_filled(
            center,
            14.0 + step as f32 * 2.1,
            Color32::from_rgba_unmultiplied(0x4C, 0x92, 0xFF, alpha),
        );
    }
    painter.circle_filled(center, 15.0, Color32::from_rgb(0x41, 0x61, 0xD8));
    painter.circle_filled(
        center + Vec2::new(-3.5, -3.5),
        12.0,
        Color32::from_rgb(0x6E, 0xA8, 0xFF),
    );
    painter.circle_filled(
        center + Vec2::new(-5.0, -5.0),
        4.6,
        Color32::from_rgba_unmultiplied(0xDC, 0xEB, 0xFF, 230),
    );

    let time = ui.input(|i| i.time);
    for (offset, radius) in [(0.0f64, 3.4f32), (2.1, 2.4), (4.2, 1.7)] {
        let angle = time * 1.15 + offset;
        let dot = center + Vec2::new(9.5 * angle.cos() as f32, 8.5 * angle.sin() as f32);
        painter.circle_filled(
            dot,
            radius,
            Color32::from_rgba_unmultiplied(0xDC, 0xEB, 0xFF, 180),
        );
    }
}

/// Empty drop zone: upload arrow plus the two hint lines, drawn like the
/// original `DropList::paintEvent`.
fn empty_state(ui: &mut egui::Ui, height: f32) {
    let (rect, _) =
        ui.allocate_exact_size(Vec2::new(ui.available_width(), height), egui::Sense::hover());
    let painter = ui.painter();
    let center = rect.center();
    let icon_y = center.y - 38.0;
    let stroke = Stroke::new(2.0, BRAND);
    let top = Pos2::new(center.x, icon_y);
    painter.line_segment([Pos2::new(center.x, icon_y + 34.0), top], stroke);
    painter.line_segment([top, Pos2::new(center.x - 11.0, icon_y + 11.0)], stroke);
    painter.line_segment([top, Pos2::new(center.x + 11.0, icon_y + 11.0)], stroke);
    painter.line_segment(
        [
            Pos2::new(center.x - 21.0, icon_y + 35.0),
            Pos2::new(center.x - 21.0, icon_y + 47.0),
        ],
        stroke,
    );
    painter.line_segment(
        [
            Pos2::new(center.x - 21.0, icon_y + 47.0),
            Pos2::new(center.x + 21.0, icon_y + 47.0),
        ],
        stroke,
    );
    painter.line_segment(
        [
            Pos2::new(center.x + 21.0, icon_y + 47.0),
            Pos2::new(center.x + 21.0, icon_y + 35.0),
        ],
        stroke,
    );
    painter.text(
        Pos2::new(center.x, icon_y + 64.0),
        Align2::CENTER_TOP,
        "将 .hip / .hiplc / .hipnc 文件拖拽到此处",
        bold(15.0),
        LABEL,
    );
    painter.text(
        Pos2::new(center.x, icon_y + 92.0),
        Align2::CENTER_TOP,
        "支持拖拽添加，按顺序处理",
        FontId::proportional(13.0),
        MUTED,
    );
}

/// Dashed rectangle: egui has no dashed stroke, so draw the four sides as
/// alternating segments.
fn dashed_box(painter: &egui::Painter, rect: Rect, color: Color32) {
    let stroke = Stroke::new(1.0, color);
    let dash = 1.0;
    let gap = 2.0;

    let mut x = rect.left();
    while x < rect.right() {
        let end = (x + dash).min(rect.right());
        painter.line_segment([Pos2::new(x, rect.top()), Pos2::new(end, rect.top())], stroke);
        painter.line_segment(
            [Pos2::new(x, rect.bottom()), Pos2::new(end, rect.bottom())],
            stroke,
        );
        x += dash + gap;
    }
    let mut y = rect.top();
    while y < rect.bottom() {
        let end = (y + dash).min(rect.bottom());
        painter.line_segment([Pos2::new(rect.left(), y), Pos2::new(rect.left(), end)], stroke);
        painter.line_segment(
            [Pos2::new(rect.right(), y), Pos2::new(rect.right(), end)],
            stroke,
        );
        y += dash + gap;
    }
}

/// `QProgressBar { height:9px; border-radius:5px }` with the percentage text
/// centred over the track, always visible like the Qt widget.
fn progress_bar(ui: &mut egui::Ui, ratio: f32) {
    let (rect, _) =
        ui.allocate_exact_size(Vec2::new(ui.available_width(), PROGRESS_H), egui::Sense::hover());
    let painter = ui.painter();
    painter.rect_filled(rect, CornerRadius::same(5), INPUT_BG);
    painter.rect_stroke(
        rect,
        CornerRadius::same(5),
        Stroke::new(1.0, BORDER),
        egui::StrokeKind::Inside,
    );
    let ratio = ratio.clamp(0.0, 1.0);
    if ratio > 0.0 {
        let inner = rect.shrink(2.0);
        let filled = Rect::from_min_size(
            inner.min,
            Vec2::new((inner.width() * ratio).max(4.0), inner.height()),
        );
        painter.rect_filled(filled, CornerRadius::same(4), BRAND);
        painter.text(
            rect.center(),
            Align2::CENTER_CENTER,
            format!("{}%", (ratio * 100.0).round() as i32),
            FontId::proportional(12.0),
            BTN_TEXT,
        );
    }
}

fn style(ctx: &egui::Context) {
    let mut visuals = egui::Visuals::dark();
    visuals.panel_fill = BG;
    visuals.window_fill = SURFACE;
    visuals.extreme_bg_color = INPUT_BG;
    visuals.widgets.inactive.bg_fill = WIDGET_BG;
    visuals.widgets.inactive.weak_bg_fill = WIDGET_BG;
    visuals.widgets.inactive.bg_stroke = Stroke::new(1.0, BORDER);
    visuals.widgets.hovered.bg_fill = WIDGET_HOVER;
    visuals.widgets.hovered.weak_bg_fill = WIDGET_HOVER;
    visuals.widgets.hovered.bg_stroke = Stroke::new(1.0, BORDER_HOVER);
    visuals.widgets.active.bg_fill = WIDGET_DOWN;
    visuals.widgets.active.weak_bg_fill = WIDGET_DOWN;
    visuals.widgets.noninteractive.bg_fill = SURFACE;
    visuals.selection.bg_fill = Color32::from_rgb(0x19, 0x3B, 0x67);
    ctx.set_visuals(visuals);

    ctx.all_styles_mut(|style| {
        style.spacing.item_spacing = Vec2::new(8.0, 0.0);
        style.spacing.button_padding = Vec2::new(11.0, 5.0);
        style.spacing.scroll.bar_width = 10.0;
        style.text_styles.insert(egui::TextStyle::Body, FontId::proportional(13.0));
        style.text_styles.insert(egui::TextStyle::Button, FontId::proportional(13.0));
        style.text_styles.insert(egui::TextStyle::Small, FontId::proportional(12.0));
    });
}

/// egui ships no CJK glyphs. Pull a system font so Chinese labels are not
/// rendered as tofu boxes, and a bold face for headings to match the Qt
/// semibold weights.
fn install_cjk_font(ctx: &egui::Context) {
    #[cfg(windows)]
    let candidates = [
        (
            "C:\\Windows\\Fonts\\msyh.ttc",
            Some("C:\\Windows\\Fonts\\msyhbd.ttc"),
        ),
        (
            "C:\\Windows\\Fonts\\msyh.ttf",
            Some("C:\\Windows\\Fonts\\msyhbd.ttf"),
        ),
        ("C:\\Windows\\Fonts\\simhei.ttf", None),
        ("C:\\Windows\\Fonts\\simsun.ttc", None),
    ];
    #[cfg(target_os = "macos")]
    let candidates = [
        (
            "/System/Library/Fonts/PingFang.ttc",
            Some("/System/Library/Fonts/PingFang.ttc"),
        ),
        ("/System/Library/Fonts/Hiragino Sans GB.ttc", None),
    ];
    #[cfg(all(unix, not(target_os = "macos")))]
    let candidates = [
        (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            Some("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        ),
        ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", None),
    ];

    for (path, bold_path) in candidates {
        let Ok(bytes) = std::fs::read(path) else {
            continue;
        };
        let mut fonts = egui::FontDefinitions::default();
        fonts
            .font_data
            .insert("cjk".to_owned(), egui::FontData::from_owned(bytes).into());
        for family in [FontFamily::Proportional, FontFamily::Monospace] {
            fonts
                .families
                .entry(family)
                .or_default()
                .insert(0, "cjk".to_owned());
        }
        if let Some(bold_bytes) = bold_path.and_then(|p| std::fs::read(p).ok()) {
            fonts.font_data.insert(
                "cjk_bold".to_owned(),
                egui::FontData::from_owned(bold_bytes).into(),
            );
            fonts.families.insert(
                FontFamily::Name("cjk-bold".into()),
                vec!["cjk_bold".to_owned(), "cjk".to_owned()],
            );
            HAS_BOLD.store(true, Ordering::Relaxed);
        }
        ctx.set_fonts(fonts);
        return;
    }
}

// Native file pickers. Kept behind tiny wrappers so the rest of the UI does
// not care whether a picker is available on this platform.
#[cfg(windows)]
fn rfd_pick_files() -> Option<Vec<PathBuf>> {
    let output = std::process::Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.OpenFileDialog; $d.Filter = 'Houdini HIP (*.hip;*.hiplc;*.hipnc)|*.hip;*.hiplc;*.hipnc'; $d.Multiselect = $true; if ($d.ShowDialog() -eq 'OK') { $d.FileNames }",
        ])
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&output.stdout);
    let files = text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(PathBuf::from)
        .collect::<Vec<_>>();
    (!files.is_empty()).then_some(files)
}

#[cfg(not(windows))]
fn rfd_pick_files() -> Option<Vec<PathBuf>> {
    None
}

#[cfg(windows)]
fn rfd_pick_file() -> Option<String> {
    let output = std::process::Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.OpenFileDialog; $d.Filter = 'hython.exe (hython.exe)|hython.exe'; if ($d.ShowDialog() -eq 'OK') { $d.FileName }",
        ])
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    (!text.is_empty()).then_some(text)
}

#[cfg(not(windows))]
fn rfd_pick_file() -> Option<String> {
    None
}

#[cfg(windows)]
fn rfd_pick_folder() -> Option<String> {
    let output = std::process::Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }",
        ])
        .output()
        .ok()?;
    let text = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    (!text.is_empty()).then_some(text)
}

#[cfg(not(windows))]
fn rfd_pick_folder() -> Option<String> {
    None
}
