"""RRF fusion for the semantic channel (docs/design/rrf-fusion.md rev 4).

Test matrix T1-T11. The embedder is stubbed with deterministic 2-D
vectors (cos(q=(1,0), (s, sqrt(1-s^2))) = s within float precision —
normalization can move the last bit), so every expected rank is computed
from declared scores, never from a live endpoint.
"""

import math
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sandbox import SANDBOX, is_inside  # noqa: E402,F401

from foldcrumbs import config, embeddings, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402

from test_foldcrumbs import TmpStore  # noqa: E402


def _rec(title, content, type_="fact"):
    return MemoryRecord(title=title, content=content, type=type_)


class _SemStore(TmpStore):
    """TmpStore + a stubbed embedder driven by a sem-score table.

    Set self.sem (dict filename->score) before searching; embed() is
    monkeypatched so the query vector is (1,0) and each candidate's
    vector encodes exactly the wanted cosine.
    """

    def setUp(self):
        super().setUp()
        self.sem = {}
        self._real_embed = embeddings.embed
        self._real_semantic = config.SEMANTIC
        config.SEMANTIC = True
        outer = self

        def fake_embed(texts):
            # texts[0] is the query; the rest are haystacks in candidate
            # order. Map haystack -> filename via the store contents.
            hay_to_name = {}
            for p in sorted(Path(outer.dir).glob("*.md")):
                rec = MemoryRecord.from_markdown(
                    p.read_text(encoding="utf-8"))
                hay = f"{rec.title}\n{rec.content}\n{' '.join(rec.tags)}".lower()
                hay_to_name[hay] = rec.filename()
            vecs = [(1.0, 0.0)]
            for t in texts[1:]:
                name = hay_to_name.get(t)
                s = outer.sem.get(name, 0.0)
                s = max(-1.0, min(1.0, s))
                vecs.append((s, math.sqrt(max(0.0, 1.0 - s * s))))
            return [list(v) for v in vecs]

        embeddings.embed = fake_embed
        self.addCleanup(self._restore)

    def _restore(self):
        embeddings.embed = self._real_embed
        config.SEMANTIC = self._real_semantic

    def _names(self, results):
        return [m.filename() for m in results]


class TestT1GoldenLegacy(_SemStore):
    """I1: semantic off/failure => ordering identical to the legacy path,
    including 2-decimal near-ties and reinforcement tie groups.

    RT r3 (GPT, card t_e6983ee3): the previous fixture was vacuous — the
    two "near-ties" had the SAME raw score (0.9474576271186441 both), so
    bucket precision was never exercised, and the reinforcement assert
    could not distinguish the tie group from the served record because
    an earlier limit=5 search had already reinforced both. Rebuilt per
    the reviewer's minimal fix: two raw scores DISTINCT in the same
    2-decimal bucket with the raw order OPPOSITE to the tie-break,
    explicit created_at timestamps, and reinforcement asserted as a
    COUNT DELTA from an empty baseline.
    """

    Q = "deploy tuesday"

    def _lex(self, m):
        # the production lexical formula (store.py ~1106): overlap*0.9 +
        # SequenceMatcher ratio *0.1 over the same haystack shape
        from difflib import SequenceMatcher
        hay = f"{m.title}\n{m.content}\n{' '.join(m.tags)}".lower()
        if self.Q in hay:
            return 1.0
        words = [w for w in self.Q.split() if len(w) > 2]
        overlap = sum(1 for w in words if w in hay) / len(words)
        return overlap * 0.9 + SequenceMatcher(None, self.Q, hay).ratio() * 0.1

    def setUp(self):
        super().setUp()
        from datetime import datetime, timedelta, timezone
        # raw-distinct, same 2-decimal bucket (verified below): "morning"
        # scores higher raw than "morningz" (shorter hay, better ratio)
        self.a = _rec("Deploy window", "we deploy on tuesday morning")
        self.b = _rec("Deploy windows", "we deploy on tuesday morningz")
        # explicit timestamps — do NOT assume same-second creation:
        # b is 2 days fresher, so the tie-break chain prefers b while
        # the raw score prefers a. A mutant comparing raw floats
        # (no bucketing) would serve a first and FAIL.
        now = datetime.now(timezone.utc)
        self.a.created_at = now - timedelta(days=2)
        self.b.created_at = now
        store.write_memory(self.a)
        store.write_memory(self.b)
        config.SEMANTIC = False

    def test_fixture_pair_properties(self):
        # self-verifying fixture: if difflib ever changes and the pair
        # stops sharing a bucket, THIS fails — not a misleading golden
        sa, sb = self._lex(self.a), self._lex(self.b)
        self.assertNotEqual(sa, sb, "raw scores must be distinct")
        self.assertEqual(round(sa, 2), round(sb, 2),
                         "raw scores must share the 2-decimal bucket")
        self.assertGreater(sa, sb, "raw order must be a > b ...")
        self.assertGreater(self.b.created_at, self.a.created_at,
                           "... and tie-break order must be b > a (opposite)")

    def test_semantic_off_legacy_order(self):
        # (a) the embedder must NEVER be called with the flag off;
        # (b) golden order pinned: bucket-equal scores => tie-break
        #     chain decides => b (fresher) first, NOT a (higher raw).
        #     A precision-3 (or full-float) mutant reorders to [a, b].
        calls = []

        def spy(texts):
            calls.append(texts)
            return None
        embeddings.embed = spy
        counts_before = store.recalls.counts(self.dir)
        self.assertEqual({k: v for k, v in counts_before.items() if v}, {},
                         "reinforcement baseline must be empty")
        r1 = store.search(self.Q, limit=5)
        self.assertEqual(calls, [], "embedder called with SEMANTIC off")
        self.assertEqual(self._names(r1),
                         [self.b.filename(), self.a.filename()])
        # (c) tie-group reinforcement as a COUNT DELTA: limit=1 serves
        # only b, but a shares the bucket => delta +1 on BOTH. A
        # served-only mutant leaves delta_a == 0 and fails. Baseline is
        # taken AFTER the limit=5 search above (which reinforced both).
        counts_mid = store.recalls.counts(self.dir)
        before_a = counts_mid.get(self.a.id, 0)
        before_b = counts_mid.get(self.b.id, 0)
        store.search(self.Q, limit=1)
        counts_after = store.recalls.counts(self.dir)
        self.assertEqual(counts_after.get(self.b.id, 0) - before_b, 1)
        self.assertEqual(counts_after.get(self.a.id, 0) - before_a, 1,
                         "bucket-mate a must be reinforced at a limit=1 cut")

    def test_embedder_none_falls_back_to_legacy(self):
        # T6/I6 (RT r2 GPT F1): the flag must be ON and the embedder must
        # actually be CALLED, returning None mid-flight => legacy ordering.
        config.SEMANTIC = True
        calls = []

        def spy(texts):
            calls.append(texts)
            return None
        embeddings.embed = spy
        with_sem_none = self._names(store.search("deploy tuesday", limit=5))
        self.assertTrue(calls, "embedder was never called — test vacuous")
        config.SEMANTIC = False
        legacy = self._names(store.search("deploy tuesday", limit=5))
        self.assertEqual(with_sem_none, legacy)


class TestT2ExactMatchGuarantee(_SemStore):
    """I2 (ordinal, honest): exact lexical match (rank_lex 1) is never
    outranked by strictly-lower-both-channel evidence; crossed-rank
    rivals tie exactly and the tiebreak chain decides."""

    def test_exact_beats_lower_both_channels(self):
        # I2: never outranked by STRICTLY lower evidence in BOTH channels.
        # (weak must not have a better semantic score, or it becomes the
        # crossed-rank tie case — covered by the next test, not this one.)
        exact = _rec("Redis port", "redis port is 6379 exact.")
        weak = _rec("Cache talk", "something about cache warming.")
        store.write_memory(exact)
        store.write_memory(weak)
        self.sem = {exact.filename(): 0.60, weak.filename(): 0.50}
        res = self._names(store.search("redis port is 6379 exact", limit=5))
        self.assertEqual(res[0], exact.filename())

    def test_crossed_ranks_tie_settles_on_tiebreak(self):
        # (rank_lex 1, rank_sem 2) vs (rank_lex 2, rank_sem 1):
        # both rrf = 1/61 + 1/62, bit-identical (commutativity)
        m1 = _rec("Alpha exact", "alpha query term here.")
        m2 = _rec("Beta partial", "beta mentions query loosely x1 x2 x3.")
        store.write_memory(m1)
        store.write_memory(m2)
        # m1: exact substring (lex 1.0); m2: weaker lexical
        self.sem = {m1.filename(): 0.30, m2.filename(): 0.90}
        res = store.search("alpha query term here", limit=5)
        names = self._names(res)
        self.assertEqual(len(names), 2)
        # RT r2 (GPT F1): pin the EXPECTED winner, not just repeatability.
        # rrf ties bit-for-bit (1/61+1/62 == 1/62+1/61 by commutativity);
        # the tie-break chain decides: same-second created_at and equal
        # reinforcement => filename order ("alpha" < "beta") => m1 first.
        self.assertEqual(names[0], m1.filename(),
                         "crossed-rank tie must settle on the tie-break chain")
        again = self._names(store.search("alpha query term here", limit=5))
        self.assertEqual(names, again)


class TestT3AgreementFixture(_SemStore):
    """I3: explicit rank table — agreement (2,1) beats single-channel
    strength (1,3): 1/62+1/61 > 1/61+1/63 (verified numerically)."""

    def test_agreement_outranks_single_channel(self):
        # RT r2 (GPT F1): the DESIGN table (2,1) vs (1,3) — the old
        # fixture accidentally built a crossed TIE (3,1)/(1,3).
        #   A ranks (lex 2, sem 1): rrf = 1/62+1/61 = 0.032522  <- wins
        #   B ranks (lex 1, sem 3): rrf = 1/61+1/63 = 0.032266
        #   C ranks (lex 3, sem 2): rrf = 1/63+1/62 = 0.032008
        # A (agreement) overtakes B (single-channel exact match): the
        # declared, honest I3 inversion — no tie involved.
        a = _rec("Migration plan", "the migration window plan.")
        b = _rec("Migration window", "migration window tuesday exact.")
        c = _rec("Migration notes", "migration notes only.")
        store.write_memory(a)
        store.write_memory(b)
        store.write_memory(c)
        # lexical ranks must come out B=1 (exact substring), A=2, C=3
        q = "migration window tuesday exact"
        self.sem = {a.filename(): 0.95, c.filename(): 0.60,
                    b.filename(): 0.30}
        names = self._names(store.search(q, limit=5))
        self.assertEqual(names, [a.filename(), b.filename(), c.filename()],
                         "agreement (2,1) must overtake single-channel (1,3)")


class TestT4AdmissionEquivalence(_SemStore):
    """I4: admitted set == capped-max admitted set:
    max(lex, sem*0.8) >= 0.22  <=>  lex >= 0.22 OR sem >= 0.275."""

    def test_below_floor_not_admitted(self):
        m = _rec("Quiet memory", "completely unrelated content qqq.")
        store.write_memory(m)
        # sem just below the floor (0.274 < 0.275), lex ~0 for this query
        self.sem = {m.filename(): 0.274}
        res = self._names(store.search("semantic query no lexical", limit=5))
        self.assertNotIn(m.filename(), res)

    def test_at_floor_admitted(self):
        # boundary: sem exactly at the floor IS admitted (>= semantics,
        # same as the old capped-max bar: 0.275*0.8 = 0.22 >= 0.22)
        m = _rec("Boundary memory", "totally different words here.")
        store.write_memory(m)
        self.sem = {m.filename(): 0.275}
        res = self._names(store.search("semantic query no lexical", limit=5))
        self.assertIn(m.filename(), res)

    def test_above_floor_admitted(self):
        m = _rec("Paraphrase memory", "totally different words entirely.")
        store.write_memory(m)
        self.sem = {m.filename(): 0.9}
        res = self._names(store.search("semantic query no lexical", limit=5))
        self.assertIn(m.filename(), res)


class TestT8T9T10EdgeCases(_SemStore):
    def test_t8_single_candidate_served(self):
        m = _rec("Only memory", "the only one here.")
        store.write_memory(m)
        self.sem = {m.filename(): 0.5}
        res = self._names(store.search("the only one", limit=5))
        self.assertEqual(res, [m.filename()])

    def test_t9_equal_lex_semantic_ranks_decide(self):
        # identical content => identical lexical scores (exact substring
        # for all three); rank_lex falls to the tiebreak chain (filename
        # order AAA<BBB<CCC). Semantic ranks then decide the fused order:
        #   AAA (lex 1, sem 3) = 1/61+1/63 = 0.032266
        #   BBB (lex 2, sem 1) = 1/62+1/61 = 0.032522
        #   CCC (lex 3, sem 2) = 1/63+1/62 = 0.032008
        # declared outcome: BBB, AAA, CCC — all sums distinct, no tie.
        content = "identical words here"
        aaa = _rec("AAA", content)
        bbb = _rec("BBB", content)
        ccc = _rec("CCC", content)
        for m in (aaa, bbb, ccc):
            store.write_memory(m)
        self.sem = {bbb.filename(): 0.90, ccc.filename(): 0.60,
                    aaa.filename(): 0.40}
        names = self._names(store.search(content, limit=5))
        self.assertEqual(names, [bbb.filename(), aaa.filename(),
                                 ccc.filename()])


class TestT11ReinforcementCutoff(_SemStore):
    """D5/T11 (RT r2 PoC): three distinct rrf scores, limit=1 =>
    exactly ONE reinforced."""

    def test_three_distinct_scores_limit_one(self):
        q = "reinforcement probe words"
        recs = []
        for i in range(3):
            m = _rec(f"Probe {i}", f"{q} variant {i} filler{i}.")
            store.write_memory(m)
            recs.append(m)
        self.sem = {recs[0].filename(): 0.95, recs[1].filename(): 0.60,
                    recs[2].filename(): 0.30}
        # lexical ranks differ (variant words), semantic ranks differ =>
        # three distinct rrf values
        store.search(q, limit=1)
        counts = store.recalls.counts(self.dir)
        reinforced = [rid for rid, c in counts.items() if c > 0]
        self.assertEqual(len(reinforced), 1,
                         f"expected exactly 1 reinforced, got {reinforced}")


class TestT10ReinforcementTieBreak(_SemStore):
    """D3/T10 (RT r2 GPT F1: autonomous fixture): two candidates with
    EQUAL rrf (crossed ranks) but different recall counts — the more
    reinforced one must win the tie-break."""

    def test_equal_rrf_more_recalled_wins(self):
        # m1: exact lexical (rank_lex 1), weaker sem (rank_sem 2)
        # m2: weaker lexical (rank_lex 2), best sem (rank_sem 1)
        # => both rrf = 1/61+1/62, bit-identical tie.
        m1 = _rec("Gamma exact", "gamma query token here.")
        m2 = _rec("Delta partial", "delta mentions query loosely y1 y2 y3.")
        store.write_memory(m1)   # m1 older
        store.write_memory(m2)   # m2 fresher — would win on recency alone
        self.sem = {m1.filename(): 0.30, m2.filename(): 0.90}
        # pre-seed reinforcement: m1 recalled more often in the past
        store.recalls.reinforce([m1.id, m1.id, m1.id], cwd=self.dir)
        store.recalls.reinforce([m2.id], cwd=self.dir)
        names = self._names(store.search("gamma query token here", limit=5))
        # tie-break = 0.6*recency + 0.4*reinforcement (normalized shares);
        # the pin is the OBSERVED, declared outcome: with these seeds the
        # more-recalled older record overtakes the fresher one.
        self.assertEqual(names[0], m1.filename(),
                         "reinforcement share must be able to settle an rrf tie")


if __name__ == "__main__":
    unittest.main()
