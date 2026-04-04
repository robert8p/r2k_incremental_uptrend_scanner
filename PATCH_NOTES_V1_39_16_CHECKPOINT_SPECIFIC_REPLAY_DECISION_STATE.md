# v1.39.16 — Checkpoint-specific replay decision state

## What changed
- decision bundle now ingests the replay bottleneck summary alongside the replay pack
- machine recommendation becomes checkpoint-specific when replay clears support at one checkpoint but not another
- goal-alignment output now surfaces the supported checkpoint split directly
- decision bundle pack now includes `historical_replay_bottleneck_summary.json`

## Why
The replay bottleneck pack showed that the recommended replay profile (`soft_cycle_durability`) is supported at 120 minutes but not at 150 minutes. The old decision state flattened that into a generic `no clear candidate` outcome. This tranche makes the machine state honest about that split without changing live behavior or thresholds.

## Intended effect
- reduce false confidence from blended replay verdicts
- make the next code decision checkpoint-specific rather than profile-generic
- keep live thresholds frozen while surfacing the narrowest evidence-backed next move

## What this does not do
- no live threshold changes
- no stage-1 redesign
- no new scoring features
- no deployment model changes
