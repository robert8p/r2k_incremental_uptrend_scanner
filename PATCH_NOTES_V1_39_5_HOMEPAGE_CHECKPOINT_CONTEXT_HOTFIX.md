# v1.39.5 — Homepage checkpoint context hotfix

## Purpose
Fix homepage rendering crash introduced by v1.39.4 when `index.html` expected `checkpoint_review` but the index page context did not include it.

## Changes
- Added `checkpoint_review` to `build_index_page_context()` using the latest scan trading day and `[120, 150]` offsets.
- Added a defensive Jinja guard on the homepage before reading `checkpoint_review.summary.selected_day`.
- Added regression test covering index page context checkpoint data.
- Bumped app version to `1.39.5`.

## Files changed
- `app/page_contexts.py`
- `app/templates/index.html`
- `app/version.py`
- `tests/test_page_contexts.py`
- `PATCH_NOTES_V1_39_5_HOMEPAGE_CHECKPOINT_CONTEXT_HOTFIX.md`
