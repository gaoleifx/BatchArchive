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

## Components
- Archive window
- Path selectors
- Latest HIP scanner
- Progress log
- Result summary
