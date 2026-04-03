# v1.39.13 — Goal alignment readout

## What changed
- Added a machine-generated **Goal alignment readout** to the homepage and diagnostics page.
- Added a downloadable plain-text snapshot at `/diagnostics/goal-alignment.txt`.
- Added goal-alignment files to the decision bundle:
  - `goal_alignment_summary.json`
  - `goal_alignment.txt`
- Included goal-alignment state in `/status` through the diagnostics snapshot.

## Why
This tranche does not change live scoring or thresholds. It reduces drift and manual interpretation by making the app state explain:
- what matters now
- what is frozen
- what would justify change

## Validation
- Full test suite: **122 passed, 1 skipped**
