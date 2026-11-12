# BatchArchive Design System

> 本文以 `app.py` 的实现为准。数值取自 `MainWindow._style()` 的 QSS 与自定义绘制代码。

## Stack
- framework: Python 3 + PyQt5 桌面应用（主进程不加载 Houdini）
- packaging: 打包逻辑运行在 `hython.exe` 子进程中，通过 stdout 的 JSON 行回传状态
- styling: 单一 QSS 深色主题（`MainWindow._style()`），无外部样式表文件
- components: 品牌行（含自绘动画球体）、hython 版本选择、输出目录选择、拖放队列、进度条与运行日志、可折叠过滤面板、打包按钮状态机
- animation: 仅品牌球体一个 55ms `QTimer`；打包全程在 `ArchiveWorker` 线程，不阻塞 UI

## Tokens
- brand: `#1677FF`（hover `#4096FF`，pressed `#0958D9`）
- status-ok: `#52D273`
- background: `#0D1117`
- surface: `#151B23`（面板）/ `#0F141A`（输入框、列表等下沉面）
- border: `#303944`
- text: `#F0F3F6`
- muted: `#8B949E`
- font: `Microsoft YaHei UI`，正文 10pt

## Decisions
- 2026-08-24 — ~~Tkinter chosen so the tool runs without third-party packages.~~ **已被取代**：GUI 后续迁移到 PyQt5（需要拖放、自绘控件与 QSS 主题），启动器 `launch_gui.cmd` 负责在缺依赖时自动安装 PyQt5。
- 2026-08-24 — packaging runs in a worker Houdini process and never edits source HIP.
- 2026-08-24 — Houdini selector lists all detected installations and synchronizes the hython path.
- 2026-08-24 — added two-level resource and node filtering in a collapsible right panel.
- 2026-09-11 — packaging action state: the disabled start button reads “打包中” while the stop button takes the brand-blue primary treatment; both return to idle styling when the worker finishes.
- 2026-10-23 — a file reference pointing at the project folder or one of its ancestors is recorded and skipped instead of copied, so a `$HIP` style path parameter cannot sweep every other scene into the package.
- 2026-10-23 — external Alembic packaging copies only the referenced file and rebuilds its folder on demand; folder parameters are rewritten to the rebuilt folder instead of copying their contents.
- 2026-10-23 — script and config files (json, py, txt, log, md, xml, yaml, csv, ini, bat, sh) are skipped by default, both as direct references and inside copied folders; the switch and `--include-auxiliary-files` keep them.
- 2026-11-11 — renderer and Render Gallery nodes are excluded unconditionally; the previous “规则二默认可取消勾选” wording in README was removed because no such switch exists.
- 2026-11-11 — tokens, components and stack rewritten against the actual QSS; the old `#4F8CFF` / `#111827` values and the “latest HIP scanner” component no longer described the shipped UI.

## Components
- Brand row — `浆果文化 | BatchArchive`、`BrandLoader` 自绘球体、右上角就绪状态点
- Hython selector — 版本下拉（枚举 `C:\Program Files\Side Effects Software\Houdini */bin/hython.exe`）+ 可手动输入的路径框 + 刷新/浏览
- Output selector — 留空则每个 HIP 使用同目录 `archive`，填写则全部共用该目录
- HIP drop queue — `DropList`，接受拖放与「添加文件」，空态用 `paintEvent` 自绘提示；状态以 `路径    [等待/处理中/完成/失败/已停止]` 后缀展示，真实路径存于 `Qt.UserRole`
- Progress + run log — 进度按已完成 HIP 数推进；日志为只读 `QPlainTextEdit`，带 `HH:MM:SS` 前缀并自动滚底
- Packaging action — `_set_packaging_ui()` 统一控制开始/停止按钮的文案、可用性与 `packagingActive` 动态属性
- Filter panel — 规则一（五类资源大类）、规则二（红色节点 / 外部资源，AND）、输出过滤（缓存输出 / 渲染输出 / 脚本配置）；面板内「折叠」与顶部「隐藏过滤规则」共用 `_toggle_filter_panel()`
- SwitchCheckBox — 自定义 `paintEvent` 绘制的 on/off 开关，替代原生 checkbox 指示器
