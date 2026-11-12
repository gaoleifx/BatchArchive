# BatchArchive · 项目概述

> 面向接手本项目的开发者 / AI Agent 的入口文档。读完这一篇应能定位代码、理解数据流、知道改哪里安全、知道什么不能碰。
> 最后更新：2026-11-12

---

## 1. 这是什么

**浆果文化 · BatchArchive** —— 一个 Windows 桌面工具，把 Houdini 工程（`.hip` / `.hiplc` / `.hipnc`）连同它依赖的外部资源，打包成一个**自包含的归档目录**，供交接、回传、外发使用。

解决的核心痛点：一个镜头工程散落在项目目录、共享盘、素材盘多处，直接拷 `.hip` 给别人会丢贴图、丢 Alembic、丢 HDA。本工具自动扫描 `hou.fileReferences()`，把真正需要的资源挑出来复制归位，并把工程内的绝对路径改写为 `$HIP/...`，让归档包在任何机器上只要打开就能跑。

**铁律：永不修改源 HIP。** 全流程是 `load 源 → 内存中改参数 → save_as 到归档目录`，源文件只读。

---

## 2. 技术栈与运行环境

| 项 | 值 |
|---|---|
| 语言 | Python 3（无第三方依赖的模块 + PyQt5 GUI） |
| GUI | PyQt5，QSS 深色主题，纯手写绘制控件 |
| Houdini | 通过 `hython.exe` 子进程执行打包脚本 |
| 平台 | Windows only（路径分隔符、注册表式安装目录、`C:\Program Files\Side Effects Software` 硬编码） |
| 启动 | 双击 `launch_gui.cmd`（自动找 Python → 检查/安装 PyQt5 → 启动 app.py） |
| 测试 | 标准库 `unittest`，`QT_QPA_PLATFORM=offscreen` |

依赖外部条件：本机装有 Houdini，`C:\Program Files\Side Effects Software\Houdini <ver>\bin\hython.exe` 存在（GUI 会自动枚举所有已安装版本，也允许手动指定）。

> **分支约定：** 本文描述的是 `main`——纯 Python/PyQt5 版。`rust-shell` 分支上另有一个 egui 单文件 exe 外壳实验（`src/` + 被编译进二进制的 `scripts/package_houdini.py`），**不并入 main**，两者共用同一套 Python 内核与 stdout JSON 协议。2026-11-12 曾合并进 main，同日撤回。要改 Rust 外壳请切到该分支，改动内核时记得两边同步。

---

## 3. 双进程架构（最重要的一张图）

```
┌─────────────────────────────┐        subprocess.Popen        ┌──────────────────────────┐
│  app.py  (普通 Python 进程)  │ ─────────────────────────────► │ package_houdini.py       │
│  PyQt5 主线程 + ArchiveWorker│                                │ (运行在 hython 内)        │
│                             │ ◄──── stdout: JSON lines ───── │  import hou  → 真实可用   │
└─────────────────────────────┘                                └──────────────────────────┘
                                                                │ 写 package_manifest.json
                                                                │ 写 archive_log.txt
                                                                │ save_as 归档 HIP
                                                                ▼
                                                         archive/<stem>/
```

**为什么拆两个进程：** GUI 用普通 Python 跑（启动快、不依赖 Houdini 环境），而 `import hou` 只在 hython 里有效。所以凡是碰 `hou.*` 的逻辑全部下沉到 `package_houdini.py`，GUI 只负责调度、显示、校验。

**进程间协议：** 子进程每行输出一个 JSON 对象，`{"event": "status", ...}` 是进度消息，`{"event": "done", ...}` 是最终摘要。GUI 用 `json.loads` 逐行解析，解析失败的行当作普通日志直接显示。**新增状态输出必须走 `emit()`，不要 `print` 裸文本**，否则 GUI 侧拿不到结构化信息。

**关键设计：GUI 决定归档目录名并传给子进程。** `ArchiveWorker` 先算好 `package_dir`（处理 `_02` 同名冲突），再用 `--package-dir` 传给子进程。这样重试时复用同一个目录、保留已复制的大文件，也不会有两次尝试各占一个坑。

---

## 4. 文件职责

| 文件 | 行数 | 职责 | 改动风险 |
|---|---|---|---|
| `app.py` | ~600 | GUI + 队列调度 + 重试循环 + 结果校验调用 | 中（UI 状态机易卡死） |
| `package_houdini.py` | ~680 | **核心**：扫描引用、分类、过滤、复制、改写路径、写清单 | 高（逻辑密集，改错静默出错包） |
| `archive_policy.py` | ~37 | 两个纯函数：`should_retry` / `is_valid_success` | 低，但语义关键 |
| `repair_hip_paths.py` | ~18 | 一次性修补脚本：把归档 HIP 里的相对路径补成 `$HIP/` | 低，独立工具 |
| `launch_gui.cmd` | ~45 | 启动器：找 Python、装 PyQt5、起 app | 低 |
| `tests/test_app.py` | ~320 | GUI/Worker 测试（off-screen Qt） | — |
| `tests/test_package_houdini.py` | ~340 | 打包逻辑测试（fake `hou` 模块注入） | — |
| `README.md` | — | **面向 GitHub / 用户的主文档**：特性、过滤规则、归档结构、CLI、成功判定 | 需与实现同步 |
| `README.txt` | — | 随包分发的纯文本快速启动（只保留操作步骤，细节指向 README.md） | 低（刻意保持简短） |
| `DESIGN.md` | — | 设计决策流水（**部分已过期**，见 §9） | — |

`archive_policy.py` 被单独拆出来是有意为之：它不 `import hou`、不 `import PyQt5`，所以纯逻辑可被两条测试线共同复用，且能在任何 Python 环境下跑。

---

## 5. 打包流程（`package()` 的执行顺序）

1. **建目录** —— 有 `--package-dir` 就用它（重试复用），否则 `<archive_root>/<stem>`，冲突则 `_02`、`_03`。
2. **加载并扫描** —— `hou.hipFile.load()`，然后 `hou.fileReferences()`。扫描期间起一个守护线程，每秒发一次「仍在扫描…已用时 N 秒」，避免大工程看起来像卡死。
3. **补扫 File Cache 输出** —— `fileReferences()` 有时漏掉 File Cache 2.0 的 `sopoutput`，遍历全场景节点手动补进 `refs`。
4. **逐条处理每个引用**（顺序即优先级，改代码时不要打乱）：
   - 渲染器节点 → 排除（默认始终生效）
   - 输出目录 / 缓存输出 / 渲染输出 → 过滤
   - 资源分类 → 按分类勾选过滤
   - 脚本/配置文件 → 跳过
   - 红色节点 / 外部资源节点 AND 规则 → 过滤
   - `temp:` / `op:` / `opdef:` / `oplib:` → 跳过（不是文件资源）
   - 含未解析变量（`$` 或 `%` 还在）→ **记为失败 `unresolved_path`**，不静默跳过
   - Houdini 安装目录内 → 跳过
   - 反引号表达式（序列）→ glob 全部匹配文件、整目录复制、改写
   - 工程目录本身或更上层 → 只记录、不改参数、不复制（防止把整个项目卷进包里）
   - Alembic 目录引用 → 改写成归档目录，复制交给文件级引用
   - 普通文件/目录 → 复制 + 改写为 `$HIP/...`
   - 都不匹配 → 记为失败 `not_found`
5. **Alembic 空目录检查** —— 目录引用重建后如果没有对应文件，判失败 `empty_alembic_directory`（不允许产出看起来成功但缺资源的包）。
6. **保存** —— `hou.hipFile.save(dest)` 到归档目录。
7. **写出结果** —— 先 `archive_log.txt`（`utf-8-sig`，保证记事本能正常显示中文），再 `package_manifest.json`（`utf-8`）。两者都是 **`.tmp` + `os.replace` + `fsync` 原子写入**，避免半个文件被下游当成有效清单。

---

## 6. 过滤体系

### 6.1 资源大类（GUI「规则一」）

后端标记：`Image/Textures`、`Geometry`、`Alembics`、`USDs`、`HDAs`。判定先看扩展名，再看路径包含的标准目录名（`otls` / `usd` / `abc` / `geo` / `textures`）。目录形态的引用按「目录内直接包含的文件扩展名」反推类别。

未识别类别的资源**保留原有打包行为**（不会因为分类过滤被误杀）。

### 6.2 节点细分（GUI「规则二」）

- `--filter-red-nodes`：只打包节点颜色偏红的（`r >= 0.5 且 r >= g*1.35 且 r >= b*1.35`）
- `--filter-external`：只打包引用工程外部资源的节点

两者**同时勾选是 AND 关系**；都不勾选则不加节点限制。

### 6.3 输出过滤（默认全开）

- `--include-cache-outputs` 取反 → 跳过 `bgeo/bgeo.sc/sim/vdb/pc/pdc` 等缓存输出
- `--include-render-outputs` 取反 → 跳过 `exr/png/jpg/tif` 等渲染输出
- `--include-auxiliary-files` 取反 → 跳过 json/py/txt/log/md/xml/yaml/csv/ini/bat/sh 等脚本配置（**含被复制目录内部的文件**）

注意「输入 vs 输出」的区分：`/geo/`、`/cache/`、`/sim/`、`/render/` 下的文件既可能是缓存输出，也可能是 SOP File 节点读取的输入。代码靠「节点/参数是否属于写入者」来判断（`filecache` 无输入的节点视为只读缓存源，必须保留）。**这是最容易改出 bug 的地方。**

### 6.4 渲染器排除

Mantra / Karma / Arnold / Redshift / RenderMan / USD Render / V-Ray / Octane，以及 Render Gallery 的 Manager 节点。裸词 `render` 只在节点已经属于 `driver` / `lop` 类别时才当信号——否则 `render` 这类常见子串会误杀大量非渲染节点。

> ⚠️ 渲染器排除**没有开关，始终开启**（`package()` 的 `exclude_renderer_nodes` 默认为真，CLI 不提供关闭参数）。README 已于 2026-11-11 按此更正。若要改成可配置项，需同时改 `package()` 默认值、CLI 解析、GUI 控件、README 四处，缺一处就会出现「界面能关但实际没关」的假开关。

---

## 7. 归档目录布局与路径改写

**工程内资源** → 保持相对工程目录的路径；落在标准 Houdini 目录（`geo` / `abc` / `render` / `anim` / `otls` / `galleries` / `textures` / `audio` / `scripts` / `cache`）下的保持原目录，不再塞进 `external`。

**外部资源** → 按类别进 `textures/`、`geo/`、`abc/`、`usd/`、`otls/`，未识别类别进 `external/`。子目录统一为 `<源文件夹名>_<短哈希8位>`，用于区分 `v001`/`v003` 并防同名冲突。

**改写规则：** 所有归档后的引用一律写成 `$HIP/<相对路径>`。
- 原本就是 `$HIP` / `$JOB` 且指向工程内部的，不重复改写
- 指向工程目录本身或更上层的参数，**原样保留不改**
- 反引号序列：保留序列语义，文件名部分取自当前求值结果

---

## 8. 成功判定与重试

判成功必须**同时**满足（`is_valid_success`）：
1. 子进程退出码 `0`
2. 清单存在且可解析
3. `status == "success"` 且 `failures` 为空
4. 清单里的 `source` 与本次任务一致（`normcase(abspath)` 比较）
5. 清单里的 `package` 的父目录就是本次 `package_dir`
6. 该归档 HIP **确实存在于磁盘**

任何一条不满足都不算完成 —— 这一步是为了防「子进程崩了但留下上一次的旧清单，GUI 误报成功」。

**重试策略**（`should_retry`）：最多 3 次。只有 `copy_failed` / `target_missing` 这类可能靠重试恢复的故障才重试；`not_found`、`unresolved_path`、`empty_alembic_directory` 是确定性错误，直接停，不做无效重跑。

每次尝试前都会 `unlink` 旧的 `package_manifest.json` 和 `archive_log.txt`；删不掉就不启动打包（宁可不跑，也不接受旧结果）。

---

## 9. 文档同步状态

2026-11-11 已把 `README.txt` 与 `DESIGN.md` 对齐到当前实现，具体改动：

- `README.txt`
  - 删除并不存在的 `启动BatchArchive.bat`，改为说明启动器会自动找 Python 3 并在缺 PyQt5 时自动安装。
  - 渲染器排除改为「始终生效、无开关」，并补齐 RenderMan、Render Gallery。
  - 脚本/配置扩展名列表补全到与 `AUXILIARY_EXTENSIONS` 一致。
  - 「打包过滤规则」按现有三组（规则一 / 规则二 / 输出过滤）重写，折叠入口补上顶部按钮。
- `DESIGN.md`
  - `Tokens` 换成 QSS 实际色值：brand `#1677FF`、background `#0D1117`、surface `#151B23` / `#0F141A`、border `#303944`、text `#F0F3F6`、muted `#8B949E`，并补 `status-ok #52D273`。
  - `Components` 按现有控件重写，移除已不存在的 `Latest HIP scanner`、`Result summary`。
  - `animation: none` 修正为「品牌球体 55ms QTimer + 打包在 worker 线程」。
  - `Tkinter` 那条决策标注为被 PyQt5 取代。

**维护约定：** `DESIGN.md` 的 `Decisions` 是历史日志，旧条目不改写内容，只用删除线标注取代关系；新决策追加在末尾，不重排顺序。改 UI 颜色或过滤规则时，DESIGN 与 README 要同步更新，别让第三次漂移发生。

### 2026-11-12 · README 改为 README.md

- 新增 `README.md` 作为唯一的完整文档（GitHub 首页渲染）：特性、环境要求、GUI 流程、三组过滤规则、归档目录树、CLI 参数表、六条成功判定与重试策略、项目结构、开发测试、已知限制。
- `README.txt` 收敛为纯文本快速启动（约 15 行），只留操作步骤与关键默认值，细节一律指向 `README.md`。**新增细节写进 README.md，不要往 txt 里加**，否则两份又会对不上。
- 内容全部按当前实现核对过：CLI 参数取自 `package_houdini.py` 的 `sys.argv[3:]` 分支，分类与目录映射取自 `classify_resource` / `category_dirs` / `standard_rel`，启动器行为取自 `launch_gui.cmd`。

---

## 10. 开发约定

**做：**
- 改打包逻辑前先跑 `python -m unittest discover -s tests`，全绿再动手。
- 新增过滤规则 → 在 `manifest` 里留下 `filtered_by` 标记，并补对应计数器写进 `summary.filtered`。归档包是否可信，全靠这份清单可追溯。
- 新增失败原因 → 想清楚它是「瞬时」还是「确定性」，前者加进 `archive_policy.TRANSIENT_FAILURE_REASONS`。
- UI 线程只做显示。任何耗时操作走 `ArchiveWorker`，通过 `events` 队列回传（`_poll` 每 150ms 消费一次）。
- 改 `package()` 的签名或参数含义时，同步改 `__main__` 里的 CLI 解析**和** `app.py` 里拼 cmd 的那段，两处必须一致。

**不要：**
- 不要在任何路径上修改源 HIP。
- 不要在 `package_houdini.py` 顶层之外做 `import hou`（GUI 进程里没有 hou）。
- 不要用裸 `print` 输出状态；统一 `emit()`。
- 不要把清单/日志写成非原子操作。
- 不要为了「让它跑通」而放宽 `is_valid_success`。
- 不要用 `rm`/通配符清理 `archive/` 之外的用户目录。

**测试注意：** `tests/test_package_houdini.py` 通过往 `sys.modules` 注入 fake `hou` 模块来脱离 Houdini 环境；`tests/test_app.py` 需要 `QT_QPA_PLATFORM=offscreen`。新增测试沿用这两套桩，不要引入真实 Houdini 依赖。

`test_app.py` 依赖 PyQt5，**必须用装了 PyQt5 的解释器跑**。本机是 `%LOCALAPPDATA%\Programs\Python\Python311\python.exe`（与 `launch_gui.cmd` 选中的是同一个）；用 IDE 自带的 Python 3.13 会直接 `ModuleNotFoundError: No module named 'PyQt5'`。

```bash
"%LOCALAPPDATA%/Programs/Python/Python311/python.exe" -m unittest discover -s tests
```

**文档维护：** 结构性改动（新增模块、改变进程边界、增删过滤维度、调整 CLI 参数）要同步更新 `AGENTS.md`（本文件）、`README.md` 与 `DESIGN.md`。本文件是接手者的第一入口，写错比不写更坑。

---

## 11. 常见故障速查

| 现象 | 大概率原因 |
|---|---|
| 日志停在「仍在扫描 HIP 文件引用…」 | 超大工程，`hou.fileReferences()` 本身慢，属正常；不要误判卡死 |
| 报 `not_found` | 工程里引用的资源真的不在磁盘上，或被移动/改名 |
| 报 `unresolved_path` | 环境变量没配置，`expandString()` 解不出来 |
| 报 `empty_alembic_directory` | 只有目录级引用、没有文件级引用，重建后是空目录 |
| 一直重试三次都失败 | 看 `package_manifest.json` 的 `failures[].reason`；确定性错误本身不该重试（若出现，检查 `should_retry`） |
| GUI 卡在「打包中」 | `ArchiveWorker.run()` 的 `finally` 里必须 `events.put(("finished", ...))`，别吞掉 |
| 归档 HIP 打开丢贴图 | 检查该资源的 `filtered_by` —— 大概率被输出过滤或分类过滤误伤 |

---

## 12. 后续可做（未实现，非承诺）

- 把「渲染器排除」做成可配置项。需要同步改四处：`package()` 默认值、CLI 参数解析、GUI 开关控件、README 说明；只改 GUI 会得到假开关。
- 打包前预检：汇总所有 `not_found` / `unresolved_path` 一次性列出，而不是边打边报。
- 归档包体积预估与磁盘空间检查。
- 归档目录一键回滚/清理旧版本。
