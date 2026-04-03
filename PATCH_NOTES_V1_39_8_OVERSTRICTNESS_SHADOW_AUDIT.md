# v1.39.8 — Overstrictness tracker and shadow threshold audit

This tranche adds a new diagnostics endpoint, `/diagnostics/overstrictness-shadow-pack.zip`, to accumulate automated adjudication evidence across clean sessions and run narrow shadow threshold profiles without changing live behavior.

## Included
- Clean-session overstrictness tracker across recent 120/150 sessions
- Predicate-fail rollup for `possible_classifier_overstrict` vs `classifier_correct_reject`
- Shadow threshold profiles for the main structural predicates
- Intraday bar export for audited rows

## Not included
- No live threshold changes
- No stage-1 redesign
- No new scoring or queue logic
- No presentation-first product changes
