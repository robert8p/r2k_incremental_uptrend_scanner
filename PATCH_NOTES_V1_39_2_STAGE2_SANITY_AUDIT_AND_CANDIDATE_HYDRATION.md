# v1.39.2 — Stage-2 sanity audit and candidate hydration

## Objective
Tighten the next tranche around clean post-v1.39.1 evidence instead of adding more downstream complexity.

## What changed
- Hydrated candidate-level recommendation fields from stored metrics when older scan-candidate rows do not persist them as top-level columns.
- Added a narrow stage-2 sanity audit pack at `/diagnostics/stage2-sanity-pack.zip`.
- Added latest-scan UI visibility for stage-2 recommendation tier, recommendation book, execution lane, and touch window.
- Added candidate-detail visibility for stage-2 status and recommendation surface.

## Why this matters
- Clean v1.39.1 evidence showed shortlist alignment was working, but the downstream decision surface was still hard to inspect honestly.
- The audit pack now makes it easier to review:
  - whether clean scans are producing real stage-2 names
  - how 120-minute advanced candidates behave by 150 minutes
  - whether the current blocker is a remaining stage-2 thesis gate issue rather than a stage-1 liquidity-prefilter issue

## What this tranche does not do
- No new scoring features
- No new tier logic
- No new queue logic
- No threshold retune
- No deployment-model change
- No broad stage-1 redesign
