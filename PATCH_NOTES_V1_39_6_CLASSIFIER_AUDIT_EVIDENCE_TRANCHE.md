# v1.39.6 — Classifier audit evidence tranche

## Purpose
This tranche does **not** add new scoring, new queues, new tiers, or new homepage presentation. It adds a narrow evidence pack so the live range classifier can be audited before further product or logic changes.

## What it adds
- New diagnostics endpoint: `/diagnostics/classifier-audit-pack.zip`
- New Diagnostics-page download link for the classifier audit pack
- New service: `app/services/classifier_audit_pack.py`

## What the pack contains
- `classifier_audit_summary.json`
- `classifier_audit_scan_rollup.csv`
- `classifier_audit_symbols.csv`
- `classifier_audit_metric_snapshots.csv`
- `classifier_audit_metric_deltas.csv`
- `classifier_audit_gate_snapshot.csv`
- `classifier_audit_intraday_bars.csv`
- `report.md`

## Evidence intent
The pack is built to test whether classification-C domination is:
1. mostly correct for Russell 2000 intraday movers, or
2. too strict / too volatile in the adaptive range classifier.

It audits:
- symbols that advanced at the early checkpoint and then regressed later
- a small sample of classification-C rejected names
- predicate/gate status across 120 → 150
- exact intraday bars for audited symbols for manual chart review

## What stays frozen
- No stage-1 redesign
- No threshold retune
- No new scoring features
- No new queue/tier machinery
- No deployment-model changes
