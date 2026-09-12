import json
import math
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from archive_policy import is_valid_success, should_retry


APP_DIR = Path(__file__).resolve().parent
PACKAGE_SCRIPT = APP_DIR / "package_houdini.py"
HIP_EXTS = (".hip", ".hiplc", ".hipnc")


def find_hython_versions():
    root = Path(r"C:\Program Files\Side Effects Software")
    paths = list(root.glob("Houdini */bin/hython.exe")) if root.exists() else []
    paths = [p for p in paths if p.is_file()]
    paths = sorted(paths, key=lambda p: p.parent.parent.name, reverse=True)
    return [(p.parent.parent.name.replace("Houdini ", ""), str(p)) for p in paths]


def find_hython():
    versions = find_hython_versions()
    return versions[0][1] if versions else ""


class DropList(QtWidgets.QListWidget):
    pathsChanged = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            self.add_path(url.toLocalFile())
        event.acceptProposedAction()
        self.pathsChanged.emit()

    def add_path(self, path):
        path = os.path.abspath(path)
        if os.path.isfile(path) and path.lower().endswith(HIP_EXTS):
            known_paths = {os.path.normcase(existing) for existing in self.paths()}
            if os.path.normcase(path) not in known_paths:
                item = QtWidgets.QListWidgetItem(path)
                item.setData(QtCore.Qt.UserRole, path)
                self.addItem(item)

    def paths(self):
        return [self.item(i).data(QtCore.Qt.UserRole) or self.item(i).text().rsplit("    [", 1)[0]
                for i in range(self.count())]

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() != 0:
            return
        painter = QtGui.QPainter(self.viewport())
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        center = self.viewport().rect().center()
        icon_y = center.y() - 48
        pen = QtGui.QPen(QtGui.QColor("#1677FF"), 3, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(center.x(), icon_y + 30, center.x(), icon_y)
        painter.drawLine(center.x(), icon_y, center.x() - 11, icon_y + 11)
        painter.drawLine(center.x(), icon_y, center.x() + 11, icon_y + 11)
        painter.drawLine(center.x() - 21, icon_y + 31, center.x() - 21, icon_y + 43)
        painter.drawLine(center.x() - 21, icon_y + 43, center.x() + 21, icon_y + 43)
        painter.drawLine(center.x() + 21, icon_y + 43, center.x() + 21, icon_y + 31)
        painter.setPen(QtGui.QColor("#D0D7DE"))
        painter.setFont(QtGui.QFont("Microsoft YaHei UI", 11, QtGui.QFont.DemiBold))
        text_rect = QtCore.QRect(0, center.y() + 6, self.viewport().width(), 28)
        painter.drawText(text_rect, QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop, "将 .hip / .hiplc / .hipnc 文件拖拽到此处")
        painter.setPen(QtGui.QColor("#8B949E"))
        painter.setFont(QtGui.QFont("Microsoft YaHei UI", 9))
        sub_rect = QtCore.QRect(0, center.y() + 34, self.viewport().width(), 24)
        painter.drawText(sub_rect, QtCore.Qt.AlignHCenter | QtCore.Qt.AlignTop, "支持拖拽添加，按顺序处理")


class SwitchCheckBox(QtWidgets.QCheckBox):
    """Compact on/off switch used for the resource category filters."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setObjectName("switchCheckBox")

    def sizeHint(self):
        base = super().sizeHint()
        return QtCore.QSize(58 + base.width(), max(28, base.height()))

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        switch_rect = QtCore.QRect(0, 2, 52, 24)
        checked = self.isChecked()
        track_color = QtGui.QColor("#1E5C3C" if checked else "#6B2028")
        if not self.isEnabled():
            track_color = QtGui.QColor("#343B45")
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(switch_rect, 12, 12)

        knob_x = switch_rect.right() - 21 if checked else switch_rect.left() + 3
        painter.setBrush(QtGui.QColor("#F7FAFC"))
        painter.drawEllipse(QtCore.QRect(knob_x, 5, 18, 18))

        painter.setPen(QtGui.QColor("#FFFFFF"))
        painter.setFont(QtGui.QFont("Microsoft YaHei UI", 8, QtGui.QFont.DemiBold))
        label_rect = QtCore.QRect(5 if checked else 25, 2, 22, 24)
        painter.drawText(label_rect, QtCore.Qt.AlignCenter, "on" if checked else "off")

        text_color = self.palette().color(QtGui.QPalette.WindowText)
        if not self.isEnabled():
            text_color = QtGui.QColor("#687482")
        painter.setPen(text_color)
        painter.setFont(self.font())
        painter.drawText(QtCore.QRect(62, 0, max(0, self.width() - 62), 28), QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, self.text())


class BrandLoader(QtWidgets.QWidget):
    """Small, low-cost animated sphere that gives the brand row a living cue."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(52, 52)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.setToolTip("BatchArchive 已就绪")
        self.phase = 0.0
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._advance)
        self.timer.start(55)

    def _advance(self):
        self.phase = (self.phase + 0.075) % (2 * 3.141592653589793)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        center = QtCore.QPointF(self.width() / 2, self.height() / 2)

        glow = QtGui.QRadialGradient(center, 25)
        glow.setColorAt(0.0, QtGui.QColor(76, 146, 255, 90))
        glow.setColorAt(0.48, QtGui.QColor(112, 80, 255, 38))
        glow.setColorAt(1.0, QtGui.QColor(112, 80, 255, 0))
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QtCore.QRectF(1, 1, 50, 50))

        sphere = QtGui.QRadialGradient(QtCore.QPointF(19, 17), 27)
        sphere.setColorAt(0.0, QtGui.QColor("#DCEBFF"))
        sphere.setColorAt(0.28, QtGui.QColor("#6EA8FF"))
        sphere.setColorAt(0.72, QtGui.QColor("#4161D8"))
        sphere.setColorAt(1.0, QtGui.QColor("#282D86"))
        painter.setBrush(sphere)
        painter.drawEllipse(QtCore.QRectF(10, 10, 32, 32))

        for index, radius in enumerate((4.0, 2.7, 2.0)):
            angle = self.phase * (1.0 + index * 0.22) + index * 2.1
            x = 26 + 9.0 * math.cos(angle)
            y = 26 + 8.0 * math.sin(angle)
            painter.setBrush(QtGui.QColor(220, 235, 255, 180 - index * 28))
            painter.drawEllipse(QtCore.QRectF(x - radius, y - radius, radius * 2, radius * 2))


class ArchiveWorker(threading.Thread):
    MAX_ATTEMPTS = 3

    def __init__(self, tasks, hython, output_mode, skip_cache_outputs, skip_render_outputs, categories, filter_red_nodes, filter_external_nodes, events):
        super().__init__(daemon=True)
        self.tasks = tasks
        self.hython = hython
        self.output_mode = output_mode
        self.skip_cache_outputs = skip_cache_outputs
        self.skip_render_outputs = skip_render_outputs
        self.categories = categories
        self.filter_red_nodes = filter_red_nodes
        self.filter_external_nodes = filter_external_nodes
        self.events = events
        self.stop_requested = False
        self.process = None

    def stop(self):
        self.stop_requested = True
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def run(self):
        total = len(self.tasks)
        for index, hip in enumerate(self.tasks, 1):
            if self.stop_requested:
                break
            hip_path = Path(hip)
            archive = hip_path.parent / "archive"
            if self.output_mode:
                archive = Path(self.output_mode)
            archive.mkdir(parents=True, exist_ok=True)
            package_dir = archive / hip_path.stem
            suffix = 2
            while package_dir.exists():
                package_dir = archive / (hip_path.stem + "_%02d" % suffix)
                suffix += 1
            manifest_path = package_dir / "package_manifest.json"
            self.events.put(("item", index - 1, "处理中"))
            self.events.put(("log", f"[{index}/{total}] 开始：{hip}"))
            cmd = [self.hython, "-u", str(PACKAGE_SCRIPT), str(hip_path), str(archive), "--package-dir", str(package_dir)]
            if not self.skip_cache_outputs:
                cmd.append("--include-cache-outputs")
            if not self.skip_render_outputs:
                cmd.append("--include-render-outputs")
            cmd.extend(["--categories", ",".join(sorted(self.categories))])
            if self.filter_red_nodes:
                cmd.append("--filter-red-nodes")
            if self.filter_external_nodes:
                cmd.append("--filter-external")
            code = -1
            manifest = None
            for attempt in range(1, self.MAX_ATTEMPTS + 1):
                if self.stop_requested:
                    break
                if attempt > 1:
                    self.events.put(("log", f"第 {attempt}/{self.MAX_ATTEMPTS} 次重试：{hip}"))
                # Never accept a manifest left by an earlier attempt. Removing
                # these tiny status files does not touch already copied assets.
                try:
                    manifest_path.unlink(missing_ok=True)
                    (package_dir / "archive_log.txt").unlink(missing_ok=True)
                except Exception as exc:
                    self.events.put(("log", f"无法清理旧结果文件：{exc}"))
                    code = -1
                    manifest = None
                    if attempt < self.MAX_ATTEMPTS:
                        self.events.put(("log", f"第 {attempt} 次无法建立安全校验状态，准备重试。"))
                        continue
                    self.events.put(("log", f"已达到最多 {self.MAX_ATTEMPTS} 次尝试：无法安全更新校验文件。"))
                    break
                try:
                    env = os.environ.copy()
                    env["PYTHONUNBUFFERED"] = "1"
                    self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, universal_newlines=True, env=env, cwd=str(hip_path.parent))
                    self.events.put(("log", f"Houdini 子进程已启动，PID={self.process.pid}"))
                    for line in self.process.stdout:
                        line = line.rstrip()
                        try:
                            event = json.loads(line)
                            if event.get("event") == "status":
                                self.events.put(("log", event.get("message", "")))
                            elif event.get("event") == "done":
                                self.events.put(("log", f"JSON 校验：{event.get('status', 'unknown')} | 失败 {event.get('failures', 0)} | 资源 {event.get('resources_copied', 0)} | 改写 {event.get('parameters_rewritten', 0)}"))
                        except Exception:
                            if line:
                                self.events.put(("log", line))
                    code = self.process.wait()
                except Exception as exc:
                    code = -1
                    self.events.put(("log", "启动失败：" + str(exc)))
                finally:
                    if self.process and self.process.stdout:
                        self.process.stdout.close()
                    self.process = None

                try:
                    with manifest_path.open("r", encoding="utf-8") as manifest_file:
                        manifest = json.load(manifest_file)
                except Exception as exc:
                    manifest = None
                    self.events.put(("log", f"第 {attempt} 次校验失败：JSON 不存在或无法解析（{exc}）"))

                if self.stop_requested:
                    break
                nominal_success = code == 0 and manifest and manifest.get("status") == "success" and not manifest.get("failures")
                if nominal_success and is_valid_success(code, manifest, hip_path, package_dir):
                    break
                if nominal_success:
                    self.events.put(("log", f"第 {attempt} 次校验失败：成功清单与当前任务或归档 HIP 不匹配。"))
                    manifest = None
                failure_count = len(manifest.get("failures", [])) if manifest else 0
                reason = f"{failure_count} 个失败项" if manifest else f"进程退出码 {code}"
                retryable = should_retry(code, manifest)
                if attempt < self.MAX_ATTEMPTS and retryable:
                    self.events.put(("log", f"第 {attempt} 次打包未通过 JSON 校验：{reason}，准备重试。"))
                elif attempt < self.MAX_ATTEMPTS:
                    self.events.put(("log", f"第 {attempt} 次打包发现确定性错误：{reason}，停止无效重试。"))
                    break
                else:
                    self.events.put(("log", f"已达到最多 {self.MAX_ATTEMPTS} 次尝试：{reason}"))
            if self.stop_requested:
                self.events.put(("item", index - 1, "已停止"))
                break
            success = is_valid_success(code, manifest, hip_path, package_dir)
            state = "完成" if success else "失败"
            self.events.put(("item", index - 1, state))
            self.events.put(("progress", int(index * 100 / total)))
        self.events.put(("finished", not self.stop_requested))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("浆果文化 · BatchArchive")
        self.resize(1280, 820)
        self.setMinimumSize(980, 680)
        self.events = queue.Queue()
        self.worker = None
        self.hython_versions = find_hython_versions()
        self.hython_combo = QtWidgets.QComboBox()
        self.hython_combo.setMinimumWidth(150)
        for version, path in self.hython_versions:
            self.hython_combo.addItem("Houdini " + version, path)
        if not self.hython_versions:
            self.hython_combo.addItem("未检测到 Houdini")
        self.hython_combo.currentIndexChanged.connect(self._on_hython_version_changed)
        self.hython_edit = QtWidgets.QLineEdit(find_hython())
        self.hython_edit.setPlaceholderText("也可以手动指定 hython.exe")
        self.output_edit = QtWidgets.QLineEdit()
        self.skip_cache_cb = SwitchCheckBox("跳过工程内部缓存输出（bgeo / sim / vdb 等）")
        self.skip_render_cb = SwitchCheckBox("跳过工程内部渲染输出（exr / AOV 等）")
        self.skip_cache_cb.setChecked(True)
        self.skip_render_cb.setChecked(True)
        self.category_names = ["Image/Textures", "Geometry", "Alembics", "USDs", "HDAs"]
        self.category_cbs = {name: SwitchCheckBox(name) for name in self.category_names}
        for checkbox in self.category_cbs.values():
            checkbox.setChecked(True)
        self.red_node_cb = SwitchCheckBox("只打包节点颜色是红色的节点")
        self.external_node_cb = SwitchCheckBox("只打包引用外部资源的节点")
        self.list = DropList()
        self.list.setObjectName("dropList")
        self.progress = QtWidgets.QProgressBar()
        self.log = QtWidgets.QPlainTextEdit()
        self.start_btn = QtWidgets.QPushButton("开始依次打包")
        self.start_btn.setObjectName("primaryButton")
        self.stop_btn = QtWidgets.QPushButton("停止")
        self.stop_btn.setObjectName("stopButton")
        self.stop_btn.setProperty("packagingActive", False)
        self._build_ui()
        self._style()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(150)

    def _build_ui(self):
        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central); root.setContentsMargins(24, 22, 24, 20); root.setSpacing(12)
        brand_row = QtWidgets.QHBoxLayout(); brand_row.setSpacing(14)
        brand = QtWidgets.QLabel("浆果文化"); brand.setObjectName("brand"); brand_row.addWidget(brand)
        brand_row.addWidget(BrandLoader())
        divider = QtWidgets.QLabel("|"); divider.setObjectName("brandDivider"); brand_row.addWidget(divider)
        product = QtWidgets.QLabel("BatchArchive"); product.setObjectName("product"); brand_row.addWidget(product)
        status = QtWidgets.QLabel("● 就绪"); status.setObjectName("status")
        brand_row.addStretch(1); brand_row.addWidget(status); root.addLayout(brand_row)
        sub = QtWidgets.QLabel("把 HIP 文件拖进窗口，按列表顺序归档到各自工程目录的 archive 文件夹")
        sub.setObjectName("muted"); root.addWidget(sub)
        top_actions = QtWidgets.QHBoxLayout(); top_actions.addStretch(1)
        self.global_filter_btn = QtWidgets.QPushButton("隐藏过滤规则")
        self.global_filter_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_ArrowDown))
        self.global_filter_btn.clicked.connect(self._toggle_filter_panel)
        top_actions.addWidget(self.global_filter_btn); root.addLayout(top_actions)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        left = QtWidgets.QWidget(); left_layout = QtWidgets.QVBoxLayout(left); left_layout.setContentsMargins(0, 0, 10, 0); left_layout.setSpacing(10)

        form = QtWidgets.QFormLayout(); form.setSpacing(8)
        hython_row = QtWidgets.QHBoxLayout(); hython_row.addWidget(self.hython_combo); hython_row.addWidget(self.hython_edit)
        auto = QtWidgets.QPushButton("刷新版本"); auto.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_BrowserReload)); auto.clicked.connect(self._refresh_houdini_versions); hython_row.addWidget(auto)
        browse = QtWidgets.QPushButton("浏览"); browse.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogOpenButton)); browse.clicked.connect(self._browse_hython); hython_row.addWidget(browse)
        form.addRow("Houdini hython", hython_row)
        output_row = QtWidgets.QHBoxLayout(); output_row.addWidget(self.output_edit)
        self.output_edit.setPlaceholderText("留空：每个 HIP 使用所在目录\\archive；填写后所有任务共用此输出目录")
        output_btn = QtWidgets.QPushButton("选择"); output_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DirOpenIcon)); output_btn.clicked.connect(self._browse_output); output_row.addWidget(output_btn)
        form.addRow("输出目录", output_row); left_layout.addLayout(form)

        label = QtWidgets.QLabel("HIP 队列（拖放区域）"); label.setObjectName("section"); left_layout.addWidget(label)
        self.list.setMinimumHeight(220); left_layout.addWidget(self.list, 2)
        hint = QtWidgets.QLabel("支持 .hip / .hiplc / .hipnc；列表顺序就是执行顺序，可多选后删除")
        hint.setObjectName("muted"); left_layout.addWidget(hint)
        controls = QtWidgets.QHBoxLayout()
        add_btn = QtWidgets.QPushButton("添加文件"); add_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_FileIcon)); add_btn.clicked.connect(self._add_files)
        remove_btn = QtWidgets.QPushButton("删除选中"); remove_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_TrashIcon)); remove_btn.clicked.connect(self._remove_selected)
        clear_btn = QtWidgets.QPushButton("清空"); clear_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DialogResetButton)); clear_btn.clicked.connect(self._clear_all)
        controls.addWidget(add_btn); controls.addWidget(remove_btn); controls.addWidget(clear_btn); controls.addStretch(1)
        self.start_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaPlay)); self.stop_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_MediaStop))
        self.start_btn.clicked.connect(self._start); self.stop_btn.clicked.connect(self._stop); self.stop_btn.setEnabled(False)
        controls.addWidget(self.start_btn); controls.addWidget(self.stop_btn); left_layout.addLayout(controls)
        left_layout.addWidget(self.progress); left_layout.addWidget(QtWidgets.QLabel("运行日志"))
        self.log.setReadOnly(True); left_layout.addWidget(self.log, 2)

        right = QtWidgets.QFrame(); right.setObjectName("filterPanel"); right_layout = QtWidgets.QVBoxLayout(right); right_layout.setContentsMargins(16, 14, 16, 14); right_layout.setSpacing(10)
        header = QtWidgets.QHBoxLayout(); header_label = QtWidgets.QLabel("打包过滤规则"); header_label.setObjectName("section"); header.addWidget(header_label); header.addStretch(1)
        self.collapse_btn = QtWidgets.QPushButton("折叠"); self.collapse_btn.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_ArrowUp)); self.collapse_btn.clicked.connect(self._toggle_filter_panel); header.addWidget(self.collapse_btn); right_layout.addLayout(header)
        group_one = QtWidgets.QGroupBox("规则一 · 资源大类"); one_layout = QtWidgets.QVBoxLayout(group_one)
        for checkbox in self.category_cbs.values(): one_layout.addWidget(checkbox)
        right_layout.addWidget(group_one)
        group_two = QtWidgets.QGroupBox("规则二 · 节点细分（AND）"); two_layout = QtWidgets.QVBoxLayout(group_two)
        two_layout.addWidget(self.red_node_cb); two_layout.addWidget(self.external_node_cb)
        rule_hint = QtWidgets.QLabel("不勾选时不增加节点限制；同时勾选时必须同时满足。")
        rule_hint.setObjectName("muted"); rule_hint.setWordWrap(True); two_layout.addWidget(rule_hint); right_layout.addWidget(group_two)
        group_output = QtWidgets.QGroupBox("输出过滤"); output_layout = QtWidgets.QVBoxLayout(group_output)
        output_layout.addWidget(self.skip_cache_cb); output_layout.addWidget(self.skip_render_cb); right_layout.addWidget(group_output)
        panel_hint = QtWidgets.QLabel("未分类资源保留原有打包行为。内部路径使用标准目录，外部资源进入 external。")
        panel_hint.setObjectName("muted"); panel_hint.setWordWrap(True); right_layout.addWidget(panel_hint); right_layout.addStretch(1)

        splitter.addWidget(left); splitter.addWidget(right); splitter.setSizes([820, 340]); self.filter_panel = right; self.filter_expanded_width = 340
        root.addWidget(splitter, 1)

    def _toggle_filter_panel(self):
        if self.filter_panel.isVisible():
            self.filter_expanded_width = max(260, self.filter_panel.width())
            self.filter_panel.setVisible(False)
            self.collapse_btn.setText("展开规则")
            self.global_filter_btn.setText("显示过滤规则")
        else:
            self.filter_panel.setVisible(True)
            self.filter_panel.setMinimumWidth(260)
            self.collapse_btn.setText("折叠")
            self.global_filter_btn.setText("隐藏过滤规则")

    def _style(self):
        self.setStyleSheet("""
        QMainWindow, QWidget { background:#0D1117; color:#F0F3F6; font-family:'Microsoft YaHei UI'; font-size:10pt; }
        QLabel { background:transparent; }
        QLabel#brand { font-size:26pt; font-weight:700; color:#F5F7FA; }
        QLabel#brandDivider { font-size:22pt; color:#3D4652; }
        QLabel#product { font-size:17pt; font-weight:500; color:#D8DEE6; }
        QLabel#status { background:transparent; border:none; padding:0; color:#52D273; font-weight:600; }
        QLabel#section { font-size:12pt; font-weight:600; color:#E6EDF3; }
        QLabel#muted { color:#8B949E; }
        QFormLayout QLabel { color:#C9D1D9; font-weight:600; }
        QFrame#filterPanel { background:#151B23; border:1px solid #303944; border-radius:7px; }
        QGroupBox { border:1px solid #303944; border-radius:6px; margin-top:10px; padding-top:10px; color:#E6EDF3; font-weight:600; }
        QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 5px; background:#151B23; }
        QCheckBox { color:#D0D7DE; spacing:7px; padding:4px 2px; }
        QCheckBox:hover { color:#FFFFFF; }
        QCheckBox::indicator { width:14px; height:14px; }
        QLineEdit, QComboBox, QPlainTextEdit { background:#0F141A; border:1px solid #303944; border-radius:6px; padding:7px; color:#E6EDF3; selection-background-color:#1F6FEB; }
        QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border:1px solid #1677FF; }
        QComboBox::drop-down { border:0; width:25px; }
        QComboBox QAbstractItemView { background:#161B22; border:1px solid #303944; color:#E6EDF3; selection-background-color:#1F6FEB; }
        QListWidget#dropList { background:#0F141A; border:1px dashed #3B4652; border-radius:6px; padding:7px; color:#E6EDF3; }
        QListWidget#dropList::item { background:#161B22; color:#E6EDF3; padding:7px; border-bottom:1px solid #242C35; }
        QListWidget#dropList::item:hover { background:#1A2C43; color:#FFFFFF; }
        QListWidget#dropList::item:selected { background:#193B67; color:#FFFFFF; }
        QPushButton { background:#1B222C; border:1px solid #303944; border-radius:6px; padding:8px 13px; color:#E6EDF3; }
        QPushButton:hover { background:#242D38; border-color:#596777; }
        QPushButton:pressed { background:#111820; }
        QPushButton#primaryButton { background:#1677FF; border:1px solid #1677FF; color:#FFFFFF; font-weight:600; padding:9px 16px; }
        QPushButton#primaryButton:hover { background:#4096FF; border-color:#4096FF; }
        QPushButton#primaryButton:pressed { background:#0958D9; border-color:#0958D9; }
        QPushButton#stopButton[packagingActive="true"] { background:#1677FF; border:1px solid #1677FF; color:#FFFFFF; font-weight:600; padding:9px 16px; }
        QPushButton#stopButton[packagingActive="true"]:hover { background:#4096FF; border-color:#4096FF; }
        QPushButton#stopButton[packagingActive="true"]:pressed { background:#0958D9; border-color:#0958D9; }
        QPushButton#primaryButton:focus, QPushButton#stopButton[packagingActive="true"]:focus { border:2px solid #8BB9FF; padding:8px 15px; }
        QPushButton:disabled { color:#687482; background:#151B23; border-color:#2A323C; }
        QPushButton#primaryButton:disabled { color:#7B8794; background:#202833; border-color:#303A46; }
        QProgressBar { background:#0F141A; border:1px solid #303944; border-radius:5px; text-align:center; color:#E6EDF3; height:9px; }
        QProgressBar::chunk { background:#1677FF; border-radius:4px; }
        QSplitter::handle { background:#252D36; width:1px; }
        QScrollBar:vertical { background:transparent; width:16px; margin:0; }
        QScrollBar::handle:vertical { background:#2F74D0; border-radius:4px; min-height:48px; margin:0 5px; }
        QScrollBar::handle:vertical:hover { background:#4D96F5; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:#252C36; border-radius:2px; margin:0 6px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; background:transparent; }
        QScrollBar:horizontal { background:transparent; height:16px; margin:0; }
        QScrollBar::handle:horizontal { background:#2F74D0; border-radius:4px; min-width:48px; margin:5px 0; }
        QScrollBar::handle:horizontal:hover { background:#4D96F5; }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background:#252C36; border-radius:2px; margin:6px 0; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width:0; background:transparent; }
        """)

    def _browse_hython(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择 hython.exe", "", "hython.exe (hython.exe)")
        if p:
            self.hython_edit.setText(p)
            self.hython_combo.setCurrentIndex(-1)

    def _on_hython_version_changed(self, index):
        path = self.hython_combo.itemData(index)
        if path:
            self.hython_edit.setText(path)

    def _refresh_houdini_versions(self):
        current = self.hython_edit.text().strip()
        self.hython_versions = find_hython_versions()
        self.hython_combo.blockSignals(True)
        self.hython_combo.clear()
        for version, path in self.hython_versions:
            self.hython_combo.addItem("Houdini " + version, path)
        if not self.hython_versions:
            self.hython_combo.addItem("未检测到 Houdini")
        self.hython_combo.blockSignals(False)
        selected = self.hython_combo.findData(current)
        if selected >= 0:
            self.hython_combo.setCurrentIndex(selected)
        elif self.hython_versions:
            self.hython_combo.setCurrentIndex(0)
        else:
            self.hython_edit.setText(current)

    def _browse_output(self):
        p = QtWidgets.QFileDialog.getExistingDirectory(self, "选择统一输出目录")
        if p: self.output_edit.setText(p)

    def _add_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加 HIP 文件", "", "Houdini HIP (*.hip *.hiplc *.hipnc)")
        for p in paths: self.list.add_path(p)

    def _remove_selected(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def _clear_all(self):
        self.list.clear()
        self.log.clear()
        if not (self.worker and self.worker.is_alive()):
            self.progress.setValue(0)

    def _set_packaging_ui(self, active):
        self.start_btn.setText("打包中" if active else "开始依次打包")
        self.start_btn.setEnabled(not active)
        self.stop_btn.setProperty("packagingActive", active)
        self.stop_btn.setEnabled(active)
        self.stop_btn.style().unpolish(self.stop_btn)
        self.stop_btn.style().polish(self.stop_btn)
        self.stop_btn.update()

    def _start(self):
        if self.worker and self.worker.is_alive(): return
        hython = self.hython_edit.text().strip()
        tasks = self.list.paths()
        if not os.path.isfile(hython):
            QtWidgets.QMessageBox.warning(self, "缺少 hython", "请指定有效的 hython.exe。")
            return
        if not tasks:
            QtWidgets.QMessageBox.information(self, "没有任务", "请把 HIP 文件拖进窗口。")
            return
        self.progress.setValue(0); self.log.appendPlainText("开始依次打包，共 %d 个 HIP。" % len(tasks))
        for i in range(self.list.count()): self.list.item(i).setText(tasks[i] + "    [等待]")
        self._set_packaging_ui(True)
        categories = {name for name, checkbox in self.category_cbs.items() if checkbox.isChecked()}
        self.worker = ArchiveWorker(tasks, hython, self.output_edit.text().strip(), self.skip_cache_cb.isChecked(), self.skip_render_cb.isChecked(), categories, self.red_node_cb.isChecked(), self.external_node_cb.isChecked(), self.events); self.worker.start()

    def _stop(self):
        if self.worker: self.worker.stop(); self.log.appendPlainText("已请求停止，正在终止当前 HIP。")

    def _poll(self):
        while True:
            try: event = self.events.get_nowait()
            except queue.Empty: break
            kind = event[0]
            if kind == "log":
                self.log.appendPlainText(datetime.now().strftime("%H:%M:%S  ") + event[1])
                self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
            elif kind == "item":
                i, state = event[1], event[2]
                if i < self.list.count():
                    base = self.list.item(i).text().rsplit("    [", 1)[0]
                    self.list.item(i).setText(base + "    [" + state + "]")
            elif kind == "progress": self.progress.setValue(event[1])
            elif kind == "finished":
                self._set_packaging_ui(False); self.worker = None
                self.log.appendPlainText("队列处理完成。")


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setFont(QtGui.QFont("Microsoft YaHei UI", 10))
    win = MainWindow(); win.show()
    sys.exit(app.exec_())
