# v1.39.3 — Stage-2 regression audit and honest shortlist surface

This tranche stays narrow and evidence-led.

## What changed
- Added a new diagnostics pack at `/diagnostics/stage2-regression-pack.zip`.
- The regression pack compares the same symbols across the clean early/late offsets (default `120 -> 150`) and records:
  - which symbols advanced earlier but regressed later
  - the exact metric deltas between offsets
  - which thesis-gate / stable-range predicates flipped from pass to fail
  - the latest-day honest shortlist surface breakdown
- Added a decision-surface summary to the scan detail page so the fixed 50-name stage-1 surface is not mistaken for 50 equally actionable ideas.

## What this tranche does not do
- No new scoring features
- No new queue logic
- No new tier logic
- No threshold retune
- No stage-1 redesign
- No deployment-model change

## Why this is next
The clean `v1.39.2` evidence showed that shortlist alignment is retaining a large pool and that the pipeline can produce stage-2 candidates at 120 minutes, but those same names can regress to rejected at 150 minutes under the unstable-non-range thesis gate. This tranche makes that downgrade path inspectable before any more redesign work.
