BatchArchive · 浆果文化 —— Houdini 工程打包工具

双击 launch_gui.cmd 运行。启动器会自动查找本机 Python 3，检测到缺少 PyQt5 时会尝试自动安装。

使用方法：
1. 将一个或多个 .hip / .hiplc / .hipnc 拖进窗口，也可以点击“添加文件”。
2. 确认 Houdini hython.exe 路径（自动枚举已装版本，也可手动填写）。
3. 点击“开始打包”。按列表顺序依次处理，状态显示在每个文件后面。

默认输出到每个 HIP 同级的 archive 文件夹；填写“输出目录”则全部集中到该目录。
工具不修改原始 HIP，路径改写为 $HIP/...，目标已存在时自动加 _02、_03 后缀。

过滤默认全开：跳过缓存输出、渲染输出、脚本与配置文件；外部输入资源不受影响。
渲染器节点（Mantra / Karma / Arnold / Redshift / RenderMan / USD Render / Render Gallery）始终不参与打包，没有开关。

完整说明（安装、过滤规则、归档目录结构、命令行用法、manifest 校验与重试策略）见同目录 README.md。
