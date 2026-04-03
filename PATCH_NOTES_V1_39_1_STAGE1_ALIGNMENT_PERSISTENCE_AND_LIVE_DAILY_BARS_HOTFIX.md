# v1.39.1 — Stage-1 Alignment Persistence and Live Daily-Bars Hotfix

## Purpose

Fix two inspection-confirmed live-path defects that were contaminating the evidence after v1.39.0:

- shortlist-alignment diagnostics were being built but dropped by the typed scan-summary contract before persistence
- the live stage-2 path was refetching daily bars with a zero-width date window instead of using prior-history daily bars, creating a live-vs-replay liquidity mismatch

## What changed

- Added `shortlist_alignment` to the typed `ScanSummary` contract so the field now survives normalization, DB persistence, and API/template reads.
- Updated scan-detail and latest-scan UI surfaces so shortlist-alignment diagnostics are visible after a stored scan.
- Fixed the live stage-2 daily-bars source to reuse the stage-1 preview daily bars when available and otherwise fetch a proper 60-day lookback ending at the session open.
- Added regression tests for:
  - shortlist-alignment persistence through summary normalization
  - stage-2 reuse of stage-1 daily bars
- Bumped app version to `1.39.1`.

## Why this tranche is next

The inherited handoff said not to jump blindly into a deeper population redesign before first inspecting whether v1.39.0 had a wiring or observability failure. Inspection confirmed that it did. This hotfix removes two concrete sources of false evidence before any larger stage-1 redesign decision.
