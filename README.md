# BatchArchive · 浆果文化

**把 Houdini 工程打包成可交付的自包含归档。**

[![platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows&logoColor=white)](#环境要求)
[![python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)](#环境要求)
[![houdini](https://img.shields.io/badge/Houdini-hython-FF4713)](#环境要求)
[![gui](https://img.shields.io/badge/GUI-PyQt5-41CD52?logo=qt&logoColor=white)](#环境要求)

一个镜头工程往往散落在项目目录、共享盘、素材盘好几个地方。直接把 `.hip` 拷给别人，结果就是丢贴图、丢 Alembic、丢 HDA。

BatchArchive 读一遍工程的 `hou.fileReferences()`，把真正被引用的外部资源挑出来复制到归档目录里归好位，再把工程里的绝对路径改写成 `$HIP/...`。归档包拿到任何一台装了 Houdini 的机器上，打开就能跑。

> **铁律：不修改源 HIP。** 全流程是 `load 源 → 内存中改参数 → save_as 到归档目录`，源文件从头到尾只读。

---

## 目录

- [特性](#特性)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [使用流程](#使用流程)
- [过滤规则](#过滤规则)
- [归档目录长什么样](#归档目录长什么样)
- [命令行用法](#命令行用法)
- [产物与成功判定](#产物与成功判定)
- [项目结构](#项目结构)
- [开发与测试](#开发与测试)
- [已知限制](#已知限制)

---

## 特性

- **自动扫描真实引用** —— 基于 `hou.fileReferences()`，并对 File Cache 2.0 之类容易漏的 `sopoutput` 做二次补扫。
- **外部资源分类归位** —— 贴图 / 几何 / Alembic / USD / HDA 各回各家，不再一股脑塞 `external/`。
- **路径全部改成 `$HIP/...`** —— 归档包搬机器、换盘符都不散架；原本就指向工程内部的 `$HIP` / `$JOB` 不会重复改写。
- **不乱搬工程目录** —— 参数如果指向工程目录本身或更上层（PDG 的 working dir 写成 `$HIP` 是常见写法），只记录不改写、不复制，避免把整卷项目和备份一起卷进包里。
- **不会静默产出残包** —— 复制失败、路径变量没解析、Alembic 目录为空，全部进 `failures` 并被判失败，而不是"看起来成功"。
- **可追溯** —— 每个归档目录都有人看的 `archive_log.txt` 和给程序校验的 `package_manifest.json`，含分类计数与过滤原因。
- **失败会自己重来一次** —— 复制类瞬时故障最多重试 3 次，复用同一个归档目录保留已拷贝的大文件；资源不存在这类确定性错误直接停，不做无效重跑。
- **深色 GUI + 拖放队列** —— 深色主题、拖文件即入队、批量顺序打包、逐项状态显示、可折叠过滤面板。

---

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows（路径处理与 Houdini 安装目录探测依赖 Windows 约定） |
| Python | Python 3（GUI 需要 PyQt5，启动器会在缺失时尝试自动安装） |
| Houdini | 任意已安装版本，需要其中的 `hython.exe` |

Houdini 的查找位置是 `C:\Program Files\Side Effects Software\Houdini <版本>\bin\hython.exe`。GUI 会自动枚举所有已装版本，也可以手动填路径。

---

## 快速开始

1. 克隆或下载本仓库到本地任意目录。
2. 双击 `launch_gui.cmd`。
3. 启动器会自动找本机 Python 3；检测到缺 PyQt5 时会尝试 `pip install PyQt5`。

也可以手动启动：

```bat
python app.py
```

---

## 使用流程

1. **加工程** —— 把一个或多个 `.hip` / `.hiplc` / `.hipnc` 拖进窗口，或点「添加文件」。列表顺序就是打包顺序。
2. **选 Houdini** —— 版本下拉里选一个，路径框会同步；装在多处就点「刷新版本」。
3. **定输出** —— 留空则每个 HIP 输出到自己同级的 `archive/`；填了「输出目录」则全部集中到该目录。
4. **配过滤** —— 右侧「打包过滤规则」按需调整，默认全开见下一节。
5. **点「开始打包」** —— 状态逐个显示在文件后面（等待 / 处理中 / 完成 / 失败 / 已停止），日志区实时滚出 hython 的输出。中途可以「停止」。

打包全程跑在后台线程，界面不会卡；真正的 `import hou` 只发生在 `hython.exe` 子进程里。

---

## 过滤规则

界面右侧面板分三组，**默认全开**。

### 规则一 · 资源大类

| 类别 | 判定依据 | 归档位置 |
|---|---|---|
| `Image/Textures` | `.jpg .png .tif .exr .hdr .rat .tx` 等，或路径含 `textures` | `textures/` |
| `Geometry` | `.bgeo .bgeo.sc .vdb .sim .obj .fbx .ply .stl` 等，或路径含 `geo` | `geo/` |
| `Alembics` | `.abc`，或路径含 `abc` / `alembic` | `abc/` |
| `USDs` | `.usd .usda .usdc .usdz`，或路径含 `usd` | `usd/` |
| `HDAs` | `.hda .hdalc .hdanc`，或路径含 `otls` | `otls/` |

识别不出类别的资源**不会**因为分类过滤被丢掉——保持原有打包行为。

### 规则二 · 节点细分

- **只打包红色节点** —— 节点颜色满足 `r >= 0.5 且 r >= g×1.35 且 r >= b×1.35`。
- **只打包外部资源节点** —— 节点引用的资源在工程目录之外。

两个勾选框**同时勾是 AND 关系**；都不勾则不额外限制节点。

### 输出过滤

- **跳过缓存输出** —— `bgeo / bgeo.sc / sim / vdb / pc / pdc` 这类缓存节点的输出目录与序列。
- **跳过渲染输出** —— ROP / LOP 渲染节点的 `exr / png / jpg / tif` 等输出。
- **跳过脚本与配置** —— `json / py / pyc / txt / log / md / xml / yaml / yml / csv / ini / cfg / toml / bat / cmd / sh / ps1`，**包含被复制目录内部**的这些文件。

取消勾选就把它们也纳入打包。外部输入资源不受输出过滤影响。

> 缓存输出和 SOP File 读取的输入可能落在同一个 `/geo/`、`/cache/` 目录里，代码靠"节点是不是写入者"来区分（`filecache` 无输入时视为只读缓存源，必须保留）。这是这套过滤里最容易改出 bug 的地方。

### 恒定行为

**渲染器节点始终不参与资源打包**，包括 Mantra、Karma、Arnold、Redshift、RenderMan、USD Render、V-Ray、Octane，以及 Render Gallery 的 Manager 节点。这一条没有开关，界面上也找不到对应勾选项。

另外：Houdini 安装目录内的资源、`temp:` / `op:` / `opdef:` / `oplib:` 这类非文件引用，一律跳过。

---

## 归档目录长什么样

```text
archive/
└── shotA/                            # 默认 <HIP 文件名>，同名自动 _02、_03
    ├── shotA.hip                     # 已改写路径的工程副本（源文件未动）
    ├── archive_log.txt               # 人看的归档记录（utf-8-sig，记事本可读）
    ├── package_manifest.json         # 程序校验用的清单
    ├── geo/                          # 工程内的标准 Houdini 目录保持原结构
    ├── textures/
    ├── abc/
    │   └── fxCache_a1b2c3d4/         # 外部资源：<源文件夹名>_<8位哈希>
    ├── usd/
    ├── otls/
    └── external/                     # 未能分类的外部资源
```

- **工程内资源** —— 保持相对工程目录的路径；落在标准 Houdini 目录（`geo` `abc` `render` `anim` `otls` `galleries` `textures` `audio` `scripts` `cache`）下的保持原目录，不再塞进 `external`。
- **外部资源** —— 按类别进对应目录，子目录统一为 `<源文件夹名>_<短哈希>`。哈希用来区分 `v001` / `v003`，也顺手解决同名冲突。
- **外部 Alembic** —— 只复制被参数引用的那一个文件，目标目录按需创建，不搬整个版本文件夹。指向文件夹的参数只重写为归档目录，由文件级引用去填充。
- **Alembic 空目录** —— 如果只有目录引用、没有任何文件级引用，归档后会检测到空目录并直接判失败。

---

## 命令行用法

GUI 只是壳，核心脚本可以单独跑。必须用 `hython` 而不是普通 Python（脚本顶层就 `import hou`）：

```bat
hython.exe -u package_houdini.py <源 HIP> <归档根目录> [选项]
```

| 选项 | 作用 |
|---|---|
| `--package-dir <目录>` | 指定本次归档目录（GUI 用它复用重试目录；不传则自动 `<归档根>/<stem>`） |
| `--categories Image/Textures,Geometry,Alembics,USDs,HDAs` | 只打包这些大类；传空串表示不过滤 |
| `--filter-red-nodes` | 只处理红色节点 |
| `--filter-external` | 只处理引用外部资源的节点 |
| `--include-cache-outputs` | 不过滤缓存输出 |
| `--include-render-outputs` | 不过滤渲染输出 |
| `--include-auxiliary-files` | 不跳过脚本与配置文件 |

示例：

```bat
"C:\Program Files\Side Effects Software\Houdini 20.5\bin\hython.exe" -u package_houdini.py ^
    "D:\proj\shots\shotA\houdini\shotA.hip" "D:\proj\shots\shotA\houdini\archive"
```

子进程通过 stdout 每行一个 JSON 回传状态（`{"event": "status", ...}` / `{"event": "done", ...}`），非 JSON 行当作普通日志。

---

## 产物与成功判定

归档目录里两个文件：

- **`archive_log.txt`** —— 给人和邮件看的：结果、源工程、资源统计、分类计数、导出内容清单、失败项及原因。
- **`package_manifest.json`** —— 给程序校验的：`status`、`failures`、`source`、`package`、`summary`（含 `filtered` 各项计数）。

两个文件都是 `.tmp` + `os.replace` + `fsync` 的**原子写入**，不会出现半个文件被下游当成有效清单。

打包算完成，必须**同时**满足：

1. hython 退出码为 `0`
2. 清单存在且能解析
3. `status == "success"` 且 `failures` 为空
4. 清单里的 `source` 与本次任务一致
5. 清单里的 `package` 父目录就是本次归档目录
6. 归档 HIP 确实落在磁盘上

任何一条不满足都不算完成——这道关卡专门用来防"子进程崩了但留着上一次的旧清单，GUI 误报成功"。

**重试策略：** 单文件最多 3 次。只有 `copy_failed` / `target_missing` 这类可能自愈的故障才重试；`not_found` / `unresolved_path` / `empty_alembic_directory` 是确定性错误，直接停。每次尝试前先删掉旧清单和旧日志，删不掉就不启动。

---

## 项目结构

```text
BatchArchive/
├── app.py                  # PyQt5 GUI + 队列调度 + 重试循环 + 结果校验
├── package_houdini.py      # 核心：扫描引用、分类、过滤、复制、改写路径、写清单
├── archive_policy.py       # 纯逻辑：should_retry / is_valid_success
├── repair_hip_paths.py     # 一次性修补脚本：给归档 HIP 补 $HIP/ 前缀
├── launch_gui.cmd          # 启动器：找 Python、装 PyQt5、起 app
├── tests/                  # unittest，一套 fake hou，一套 offscreen Qt
├── AGENTS.md               # 给接手者/AI 的架构说明（数据流、改哪安全）
├── DESIGN.md               # UI 设计系统与决策流水
└── README.md
```

架构上最关键的一点是**双进程**：GUI 跑在普通 Python 里（启动快、不依赖 Houdini 环境），所有碰 `hou.*` 的逻辑都在 `hython.exe` 子进程里。改代码时别在 `package_houdini.py` 之外 `import hou`。

---

## 开发与测试

```bat
python -m unittest discover -s tests
```

- `tests/test_package_houdini.py` 往 `sys.modules` 注入 fake `hou`，不需要真 Houdini。
- `tests/test_app.py` 需要 PyQt5，且要 `QT_QPA_PLATFORM=offscreen`（用装了 PyQt5 的解释器跑）。

改打包逻辑前先跑一遍全绿再动手。新增过滤规则记得在 manifest 里留下 `filtered_by` 标记并补对应计数——归档包可不可信，全靠这份清单可追溯。

---

## 已知限制

- **只支持 Windows**，Houdini 安装目录是硬编码探测的。
- 引用里还剩 `$` / `%` 没解析出来（变量缺失）会记为失败 `unresolved_path`，不会猜路径。
- 渲染器排除目前不可配置。要改成开关得同时动四处：`package()` 默认值、CLI 解析、GUI 控件、文档，缺一处就会出现"界面能关但实际没关"的假开关。
- 目标目录已存在时自动 `_02`、`_03`，**不覆盖**旧包，所以反复打包会堆积目录，需要手动清理。

---

## 许可

内部工具，仓库暂未声明开源许可证。使用与分发请先联系作者。
