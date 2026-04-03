# v1.39.7 — Automated outcome adjudication pack

## What changed
- Added `/diagnostics/outcome-adjudication-pack.zip` and diagnostics-page download link.
- Added `app/services/outcome_adjudication_pack.py` to automatically adjudicate post-scan tradeability for earlier advancers and sampled classification-C rejects.
- Outcome adjudication now computes entry touch, configured target hit, intrabar target reach, MFE/MAE, end-of-window return, and verdict buckets.
- Added symbol progression verdicts across 120 -> 150 checkpoints.
- Bumped app version to `1.39.7`.

## Why
This tranche automates the missing judgment layer: whether the names the classifier advanced or rejected were actually tradeable after the checkpoint, without requiring manual chart review.
