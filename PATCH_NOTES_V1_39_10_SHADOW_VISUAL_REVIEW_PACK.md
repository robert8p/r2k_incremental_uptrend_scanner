# v1.39.10 — Shadow visual review pack

## Goal
Automate the pre-promotion chart-window sanity check for the current best shadow profile without changing live classifier behavior.

## What changed
- Added `/diagnostics/shadow-visual-review-pack.zip`
- Added a Diagnostics page download button for the shadow visual review pack
- Added `app/services/shadow_visual_review_pack.py`
- The new pack:
  - reads the current best profile from the shadow promotion pack
  - selects the names that profile would currently admit
  - renders chart-window SVGs for those names
  - computes an automated visual verdict from the post-scan path shape and outcome context
  - emits an HTML index plus CSV/JSON artifacts

## What stayed frozen
- No live threshold changes
- No stage-1 redesign
- No new scoring features
- No new queue/tier machinery
- No deployment-model changes

## Key output files
- `shadow_visual_review_summary.json`
- `shadow_visual_review_rows.csv`
- `shadow_visual_review_intraday_bars.csv`
- `shadow_visual_review.html`
- `charts/*.svg`

## Validation
- Full suite: `110 passed, 1 skipped`
