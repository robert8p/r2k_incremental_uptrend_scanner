# Patch notes v1.39.15 — Replay bottleneck pack

## Why this tranche exists

The first replay-primary evidence checkpoint did not justify a live change, but it did expose a sharper question: the recommended replay profile (`soft_cycle_durability`) is not uniformly weak. It looks materially better at the earlier checkpoint than at the later one, and its misses split into two different failure paths:

- entry never touched
- entry touched but target never hit

That means the best next tranche is not more scoring complexity and not a live-threshold change. It is a narrow bottleneck-isolation tranche that shows **where** the recommended replay profile breaks down so the next change can be surgical.

## What changed

- Added a new diagnostics pack route:
  - `/diagnostics/replay-bottleneck-pack.zip`
- Added `app/services/replay_bottleneck_pack.py`
- The new pack reads the cached historical replay artifact instead of re-running a new analysis path, so it stays aligned with the current replay-primary evidence engine.
- The pack isolates the replay-recommended profile and exports:
  - offset rollup
  - failure-path rollup
  - rank-bucket rollup
  - daily offset rollup
  - metric rollup by failure path
  - admitted rows for the recommended profile
- Added diagnostics UI links for the new pack.

## Why this is the right next step

This tranche improves decision quality without changing live behaviour. It helps answer whether the next real code change should focus on:

- checkpoint-specific decay (for example 120 vs 150 minutes)
- entry-miss behaviour
- post-entry target-failure behaviour

## What this patch deliberately does not do

- no live threshold changes
- no stage-1 redesign
- no new scoring features
- no new queue/tier/penalty machinery
- no deployment model changes
