# v1.39.12 — Diagnostics layout polish and text snapshot exports

## What changed
- reorganized the diagnostics action area into grouped, responsive grids so buttons no longer bunch together or overflow awkwardly
- added direct `.txt` snapshot downloads for the config and universe fields
- improved diagnostics snapshot card headers and JSON panes for easier reading

## Why
- the diagnostics page had become visually cramped and harder to use as the number of bundles grew
- the user wanted the UI layout fixed and wanted one-click `.txt` downloads for config and universe snapshots

## Validation
- added route tests for the diagnostics page and the new text snapshot download endpoints
