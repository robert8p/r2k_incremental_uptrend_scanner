# v1.39.14 — historical replay primary evidence tranche

## What changed
- Added `/diagnostics/historical-replay-shadow-pack.zip`.
- Added historical replay shadow backfill under current clean logic using replay at 120/150 checkpoints.
- Decision bundle and goal-alignment readout now treat historical replay as the primary evidence engine when available, with live clean days acting as the release gate.
- Auto-surfaced replay evidence state on homepage and diagnostics.
- Added optional `evaluate_non_advanced_rows=True` replay evaluation in validation so historical replay can judge tradeability of baseline-rejected rows in shadow mode.

## What did not change
- No live threshold changes.
- No stage-1 redesign.
- No new scoring features or queue/tier expansion.
