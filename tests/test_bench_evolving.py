"""Robustness meta-tests for the evolving-state mini-bench (RT PR62 F1/F2).

A benchmark that cannot fail is marketing. These tests mutate the product
contracts in-memory and assert the bench grades the mutation SUPERSEDED /
FAIL instead of staying green:

* F1 mutant: store._visible lets a superseded mid-chain record (S6 v2,
  Kafka) through -> S6 must grade SUPERSEDED, main must exit nonzero.
* F2: the sandbox must be real — config.STATE_DIR and config.SEMANTIC are
  module-level constants captured at import; the runner must override them
  per scenario (state inside the scenario tmp) and restore the caller's
  values afterwards, and the embeddings channel must never be consulted.
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

BENCH = REPO / "benchmarks" / "evolving_state"


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "fc_bench_runner", BENCH / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestF1MutantCaught(unittest.TestCase):

    def test_s6_mutant_visible_midchain_grades_superseded(self):
        runner = _load_runner()
        from foldcrumbs import store
        scenarios = json.loads((BENCH / "scenarios.json").read_text())
        s6 = next(s for s in scenarios["scenarios"]
                  if s["id"] == "S6-two-hops")
        real_visible = store._visible

        def mutant(rec):
            # the exact RT PoC: keep original behavior, but ALSO let the
            # superseded mid-chain record (v2 Kafka) stay visible
            if rec.content.startswith("We moved the job queue to Kafka."):
                return rec.status in ("active", "superseded") \
                    and not rec.is_expired
            return real_visible(rec)

        store._visible = mutant
        try:
            res = runner._run_scenario(s6)
        finally:
            store._visible = real_visible
        self.assertEqual(res["grade"], "SUPERSEDED",
                         f"mutant not caught: {res}")

    def test_s1_mutant_grades_superseded(self):
        # same mutation on the single-hop scenario
        runner = _load_runner()
        from foldcrumbs import store
        scenarios = json.loads((BENCH / "scenarios.json").read_text())
        s1 = next(s for s in scenarios["scenarios"]
                  if s["id"] == "S1-supersede-chain")
        real_visible = store._visible

        def mutant(rec):
            if rec.content.startswith("We deploy on Fridays"):
                return not rec.is_expired
            return real_visible(rec)

        store._visible = mutant
        try:
            res = runner._run_scenario(s1)
        finally:
            store._visible = real_visible
        self.assertEqual(res["grade"], "SUPERSEDED")

    def test_baseline_still_current(self):
        # the mutants above are in-memory only: the genuine bench is green
        runner = _load_runner()
        scenarios = json.loads((BENCH / "scenarios.json").read_text())
        grades = {r["id"]: r["grade"]
                  for r in (runner._run_scenario(s)
                            for s in scenarios["scenarios"])}
        self.assertTrue(all(g == "CURRENT" for g in grades.values()), grades)

    def test_suite_validation_rejects_empty_and_duplicates(self):
        runner = _load_runner()
        with self.assertRaises(Exception):
            runner.validate_suite({"name": "x", "scenarios": []})
        with self.assertRaises(Exception):
            runner.validate_suite({"name": "x", "scenarios": [
                {"id": "dup", "setup": [], "query": "q"},
                {"id": "dup", "setup": [], "query": "q"},
            ]})
        with self.assertRaises(Exception):
            runner.validate_suite({"name": "x", "scenarios": [
                {"id": "bad-ref", "setup": [], "query": "q",
                 "forbid_key": "nope"},
            ]})


class TestF2SandboxReal(unittest.TestCase):

    def test_state_and_memory_resolve_inside_scenario_tmp(self):
        runner = _load_runner()
        from foldcrumbs import config
        scenarios = json.loads((BENCH / "scenarios.json").read_text())
        s1 = scenarios["scenarios"][0]

        outer_state = config.STATE_DIR
        res = runner._run_scenario(s1)
        self.assertEqual(res["grade"], "CURRENT")
        # after the run, config.STATE_DIR must be restored to the caller's
        self.assertEqual(config.STATE_DIR, outer_state)

    def test_semantic_disabled_during_bench(self):
        runner = _load_runner()
        from foldcrumbs import config, embeddings
        scenarios = json.loads((BENCH / "scenarios.json").read_text())
        outer_semantic = config.SEMANTIC
        calls = []

        def spy_embed(*a, **k):
            calls.append(1)
            raise AssertionError("embeddings must never be called by the bench")

        real_embed = getattr(embeddings, "embed", None)
        config.SEMANTIC = True          # hostile caller env
        if real_embed is not None:
            embeddings.embed = spy_embed
        try:
            for s in scenarios["scenarios"]:
                r = runner._run_scenario(s)
                self.assertEqual(r["grade"], "CURRENT", r)
        finally:
            if real_embed is not None:
                embeddings.embed = real_embed
            config.SEMANTIC = outer_semantic
        self.assertEqual(calls, [], "embedding channel was invoked")

    def test_env_restored_after_exception(self):
        runner = _load_runner()
        os.environ["FOLDCRUMBS_DIR"] = "/tmp/fc_bench_env_probe"
        try:
            bad = {"id": "boom", "query": "q",
                   "setup": [{"op": "no_such_op"}]}
            res = runner._run_scenario(bad)   # returns FAIL, must not raise
            self.assertEqual(res["grade"], "FAIL")
            self.assertEqual(os.environ["FOLDCRUMBS_DIR"],
                             "/tmp/fc_bench_env_probe",
                             "caller env clobbered by the runner")
        finally:
            os.environ.pop("FOLDCRUMBS_DIR", None)


if __name__ == "__main__":
    unittest.main()
