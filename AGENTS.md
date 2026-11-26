# BatchArchive · 项目概述

> 面向接手本项目的开发者 / AI Agent 的入口文档。读完这一篇应能定位代码、理解数据流、知道改哪里安全、知道什么不能碰。
> 最后更新：2026-11-26

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
| `package_houdini.py` | ~1080 | **核心**：扫描引用、分类、过滤、复制、改写路径、写清单 | 高（逻辑密集，改错静默出错包） |
| `archive_policy.py` | ~37 | 两个纯函数：`should_retry` / `is_valid_success` | 低，但语义关键 |
| `hip_version.py` | ~150 | 纯逻辑：读 HIP 保存版本（`_HIP_SAVEVERSION`）+ 按版本挑 hython | 低，但判据别改错 |
| `repair_hip_paths.py` | ~18 | 一次性修补脚本：把归档 HIP 里的相对路径补成 `$HIP/` | 低，独立工具 |
| `launch_gui.cmd` | ~85 | 启动器：挑一个**带 PyQt5** 的 Python、装依赖、起 app。**必须纯 ASCII + CRLF**，不能放中文 | 低 |
| `tests/test_app.py` | ~415 | GUI/Worker 测试（off-screen Qt） | — |
| `tests/test_package_houdini.py` | ~930 | 打包逻辑测试（fake `hou` 模块注入） | — |
| `tests/test_hip_version.py` | ~130 | 版本探测与 hython 匹配测试（纯函数，无需 Qt/hou） | — |
| `README.md` | — | **面向 GitHub / 用户的主文档**：特性、过滤规则、归档结构、CLI、成功判定 | 需与实现同步 |
| `README.txt` | — | 随包分发的纯文本快速启动（只保留操作步骤，细节指向 README.md） | 低（刻意保持简短） |
| `DESIGN.md` | — | 设计决策流水（**部分已过期**，见 §9） | — |

`archive_policy.py` 被单独拆出来是有意为之：它不 `import hou`、不 `import PyQt5`，所以纯逻辑可被两条测试线共同复用，且能在任何 Python 环境下跑。`hip_version.py` 同理——GUI 和 hython 侧都要用它，且要能在裸 Python 下测。

---

## 5. 打包流程（`package()` 的执行顺序）

1. **建目录** —— 有 `--package-dir` 就用它（重试复用），否则 `<archive_root>/<stem>`，冲突则 `_02`、`_03`。
2. **加载并扫描** —— `hou.hipFile.load()`，随即读 `hou.applicationVersionString()` 与 `--expect-version` 比对（不一致只记日志与清单，不判失败），然后 `hou.fileReferences()`。扫描期间起一个守护线程，每秒发一次「仍在扫描…已用时 N 秒」，避免大工程看起来像卡死。
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
   - 序列引用（反引号表达式、`$F`/`$F4`/`$SF`/`$SF4`/`$FF`）→ token 化后 glob 全部匹配帧、逐帧复制、改写时保留原表达式
   - 帧 token 已被解析掉的缓存输出（File Cache 2.0 的 `sopoutput`）→ 见 §6.3 的「单帧反推」，仍走上面的序列分支
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

另外：**File Cache HDA 内部的节点跟随缓存输出开关**。File Cache 2.0 会在 HDA 里自动建 `read_back`（一个 `file` SOP，参数是 `` `chs("../sopoutput")` ``），它读的正是该 HDA 写的缓存。`inside_filecache()` 会把这类内部引用判为 `cache_output`，默认随缓存一起跳过——否则包里会留下「半条缓存 + 一个过期帧」，比整条跳过更糟。勾选「包含缓存输出」时它们按普通依赖处理。

**单帧反推（`infer_sequence()`，2026-11-26）**：`hou.fileReferences()` 给出的 `sopoutput` 是**已经算好的单帧绝对路径**（`..._v1.0149.bgeo.sc`），帧 token 只剩在 `descriptivelabel`（`` `opname("..")`_$OS_v1.$F4.bgeo.sc ``）里；`read_back/file` 存的甚至是个旧帧号。因此序列判定不能只看 `raw`：

1. `sequence_pattern(raw)` 无 token 时，才允许反推；
2. 反推必须有**证据**（`sequence_evidence()` + `has_frame_token()`）：参数自身未展开值、或父 File Cache HDA 的 `descriptivelabel` / `sopoutput` / `filepattern` 里还有帧 token。只有 `chs(...)` 这类纯间接引用**不算证据**；
3. 用 `FRAME_DIGITS_LEAF`（`prefix + 数字 + 非数字后缀`）拆解已解析的文件名，glob 同目录同前后缀、**等宽数字**的兄弟文件，命中 ≥2 个才算序列，token 按位宽还原成 `$F4` / `$F`；
4. 清单记 `sequence_inferred: true`，状态行会打「缓存输出已被解析为单帧，按帧号推断出序列」。
5. 从 HDA 借来的 token 必须**能对上这个文件名**（`evidence_matches_leaf()`）：`` `opname("..")` ``、`$OS` 这类 Python 解析不了的片段当 `.*`，只核对字面量与帧位宽。否则「同目录里另一条看着像的序列」会被误扩。参数自己本来就有 token 的（`raw` / `unexpandedString`）不受此限，本来就无需核对。
6. 缓存 HDA 的帧范围是**单帧**（`f1 == f2`，`cache_frame_span()`）时**不反推**：那是刻意写成的静帧缓存，兄弟帧不属于工程。

没有证据就不许放宽：一个普通 SOP File 指着某一帧，不该把同目录的兄弟帧一起卷走。补扫循环（§5 第 3 步）只在 `sopoutput` 完全没进 `fileReferences()` 时才追加引用，所以「已进 refs 但值是单帧」这条路只能靠反推兜住。

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
- 序列：**保留原始表达式**。文件名部分直接用 `raw` 的 basename（含 `$F4`、反引号），绝不替换成求值后的帧号——否则序列会被固定成单帧。`$HIPNAME`/`$OS` 也保持原样，交由 Houdini 打开时按节点解析
- **表达式/通道驱动的参数不写**（`parm_is_computed()`：有 keyframe、有 `expression()`、或 `getReferencedParm()` 指向别人）。File Cache 2.0 的三个特征全占，`Parm.set()` 不抛错但**求值不变**，硬写只会在动画曲线上加一个键、并在清单里留下假的 `rewritten`。这类参数只要表达式基于 `$HIP`（默认 `basedir = $HIP/geo`），存档后自然指向归档副本，清单记 `rewrite_skipped: computed_parameter`；若它指向工程外又改不动，判失败 `rewrite_failed`，不交付仍指着源盘的包。`rewrite_parm()` 对可写的参数也会 `evalAsString()` 复核一遍，不再默认 `set()` 成功

### 7.1 去重与计数口径

**复制是按「归档目标路径」去重的，不是按引用数。** 归档目标由 `storage_info()` 从**源路径**确定性推导（工程内→相对路径，外部→`<类别>/<文件夹名>_<短哈希>`），所以多个节点指向同一套缓存时算出来的 `$HIP` 目标必然重合，第二处引用不会再写一遍字节。

- 文件级：`copy_item()` 用 `same_archived_file()` 判断「已存在且大小 + mtime 都一致」→ 跳过 `copy2`。**不能只看大小**：重试复用归档目录时，缓存被重写成同样字节数就会被永久留成旧版。mtime 容差 2 秒，照顾 FAT / 网络盘的粗粒度时间戳。`copy2` 会保留 mtime，所以正常拷贝后判等是精确的。
- 目录级：`copied_folder_targets` 以 `(源, 目标)` 为键，避免同一个目录被整棵 `os.walk` 走第二遍。
- 序列：逐帧仍走 `copy_item()`，落盘用的是**真实帧文件名**（`os.path.basename(match)`）；`sequence_basename()` 只决定**回写的参数值**。所以「静态引用某一帧 + 序列引用同一帧」不会产生两份文件，只是同一个目标被复用。

**计数三个口径别混：**

| 字段 | 含义 |
|---|---|
| `summary.resources_copied` | 真正执行了 `copy2` 的**文件数** |
| `summary.references_reused` | 命中已有副本的**引用数**（省掉的重复拷贝） |
| `summary.duplicate_archive` | 同一物理文件被归档到 ≥2 个不同位置的**组数**；明细在顶层 `duplicates[]`（`{source, targets}`），`archive_log.txt` 里也单列一节「重复归档」 |

**为什么还要报重复：** 每个参数都改写成自己的 `$HIP` 路径，两份路径都得存在，不能悄悄省掉一份——所以只报告不合并。真实会撞上的场景：一个无类别目录参数（整目录进 `external/`）+ 同一目录里的文件被 File 节点单独引用（进 `textures/`），同一份贴图各存一份。判定用的是 `norm(realpath())`，junction / 符号链接能被压成同一源；**盘符映射（`Z:` vs `D:`）压不掉**，仍会被当成两个源。

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

### 2026-11-23 · 启动器改为纯 ASCII + 挑对解释器

**现象：** 双击 `launch_gui.cmd` 后窗口里一片乱码，严重时变量也不展开（`echo ...%PYTHON_EXE%` 打出字面量）。

**根因：** 批处理文件原本是 **UTF-8 无 BOM**，而 cmd 是**按当前代码页逐字节**读文件的。在 cp936 下，全角冒号 `：`(EF BC 9A) 的尾字节 0x9A 是 GBK 前导字节，会把紧跟的 `%` 吞掉，变量语法直接失效。

**两次尝试都失败，别重走：**
1. 首行加 `chcp 65001`：cp65001 下 cmd 解析批处理会行串位，把 `REM ...直接报 ImportError` 当命令执行，报 `can't open file 'C:\BatchArchive\直接报'`。
2. 把文件转成 **GBK(cp936)**：在 cp936 控制台完美，但换到 **cp65001**（Windows Terminal / VS Code 集成终端常见）又变回乱码。同一份中文文本不可能同时满足两种代码页。

**结论（必须遵守）：**
- `launch_gui.cmd` 保持 **纯 ASCII + CRLF**，里面不写一个中文字、不写 `chcp`。中文说明只放 `README.md` / `README.txt`（在编辑器里看，不受代码页影响）。
- 代码页 936 与 65001 下实测输出一致。
- **解释器枚举用 `where python`，不要用 `py -3 -c "print(sys.executable)"`。** 后者含 `print(...)` 的小括号，一旦放进 `for /f` 且 `for` 又位于 `if ( )` 块内，内层 `()` 会把块提前闭合，整条探测**静默返回空**——现象就是所有候选都探不到、`PYTHON_EXE` 为空、最终落到兜底解释器并误报 `PyQt5 is missing, installing...`。这是 2026-11-23 第二次修 bug 的真正原因。
- 选择链：`where python` 全量 → `%LocalAppData%\Programs\Python\Python31x`（新→旧）→ `C:\Python31x`；挑第一个能 `import PyQt5` 的，都不行才用第一个找到的兜底并自动 `pip install PyQt5`，装完**再验一次** `import PyQt5` 才启动 app。
- 本机 PATH 上的 `python` 是 WorkBuddy 的 3.13，没有 PyQt5；只有 `Python311` 装了。直接 `python app.py` 会 `ModuleNotFoundError`。
- 失败时统一走 `:fail`，打印 script / cwd / python / version 再 `pause`，避免窗口一闪而过什么都看不到。

### 2026-11-23 · 打包前自动识别 HIP 保存版本

**为什么：** 原来 `find_hython()` 直接取最高版本，等于强制用最新 Houdini 打开所有工程；跨大版本加载会静默改参数默认值，产出"看起来成功、其实已被升级"的包。

**怎么读版本（`hip_version.read_version`）：** HIP 是 cpio(odc) 归档，`.variables` 成员里有 `set -g _HIP_SAVEVERSION = '21.0.596'`，位于文件前 ~1KB；读头部 1MiB 即可，实测 11MB 工程 0.5ms。缺失时退回节点 userdata 的 `___Version___\x00\x00\x00\x03\x00\x08<ver>`。

**判据禁区：** 不要用 `$HOME/houdini21.0/poselib`、`HOUDIN~1.596/...` 这类字符串——那是保存机器的路径残留，换机器/换 `$HOME` 就失真。测试里专门有一条用例守着（`test_ignores_version_lookalikes_in_paths`）。

**匹配策略（用户选的方案 2：警告 + 用最接近的更高版本继续）：** 完全一致 → 同 `主.次` 的其它 build → 更高的最接近版本 → 只能用更低版本 → 读不出用最新。除"完全一致"外都会在开工前弹一次确认框，并在日志里逐条写明。

**落到哪里：**
- GUI：队列项显示 `[H 21.0.596]`；下拉框首项是 `自动匹配`（不带副标题，保持简短），手选/手填即视为强制统一版本。
- `ArchiveWorker` 新增 `hython_plan` / `expect_versions` 两个按任务对齐的列表，逐条选解释器并向子进程传 `--expect-version`。
- `package()` 在 `hipFile.load` 后读 `hou.applicationVersionString()`，写进清单 `houdini: {saved_with, opened_with, version_match, minor_match}`，并在 `archive_log.txt` 输出「Houdini：保存 X / 打开 Y」。
- **版本不一致不算失败**：`is_valid_success` 不看这个字段，只做记录与提示。

### 2026-11-26 · 复制去重口径修正

**问题：** 原先「已存在且大小相同」就算复制过，且 `copy_item()` 无论如何 `return 1`，导致两个错觉：几个节点共用一个缓存时日志写「复制 N」而磁盘上只有 1 个文件；反过来，重试复用的归档目录里，源缓存被重写成**同样大小**时会被判成已存在，包内留下旧数据。

**改法：**
- `same_archived_file()` 改为 大小 + mtime 双判据（容差 2s，兼容 FAT / 网络盘）；`copy_item()` 返回 `(新写入数, 复用数)`，目录递归逐层累加。
- `summary.resources_copied` 只统计真实写入，新增 `summary.references_reused`；`archive_log.txt` 的资源行改成「复制 X，复用 Y」，GUI 的 `done` 日志行同步显示。
- 新增 `archived_sources`（键为 `norm(realpath(src))`）与顶层 `duplicates[]`：同一源文件落到两个归档位置时报出来，进日志「重复归档」一节。不自动合并——两份路径都被参数引用着，删任一份都会断链。

**测试：** `tests/test_package_houdini.py::SharedReferenceTests` 四条——两节点共享一个缓存只写一次、目录引用与文件引用导致的重复归档被报出、同大小同 mtime 记为复用、同大小但 mtime 变化必须重拷。

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
