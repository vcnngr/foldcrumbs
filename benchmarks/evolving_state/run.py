#!/usr/bin/env python3
"""foldcrumbs evolving-state mini-bench — runner.

Inspired by StateMemBench (arXiv 2608.19652): does the memory system serve
the CURRENT state of the world, or a superseded one? Their dataset is not
public; this is our own smaller version — fully deterministic, stdlib only,
no LLM calls (the served surface under test is the recall path: store.search
+ format_context_block, exactly what recall/answer/MCP feed on).

Usage:
    python3 benchmarks/evolving_state/run.py            # run, print report
    python3 benchmarks/evolving_state/run.py --json     # machine-readable

Grading per scenario (closed pool, like the paper):
    CURRENT    — the expected current memory is served (when one is
                 expected) and the obsolete one is NOT in the block
    SUPERSEDED — the obsolete memory appears in the served block
    FAIL       — neither: nothing usable was served

Exit code 0 only when every scenario grades CURRENT. The bench is part of
the repo: a regression in visibility rules (supersede/expiry/archive/
outcome/transit) turns it red.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.json"


def _run_scenario(scen: dict) -> dict:
    """Execute one scenario in a fresh temp store. Returns the grade dict."""
    from foldcrumbs import relations, store
    from foldcrumbs.profile import format_context_block
    from foldcrumbs.schema import MemoryRecord

    tmp = Path(tempfile.mkdtemp(prefix="fc_bench_"))
    memdir = tmp / "memory"
    memdir.mkdir()
    # route the store into the sandbox for this scenario only
    os.environ["FOLDCRUMBS_DIR"] = str(memdir)
    os.environ["FOLDCRUMBS_STATE_DIR"] = str(tmp / "state")

    keys: dict[str, MemoryRecord] = {}
    try:
        for step in scen["setup"]:
            op = step["op"]
            if op == "remember":
                rec = MemoryRecord(
                    title=step["title"], content=step["content"],
                    type=step.get("type", "fact"), source="bench")
                if "expires_in_days" in step:
                    rec.expires_at = datetime.now(timezone.utc) + timedelta(
                        days=step["expires_in_days"])
                store.write_memory(rec)
                keys[step["key"]] = rec
            elif op == "supersede":
                old, new = keys[step["old"]], keys[step["new"]]
                ok = store.supersede(old.filename(), new.filename())
                if not ok:
                    return {"id": scen["id"], "grade": "FAIL",
                            "reason": "setup: supersede returned False"}
            elif op == "archive":
                target = keys[step["target"]]
                store.set_status(target.filename(), "archived")
            elif op == "outcome_bad":
                from foldcrumbs import outcome as outcome_mod
                target = keys[step["target"]]
                res = outcome_mod.set_outcome(target.filename(), "bad",
                                              note="bench")
                if not res.get("ok"):
                    return {"id": scen["id"], "grade": "FAIL",
                            "reason": f"setup: outcome refused: {res}"}
            elif op == "transit":
                target = keys[step["target"]]
                relations.set_transit(target.id, True)
            else:
                return {"id": scen["id"], "grade": "FAIL",
                        "reason": f"unknown op {op!r}"}
        store.rebuild_index()

        hits = store.search(scen["query"], limit=10, federated=False)
        block = format_context_block(hits, heading=scen["query"])

        expect_key = scen.get("expect_current_key")
        forbid_key = scen.get("forbid_key")
        forbidden = keys[forbid_key].content if forbid_key else None
        expected = keys[expect_key].content if expect_key else None

        # a content line may be re-wrapped by the renderer: probe on a
        # distinctive fragment, not the whole string
        def _served(content: str) -> bool:
            probe = content.strip().split(".")[0][:40].lower()
            return probe in block.lower()

        if (tentative_key := scen.get("expect_tentative_key")):
            # FL-2 contract: a "bad" verdict penalizes compute_confidence,
            # it does NOT hide the memory — history stays visible, marked
            # tentative. Search ranking is relevance-based by design; the
            # honest grade is: current served AND penalized marked.
            pen = keys[tentative_key]
            if expected is None or not _served(expected):
                grade, reason = "FAIL", "expected memory not served"
            elif not _served(pen.content):
                grade, reason = "FAIL", "penalized memory vanished (history hidden — FL-2 violation)"
            elif "(tentative)" not in block:
                grade, reason = "SUPERSEDED", "penalized memory served WITHOUT tentative marker"
            else:
                # marker must sit on the penalized line, not another
                marked = [ln for ln in block.splitlines()
                          if "(tentative)" in ln]
                frag = pen.content.strip().split(".")[0][:40].lower()
                if any(frag in ln.lower() for ln in marked):
                    grade, reason = "CURRENT", "current served; penalized visible AND marked tentative"
                else:
                    grade, reason = "SUPERSEDED", "tentative marker on the wrong line"
        elif forbidden and _served(forbidden):
            grade, reason = "SUPERSEDED", "obsolete memory in served block"
        elif expected and _served(expected):
            grade, reason = "CURRENT", "current memory served, obsolete absent"
        elif expected is None and forbidden and not _served(forbidden):
            grade, reason = "CURRENT", "nothing expected; obsolete correctly absent"
        else:
            grade, reason = "FAIL", "neither expected nor forbidden content served"
        return {"id": scen["id"], "grade": grade, "reason": reason,
                "hits": len(hits)}
    finally:
        os.environ.pop("FOLDCRUMBS_DIR", None)
        os.environ.pop("FOLDCRUMBS_STATE_DIR", None)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    args = parser.parse_args()

    scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    results = [_run_scenario(s) for s in scenarios["scenarios"]]

    counts = {"CURRENT": 0, "SUPERSEDED": 0, "FAIL": 0}
    for r in results:
        counts[r["grade"]] += 1

    if args.json:
        print(json.dumps({"bench": scenarios["name"], "results": results,
                          "summary": counts}, indent=2))
    else:
        print(f"== {scenarios['name']} ==")
        for r in results:
            print(f"  {r['grade']:11} {r['id']} — {r['reason']}")
        total = len(results)
        print(f"-- CURRENT {counts['CURRENT']}/{total} · "
              f"SUPERSEDED {counts['SUPERSEDED']} · FAIL {counts['FAIL']}")
    return 0 if counts["CURRENT"] == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
