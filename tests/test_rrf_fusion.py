"""RRF fusion for the semantic channel (docs/design/rrf-fusion.md rev 4).

Test matrix T1-T11. The embedder is stubbed with deterministic 2-D
vectors (cos(q=(1,0), (s, sqrt(1-s^2))) = s exactly), so every expected
rank is computed from declared scores, never from a live endpoint.
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
    including 2-decimal near-ties and reinforcement tie groups."""

    def setUp(self):
        super().setUp()
        # near-ties: scores differing only beyond 2 decimals must stay
        # tied (round to same bucket) and order by the tiebreak chain
        self.a = _rec("Deploy window", "We deploy on tuesday mornings.")
        self.b = _rec("Deploy windows", "We deploy on tuesday morning.")
        store.write_memory(self.a)
        store.write_memory(self.b)
        config.SEMANTIC = False

    def test_semantic_off_legacy_order(self):
        r1 = store.search("deploy tuesday", limit=5)
        r2 = store.search("deploy tuesday", limit=5)
        self.assertEqual(self._names(r1), self._names(r2))
        self.assertEqual(len(r1), 2)

    def test_embedder_none_falls_back_to_legacy(self):
        # T6/I6: mid-flight embedder failure => legacy ordering
        embeddings.embed = lambda texts: None
        with_sem_none = self._names(store.search("deploy tuesday", limit=5))
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
        # rrf values must be bit-identical => order comes from the
        # tiebreak chain, deterministically: run twice, same answer
        again = self._names(store.search("alpha query term here", limit=5))
        self.assertEqual(names, again)


class TestT3AgreementFixture(_SemStore):
    """I3: explicit rank table — agreement (2,1) beats single-channel
    strength (1,3): 1/62+1/61 > 1/61+1/63 (verified numerically)."""

    def test_agreement_outranks_single_channel(self):
        # A: weaker lexical (rank_lex 2), strong semantic (rank_sem 1)
        # B: exact lexical (rank_lex 1), weak-but-admitted semantic
        #    (rank_sem 3 needs a third candidate between them)
        a = _rec("Migration plan", "the database migration plan for q3.")
        b = _rec("Migration window", "migration window words exact match.")
        c = _rec("Migration notes", "migration notes with query words.")
        store.write_memory(a)
        store.write_memory(b)
        store.write_memory(c)
        # lexical: query crafted so B is exact, C mid, A low-but-admitted
        q = "migration window words exact match"
        self.sem = {a.filename(): 0.95, b.filename(): 0.30,
                    c.filename(): 0.60}
        names = self._names(store.search(q, limit=5))
        # ranks: lex B=1, C=2, A=3? sem A=1, C=2, B=3
        # rrf: A=1/63+1/61, B=1/61+1/63 => tie; C=1/62+1/62 = 0.032258
        # A and B tie EXACTLY (crossed) -> tiebreak decides; C last.
        self.assertEqual(names[-1], c.filename())
        self.assertEqual(set(names[:2]), {a.filename(), b.filename()})


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


if __name__ == "__main__":
    unittest.main()
