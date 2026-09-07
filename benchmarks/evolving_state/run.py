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
    from foldcrumbs import config, relations, store
    from foldcrumbs.profile import format_context_block
    from foldcrumbs.schema import MemoryRecord

    tmp = Path(tempfile.mkdtemp(prefix="fc_bench_"))
    memdir = tmp / "memory"
    memdir.mkdir()
    statedir = tmp / "state"
    # RT PR62 F2: env alone is NOT the sandbox. config.STATE_DIR and
    # config.SEMANTIC are module constants captured at import time; the
    # runner must override them per scenario and restore the caller's
    # values afterwards. FOLDCRUMBS_DIR is read dynamically, but we save
    # and restore the env too — never pop what the caller had set.
    saved_env = {k: os.environ.get(k)
                 for k in ("FOLDCRUMBS_DIR", "ENGRAM_DIR",
                           "FOLDCRUMBS_STATE_DIR", "ENGRAM_STATE_DIR")}
    saved_state = config.STATE_DIR
    saved_semantic = config.SEMANTIC
    os.environ["FOLDCRUMBS_DIR"] = str(memdir)
    os.environ["FOLDCRUMBS_STATE_DIR"] = str(statedir)
    config.STATE_DIR = statedir
    config.SEMANTIC = False   # the bench never consults embeddings

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
                ok = store.set_status(target.filename(), "archived")
                if not ok:
                    return {"id": scen["id"], "grade": "FAIL",
                            "reason": "setup: archive returned False"}
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
                # postcondition (RT F4): the flag must actually persist
                check = store.get(target.filename())
                if check is None or not relations._transit_gate(check):
                    return {"id": scen["id"], "grade": "FAIL",
                            "reason": "setup: transit did not persist"}
            elif op == "invalidate":
                # INV design rev2: dependent holds only while target lives
                dep, tgt = keys[step["dependent"]], keys[step["target"]]
                ok = relations.add_relation(
                    dep.id, "invalidated_by",
                    target={"k": "m", "id": tgt.id},
                    evidence="bench contract", confidence=0.9, prov="manual")
                if not ok:
                    return {"id": scen["id"], "grade": "FAIL",
                            "reason": "setup: invalidate edge not written"}
            else:
                return {"id": scen["id"], "grade": "FAIL",
                        "reason": f"unknown op {op!r}"}
        store.rebuild_index()

        hits = store.search(scen["query"], limit=10, federated=False)
        block = format_context_block(hits, heading=scen["query"])

        expect_key = scen.get("expect_current_key")
        forbid_keys = scen.get("forbid_keys")
        if forbid_keys is None:
            # singular form accepted for compatibility
            forbid_keys = [scen["forbid_key"]] if scen.get("forbid_key") else []
        forbidden = [keys[k].content for k in forbid_keys]
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
                # marker must sit on EVERY line serving the penalized
                # memory (RT r2 F3: a duplicated unmarked bullet next to
                # the marked one is a leak, not a pass)
                frag = pen.content.strip().split(".")[0][:40].lower()
                serving = [ln for ln in block.splitlines()
                           if frag in ln.lower()]
                if serving and all("(tentative)" in ln for ln in serving):
                    grade, reason = "CURRENT", "current served; penalized visible AND marked tentative"
                else:
                    grade, reason = "SUPERSEDED", "penalized memory served on a line WITHOUT the tentative marker"
        else:
            leaked = [f for f in forbidden if _served(f)]
            if leaked:
                grade, reason = ("SUPERSEDED",
                                 f"obsolete memory in served block "
                                 f"({len(leaked)} of {len(forbidden)} forbidden)")
            elif expected and _served(expected):
                grade, reason = "CURRENT", "current memory served, obsolete absent"
            elif expected is None and forbidden and not leaked:
                grade, reason = "CURRENT", "nothing expected; obsolete correctly absent"
            else:
                grade, reason = "FAIL", "neither expected nor forbidden content served"
        return {"id": scen["id"], "grade": grade, "reason": reason,
                "hits": len(hits)}
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.STATE_DIR = saved_state
        config.SEMANTIC = saved_semantic
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def validate_suite(scenarios: dict) -> None:
    """RT PR62 F5: an empty or malformed suite must never exit green.

    Refuses: zero scenarios, duplicate ids, key references (expect/forbid/
    tentative/setup targets) that no remembered key provides, unknown ops.
    """
    suite = scenarios.get("scenarios") or []
    if not suite:
        raise ValueError("bench suite is empty — refusing to grade nothing "
                         "as CURRENT")
    seen: set[str] = set()
    known_ops = {"remember", "supersede", "archive", "outcome_bad", "transit",
                 "invalidate"}
    for scen in suite:
        sid = scen.get("id")
        if not sid:
            raise ValueError("scenario without id")
        if sid in seen:
            raise ValueError(f"duplicate scenario id: {sid}")
        seen.add(sid)
        keys: set[str] = set()
        for step in scen.get("setup", []):
            op = step.get("op")
            if op not in known_ops:
                raise ValueError(f"{sid}: unknown op {op!r}")
            if op == "remember":
                keys.add(step["key"])
            elif op == "supersede":
                for ref in ("old", "new"):
                    if step[ref] not in keys:
                        raise ValueError(
                            f"{sid}: supersede references unknown key "
                            f"{step[ref]!r}")
            elif op == "invalidate":
                for ref in ("dependent", "target"):
                    if step[ref] not in keys:
                        raise ValueError(
                            f"{sid}: invalidate references unknown key "
                            f"{step[ref]!r}")
            else:
                if step["target"] not in keys:
                    raise ValueError(
                        f"{sid}: {op} references unknown key "
                        f"{step['target']!r}")
        for field in ("expect_current_key", "forbid_key",
                      "expect_tentative_key"):
            ref = scen.get(field)
            if ref is not None and ref not in keys:
                raise ValueError(f"{sid}: {field} references unknown key "
                                 f"{ref!r}")
        for ref in scen.get("forbid_keys", []):
            if ref not in keys:
                raise ValueError(f"{sid}: forbid_keys references unknown "
                                 f"key {ref!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    args = parser.parse_args()

    scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    validate_suite(scenarios)
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
