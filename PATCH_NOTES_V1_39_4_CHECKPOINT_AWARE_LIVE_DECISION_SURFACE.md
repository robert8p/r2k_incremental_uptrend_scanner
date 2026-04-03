# v1.39.4 — checkpoint-aware live decision surface

## Purpose

Preserve earlier-valid stage-2 candidates on the product surface instead of letting them disappear when a later checkpoint becomes empty.

## Changes

- Added checkpoint-aware decision-surface service and diagnostics pack.
- Added `/diagnostics/checkpoint-decision-pack.zip`.
- Added homepage checkpoint-aware section showing:
  - unique symbols advanced at any checkpoint
  - currently valid now
  - earlier-valid names that later regressed
- Added scan-detail checkpoint-aware section for the selected trading day.
- Kept scoring, stage-1 selection, and deployment semantics frozen.
