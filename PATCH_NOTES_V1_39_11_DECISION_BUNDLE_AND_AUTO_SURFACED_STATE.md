# v1.39.11 — Decision bundle and auto-surfaced decision state

This tranche adds three weekend-efficiency improvements without changing live thresholds:

1. historical shadow backfill over a wider past-session window
2. a single post-close decision bundle that collapses the multi-pack workflow
3. auto-surfaced current decision state in `/status`, the homepage, and `/diagnostics`

It also adds a scheduler-side post-close cache refresh so the latest decision bundle can be generated automatically after market close once scans and pending live outcome evaluations are available.
