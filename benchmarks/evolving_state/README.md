# evolving-state mini-bench

Does foldcrumbs serve the **current** state of the world, or a superseded
one? That is the question StateMemBench (arXiv 2608.19652) poses to memory
systems; their dataset is not public, so this is our own smaller version —
fully deterministic, stdlib only, **no LLM calls**, runs in seconds.

```bash
python3 benchmarks/evolving_state/run.py          # human report
python3 benchmarks/evolving_state/run.py --json   # machine-readable
```

Exit code 0 only when every scenario grades `CURRENT`.

## What is graded

The served surface under test is the LOCAL recall path (`store.search`
with `federated=False` + `format_context_block`) — the context ordinary
memories are handed to `recall`, the MCP `recall` tool and answer
prompts. Out of scope, stated plainly: federation, the authorization
ledger section, the MEMORY.md index content, and any model's reading of
the block. Closed-pool grading, like the paper:

| grade | meaning |
|---|---|
| `CURRENT` | the served block reflects the current state |
| `SUPERSEDED` | an obsolete state leaks into the served block |
| `FAIL` | neither — nothing usable was served |

## Scenarios

| id | evolution mechanic | contract |
|---|---|---|
| S1 | supersede chain | new served, old absent |
| S2 | expiry past its date | absent from every served view |
| S3 | outcome "bad" (FL-2) | current served; penalized memory stays **visible** but marked `(tentative)` — the penalty lives in confidence weight, search ranking stays relevance-based *by design*; hiding history would itself be a FAIL |
| S4 | archive | absent |
| S5 | transit on a superseded memory | traversal grants no authority: still absent from served answers |
| S6 | two-hop supersede chain | only the newest served |

## Honesty notes

- This bench exercises **foldcrumbs' visibility and ranking contracts**, not
  an LLM's reading comprehension. StateMemBench grades model answers; we
  grade the context a model is handed. Both matter; we only claim the
  second.
- S3's grade changed during development from "rank above" to "served with
  tentative marker": the first formulation contradicted the FL-2 design
  (a penalty must demote weight, never hide a memory). The scenario file
  records the contract; `validate_suite` refuses an empty or malformed
  suite, and `tests/test_bench_evolving.py` pins the core contracts with
  mutation probes (a product regression that serves superseded memories
  must turn this bench red — verified by mutating `_visible` in-memory).
- S5 tests the transit flag's effect on the served recall view, not a
  graph traversal.
- We have not run StateMemBench itself and make no comparative claim
  against systems measured there.

Add scenarios by editing `scenarios.json` — setup ops (`remember`,
`supersede`, `archive`, `outcome_bad`, `transit`) are executed against a
fresh temp store per scenario; nothing touches a real store.
