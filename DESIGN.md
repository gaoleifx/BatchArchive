# BatchArchive Design System

## Stack
- framework: Python PyQt5 desktop app
- styling: Qt stylesheet with dark palette
- components: drag-drop queue, left work area, collapsible right filter panel, progress log, result summary
- animation: none; packaging runs off the UI thread

## Tokens
- brand: #4F8CFF
- background: #111827
- surface: #1F2937
- text: #F3F4F6
- muted: #9CA3AF

## Decisions
- 2026-08-24 — Tkinter chosen so the tool runs without third-party packages.
- 2026-08-24 — packaging runs in a worker Houdini process and never edits source HIP.
- 2026-08-24 — Houdini selector lists all detected installations and synchronizes the hython path.
- 2026-08-24 — added two-level resource and node filtering in a collapsible right panel.
- 2026-09-11 — packaging action state: the disabled start button reads “打包中” while the stop button takes the brand-blue primary treatment; both return to idle styling when the worker finishes.
- 2026-10-23 — a file reference pointing at the project folder or one of its ancestors is recorded and skipped instead of copied, so a `$HIP` style path parameter cannot sweep every other scene into the package.
- 2026-10-23 — external Alembic packaging copies only the referenced file and rebuilds its folder on demand; folder parameters are rewritten to the rebuilt folder instead of copying their contents.
- 2026-10-23 — script and config files (json, py, txt, log, md, xml, yaml, csv, ini, bat, sh) are skipped by default, both as direct references and inside copied folders; the switch and `--include-auxiliary-files` keep them.

## Components
- Archive window
- Path selectors
- Latest HIP scanner
- Progress log
- Result summary
- Packaging action button states (`app.py`)
