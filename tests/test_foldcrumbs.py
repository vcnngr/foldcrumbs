"""Regression tests for foldcrumbs (stdlib unittest, no external deps).

Run: python3 -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from foldcrumbs import distill, install, redact, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402


class TmpStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccmem_test_")
        os.environ["ENGRAM_DIR"] = self.dir

    def tearDown(self):
        os.environ.pop("ENGRAM_DIR", None)


class TestSchema(unittest.TestCase):
    def test_roundtrip(self):
        r = MemoryRecord(title="T", content="Body text here.", type="decision",
                         confidence=0.9, tags=["a", "b"])
        back = MemoryRecord.from_markdown(r.to_markdown())
        self.assertEqual(back.title, "T")
        self.assertEqual(back.type, "decision")
        self.assertEqual(back.confidence, 0.9)
        self.assertEqual(back.tags, ["a", "b"])

    def test_invalid_type_falls_back(self):
        self.assertEqual(MemoryRecord(title="x", content="y", type="bogus").type, "fact")

    def test_legacy_type_preserved(self):
        self.assertEqual(MemoryRecord(title="x", content="y", type="project").type,
                         "project")

    def test_supersede_zeroes_confidence(self):
        r = MemoryRecord(title="x", content="y", type="fact", confidence=0.9)
        r.mark_superseded("other-id")
        self.assertEqual(r.compute_confidence(), 0.0)


class TestStore(TmpStore):
    def test_dedup_validates(self):
        a = MemoryRecord(title="Use stdlib", content="Hooks use only stdlib here.",
                         type="decision", confidence=0.9)
        self.assertEqual(store.upsert(a)[0], "created")
        b = MemoryRecord(title="Use stdlib only",
                         content="Hooks use only stdlib here now.",
                         type="decision", confidence=0.9)
        self.assertEqual(store.upsert(b)[0], "validated")
        self.assertEqual(len([m for m in store.load_all()]), 1)

    def test_index_grouped(self):
        store.upsert(MemoryRecord(title="R", content="rule", type="instruction"))
        store.upsert(MemoryRecord(title="F", content="fact", type="fact"))
        idx = store.rebuild_index().read_text()
        self.assertIn("## Rules", idx)
        self.assertIn("## Facts", idx)

    def test_index_links_to_real_file_not_derived_name(self):
        # A file imported under a non-canonical name (e.g. by another tool) must
        # still get a resolvable index link pointing at the real file on disk.
        weird = Path(self.dir) / "voice-clone.md"
        weird.write_text(
            "---\nname: Voice Clone App\ndescription: hook\ntype: project\n---\n\nbody\n",
            encoding="utf-8",
        )
        idx = store.rebuild_index().read_text()
        self.assertIn("(voice-clone.md)", idx)
        # And every link in the index resolves to an existing file.
        import re as _re
        for target in _re.findall(r"\]\(([^)]+\.md)\)", idx):
            self.assertTrue((Path(self.dir) / target).exists(), target)

    def test_degenerate_titles_get_distinct_files(self):
        a = MemoryRecord(title="", content="one", type="fact")
        b = MemoryRecord(title="", content="two", type="fact")
        self.assertEqual(a.title, "Untitled")
        self.assertNotEqual(a.filename(), b.filename())


class TestDistill(unittest.TestCase):
    def test_parser_tolerates_fences(self):
        text = '```json\n[{"type":"decision","title":"x","content":"c","confidence":0.9}]\n```'
        out = distill.parse_llm_memories(text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "decision")

    def test_gate_filters_low_confidence(self):
        self.assertFalse(distill._passes_gate(
            {"type": "fact", "content": "c", "confidence": 0.4}))
        self.assertTrue(distill._passes_gate(
            {"type": "fact", "content": "c", "confidence": 0.8}))

    def test_heuristic_classifies(self):
        h = distill.heuristic_memories("We decided to use X. Always lint first.")
        types = {m["type"] for m in h}
        self.assertIn("decision", types)
        self.assertIn("instruction", types)

    def test_is_artifact_flags_tooling_output(self):
        self.assertTrue(distill._is_artifact("| Index | File | Stato |"))
        self.assertTrue(distill._is_artifact("references MEMORY.md directly"))
        self.assertTrue(distill._is_artifact("Link OK ✓"))
        self.assertFalse(distill._is_artifact("We use os.replace for atomic writes."))
        # Generality: legit project prose must survive — e.g. a web project that
        # genuinely fixes broken links is NOT a tooling artifact.
        self.assertFalse(distill._is_artifact("Fixed the broken links on the docs page."))

    def test_heuristic_drops_self_referential_artifacts(self):
        h = distill.heuristic_memories(
            "We decided to use Postgres. Bug: dead links in MEMORY.md after rename.")
        joined = " ".join(m["content"] for m in h).lower()
        self.assertIn("postgres", joined)
        self.assertNotIn("dead links", joined)


class TestLLMBackend(unittest.TestCase):
    """CLI backends (claude-cli, codex): dispatch + the anti-recursion kill-switch."""

    def setUp(self):
        import importlib
        from foldcrumbs import config, llm
        self.config, self.llm, self._reload = config, llm, importlib.reload
        self._saved = {k: os.environ.get(k)
                       for k in ("ENGRAM_LLM_BACKEND", "ENGRAM_DISABLE",
                                 "ENGRAM_CLAUDE_BIN", "ENGRAM_CODEX_BIN")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._reload(self.config)
        self._reload(self.llm)

    def _reload_with(self, **env):
        for k, v in env.items():
            os.environ[k] = v
        self._reload(self.config)
        self._reload(self.llm)
        return self.llm

    def test_disabled_blocks_claude_cli_and_never_spawns(self):
        # Recursion guard: inside a foldcrumbs-spawned session the CLI backend is
        # unavailable and chat() returns None without shelling out.
        llm = self._reload_with(ENGRAM_LLM_BACKEND="claude-cli", ENGRAM_DISABLE="1")
        self.assertFalse(llm.available())
        self.assertIsNone(llm.chat([{"role": "user", "content": "hi"}]))

    def test_available_true_when_cli_present(self):
        llm = self._reload_with(
            ENGRAM_LLM_BACKEND="claude-cli", ENGRAM_CLAUDE_BIN=sys.executable)
        os.environ.pop("ENGRAM_DISABLE", None)
        self._reload(self.config)
        self._reload(self.llm)
        self.assertTrue(llm.available())  # sys.executable always exists

    def test_disabled_blocks_codex_and_never_spawns(self):
        # Same recursion-guard parity for the codex backend: disabled => no spawn.
        llm = self._reload_with(ENGRAM_LLM_BACKEND="codex", ENGRAM_DISABLE="1")
        self.assertFalse(llm.available())
        self.assertIsNone(llm.chat([{"role": "user", "content": "hi"}]))

    def test_codex_available_true_when_cli_present(self):
        llm = self._reload_with(
            ENGRAM_LLM_BACKEND="codex", ENGRAM_CODEX_BIN=sys.executable)
        os.environ.pop("ENGRAM_DISABLE", None)
        self._reload(self.config)
        self._reload(self.llm)
        self.assertTrue(llm.available())  # sys.executable always exists

    def test_codex_available_false_when_cli_missing(self):
        llm = self._reload_with(
            ENGRAM_LLM_BACKEND="codex",
            ENGRAM_CODEX_BIN="/nonexistent/codex-binary-xyz")
        os.environ.pop("ENGRAM_DISABLE", None)
        self._reload(self.config)
        self._reload(self.llm)
        self.assertFalse(llm.available())

    def test_none_backend_skips_llm_entirely(self):
        # The heuristic-only rung: chat() returns None without any network/CLI,
        # and available() is False by design (distill falls to the keyword path).
        llm = self._reload_with(ENGRAM_LLM_BACKEND="none")
        os.environ.pop("ENGRAM_DISABLE", None)
        self._reload(self.config)
        self._reload(self.llm)
        self.assertFalse(llm.available())
        self.assertIsNone(llm.chat([{"role": "user", "content": "hi"}]))


class TestBackendConfig(unittest.TestCase):
    """Install-time backend selection: configure_backend + prompt_backend."""

    def setUp(self):
        import importlib
        from foldcrumbs import config
        self.config = config
        self._dir = Path(tempfile.mkdtemp(prefix="ccmem_backend_"))
        # Drive STATE_DIR via env so an importlib.reload (below) keeps the temp
        # dir instead of snapping back to ~/.foldcrumbs.
        self._saved_env = os.environ.get("ENGRAM_STATE_DIR")
        os.environ["ENGRAM_STATE_DIR"] = str(self._dir)
        importlib.reload(config)

    def tearDown(self):
        import importlib
        if self._saved_env is None:
            os.environ.pop("ENGRAM_STATE_DIR", None)
        else:
            os.environ["ENGRAM_STATE_DIR"] = self._saved_env
        importlib.reload(self.config)

    def _read(self, name):
        return (self._dir / name).read_text(encoding="utf-8").strip()

    def test_configure_codex_writes_backend_and_bin(self):
        written = install.configure_backend("codex", bin_path="/opt/homebrew/bin/codex")
        self.assertIn("llm-backend", written)
        self.assertIn("codex-bin", written)
        self.assertEqual(self._read("llm-backend"), "codex")
        self.assertEqual(self._read("codex-bin"), "/opt/homebrew/bin/codex")

    def test_configure_claude_writes_backend_and_bin(self):
        install.configure_backend("claude-cli", bin_path="/usr/local/bin/claude")
        self.assertEqual(self._read("llm-backend"), "claude-cli")
        self.assertEqual(self._read("claude-bin"), "/usr/local/bin/claude")

    def test_configure_openai_persists_endpoint_and_model(self):
        install.configure_backend(
            "openai", endpoint="http://localhost:8081", model="gemma-4-26b-a4b-it")
        self.assertEqual(self._read("llm-backend"), "openai")
        self.assertEqual(self._read("llm-endpoint"), "http://localhost:8081")
        self.assertEqual(self._read("llm-model"), "gemma-4-26b-a4b-it")

    def test_configure_none_writes_only_marker(self):
        written = install.configure_backend("none")
        self.assertEqual(written, ["llm-backend"])
        self.assertEqual(self._read("llm-backend"), "none")

    def test_configure_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            install.configure_backend("gpt-9000")

    def test_config_reads_endpoint_from_state_file(self):
        # The openai endpoint/model written above must be picked up by config
        # (env unset) — that's what makes the install prompt meaningful.
        import importlib
        install.configure_backend("openai", endpoint="http://host:9999", model="m-1")
        saved = {k: os.environ.pop(k, None)
                 for k in ("ENGRAM_LLM_ENDPOINT", "ENGRAM_LLM_MODEL")}
        try:
            importlib.reload(self.config)
            self.assertEqual(self.config.LLM_ENDPOINT, "http://host:9999")
            self.assertEqual(self.config.LLM_MODEL, "m-1")
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
            importlib.reload(self.config)

    def test_prompt_picks_by_number(self):
        choice = install.prompt_backend(in_fn=lambda _: "2", out_fn=lambda *_: None)
        self.assertEqual(choice, "codex")

    def test_prompt_picks_by_name(self):
        choice = install.prompt_backend(in_fn=lambda _: "openai", out_fn=lambda *_: None)
        self.assertEqual(choice, "openai")

    def test_prompt_blank_is_default_first_choice(self):
        choice = install.prompt_backend(in_fn=lambda _: "", out_fn=lambda *_: None)
        self.assertEqual(choice, install.BACKEND_CHOICES[0][0])

    def test_prompt_unrecognised_returns_none(self):
        choice = install.prompt_backend(in_fn=lambda _: "zzz", out_fn=lambda *_: None)
        self.assertIsNone(choice)

    def test_prompt_eof_returns_none(self):
        def _eof(_):
            raise EOFError
        self.assertIsNone(install.prompt_backend(in_fn=_eof, out_fn=lambda *_: None))


class TestAutoSupersede(TmpStore):
    """The contradiction pass: a new memory can obsolete an old same-subject one."""

    def _old_pypi_decision(self) -> MemoryRecord:
        rec = MemoryRecord(
            title="Launch is GitHub only",
            content="PyPI publishing is deferred; the launch is GitHub-only for now.",
            type="decision")
        store.write_memory(rec)
        return rec

    def _new_pypi_fact(self) -> MemoryRecord:
        return MemoryRecord(
            title="Published to PyPI",
            content="foldcrumbs is published on PyPI, installable via pip install foldcrumbs.",
            type="fact")

    def test_conflict_candidates_same_subject_cross_type(self):
        self._old_pypi_decision()
        store.write_memory(MemoryRecord(title="Postgres storage",
                                        content="We use Postgres for storage.",
                                        type="decision"))
        names = [m.title for m in store.find_conflict_candidates(self._new_pypi_fact())]
        self.assertEqual(names, ["Launch is GitHub only"])

    def test_llm_yes_supersedes_old(self):
        old = self._old_pypi_decision()
        from unittest.mock import patch
        with patch.object(distill.llm, "chat", return_value='{"supersedes": true}'):
            res = distill.persist([self._new_pypi_fact()])
        self.assertEqual(res["superseded"], 1)
        reloaded = next(m for m in store.load_all() if m.id == old.id)
        self.assertEqual(reloaded.status, "superseded")
        self.assertEqual(reloaded.compute_confidence(), 0.0)
        idx = (Path(self.dir) / "MEMORY.md").read_text()
        self.assertNotIn("Launch is GitHub only", idx)
        self.assertIn("Published to PyPI", idx)

    def test_llm_no_keeps_old(self):
        old = self._old_pypi_decision()
        from unittest.mock import patch
        with patch.object(distill.llm, "chat", return_value='{"supersedes": false}'):
            res = distill.persist([self._new_pypi_fact()])
        self.assertEqual(res["superseded"], 0)
        reloaded = next(m for m in store.load_all() if m.id == old.id)
        self.assertEqual(reloaded.status, "active")

    def test_no_llm_fails_soft(self):
        old = self._old_pypi_decision()
        from unittest.mock import patch
        with patch.object(distill.llm, "chat", return_value=None):
            res = distill.persist([self._new_pypi_fact()])
        self.assertEqual(res["superseded"], 0)
        reloaded = next(m for m in store.load_all() if m.id == old.id)
        self.assertEqual(reloaded.status, "active")

    def test_kill_switch_skips_llm_entirely(self):
        self._old_pypi_decision()
        os.environ["FOLDCRUMBS_NO_AUTO_SUPERSEDE"] = "1"
        try:
            from unittest.mock import patch
            with patch.object(distill.llm, "chat",
                              side_effect=AssertionError("LLM must not be called")):
                res = distill.persist([self._new_pypi_fact()])
        finally:
            os.environ.pop("FOLDCRUMBS_NO_AUTO_SUPERSEDE", None)
        self.assertEqual(res["superseded"], 0)

    def test_validated_duplicate_never_triggers_pass(self):
        # A near-duplicate validates (dedup) instead of creating; the
        # contradiction pass runs only for genuinely new memories.
        rec = MemoryRecord(title="Use stdlib", content="Hooks use only stdlib here.",
                           type="decision")
        store.write_memory(rec)
        dup = MemoryRecord(title="Use stdlib only",
                           content="Hooks use only stdlib here now.", type="decision")
        from unittest.mock import patch
        with patch.object(distill.llm, "chat",
                          side_effect=AssertionError("LLM must not be called")):
            res = distill.persist([dup])
        self.assertEqual(res["validated"], 1)
        self.assertEqual(res["superseded"], 0)


class TestDistillGate(unittest.TestCase):
    """Per-machine distill opt-out (shared-store read-only consumer)."""

    def setUp(self):
        self._saved = os.environ.get("ENGRAM_NO_DISTILL")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("ENGRAM_NO_DISTILL", None)
        else:
            os.environ["ENGRAM_NO_DISTILL"] = self._saved

    def test_enabled_by_default(self):
        os.environ.pop("ENGRAM_NO_DISTILL", None)
        from foldcrumbs import config
        # No marker in a throwaway state dir → enabled.
        self.assertTrue(config.distill_enabled() or (config.STATE_DIR / "no-distill").exists())

    def test_env_disables(self):
        os.environ["ENGRAM_NO_DISTILL"] = "1"
        from foldcrumbs import config
        self.assertFalse(config.distill_enabled())

    def test_marker_disables(self):
        os.environ.pop("ENGRAM_NO_DISTILL", None)
        from foldcrumbs import config
        d = tempfile.mkdtemp(prefix="ccmem_state_")
        saved = config.STATE_DIR
        try:
            config.STATE_DIR = Path(d)
            self.assertTrue(config.distill_enabled())
            (Path(d) / "no-distill").write_text("", encoding="utf-8")
            self.assertFalse(config.distill_enabled())
        finally:
            config.STATE_DIR = saved

    def test_machine_local_backend_override(self):
        # A machine-local file selects the backend without any env var (the
        # mechanism that lets one synced machine differ from the others).
        from foldcrumbs import config
        saved_env = os.environ.pop("ENGRAM_LLM_BACKEND", None)
        saved_codex_bin = os.environ.pop("ENGRAM_CODEX_BIN", None)
        d = tempfile.mkdtemp(prefix="ccmem_state_")
        saved = config.STATE_DIR
        try:
            config.STATE_DIR = Path(d)
            self.assertEqual(config.llm_backend(), "openai")  # default
            (Path(d) / "llm-backend").write_text("claude-cli\n", encoding="utf-8")
            self.assertEqual(config.llm_backend(), "claude-cli")
            (Path(d) / "llm-backend").write_text("codex\n", encoding="utf-8")
            self.assertEqual(config.llm_backend(), "codex")
            # codex_bin honours the machine-local file too (no env var).
            self.assertEqual(config.codex_bin(), "codex")  # default name
            (Path(d) / "codex-bin").write_text("/opt/homebrew/bin/codex\n", encoding="utf-8")
            self.assertEqual(config.codex_bin(), "/opt/homebrew/bin/codex")
        finally:
            config.STATE_DIR = saved
            if saved_env is not None:
                os.environ["ENGRAM_LLM_BACKEND"] = saved_env
            if saved_codex_bin is not None:
                os.environ["ENGRAM_CODEX_BIN"] = saved_codex_bin


class TestRedact(unittest.TestCase):
    def test_scrubs_known_tokens(self):
        out = redact.scrub("key is sk-abcdefabcdefabcdefabcdef and gho_" + "a" * 36)
        self.assertNotIn("sk-abcdef", out)
        self.assertNotIn("gho_a", out)
        self.assertIn("[REDACTED]", out)

    def test_scrubs_kv_secret(self):
        out = redact.scrub('password = "hunter2secret"')
        self.assertNotIn("hunter2secret", out)
        self.assertIn("password", out)  # key name kept, value gone

    def test_keeps_normal_text(self):
        text = "We use os.replace for atomic writes."
        self.assertEqual(redact.scrub(text), text)


class TestAudit(TmpStore):
    def _write_raw(self, name, name_field, content, type_="fact"):
        (Path(self.dir) / name).write_text(
            f"---\nname: {name_field}\ndescription: d\ntype: {type_}\n---\n\n{content}\n",
            encoding="utf-8")

    def test_heal_index_relinks_orphan(self):
        from foldcrumbs import audit
        # A memory file present on disk but not in the index → heal rebuilds.
        self._write_raw("note.md", "Some note", "body")
        a = audit.audit()
        self.assertIn("note.md", a["orphans"])
        self.assertTrue(audit.heal_index())
        self.assertIn("(note.md)", store.rebuild_index().read_text())
        self.assertEqual(audit.audit()["orphans"], [])

    def test_audit_flags_pollution(self):
        from foldcrumbs import audit
        store.upsert(MemoryRecord(title="Good", content="We use os.replace.", type="decision"))
        self._write_raw("error_junk.md", "junk", "| Index | File | Stato |", "error")
        self.assertIn("error_junk.md", audit.audit()["pollution"])

    def test_prune_dry_run_then_apply(self):
        from foldcrumbs import audit
        self._write_raw("error_tbl.md", "tbl", "| a | b | c |", "error")
        store.upsert(MemoryRecord(title="Keep", content="Real decision here.", type="decision"))
        dry = audit.prune(apply=False)
        self.assertIn("error_tbl.md", dry["candidates"])
        self.assertEqual(dry["removed"], [])
        self.assertTrue((Path(self.dir) / "error_tbl.md").exists())
        done = audit.prune(apply=True)
        self.assertIn("error_tbl.md", done["removed"])
        self.assertFalse((Path(self.dir) / "error_tbl.md").exists())

    def test_auto_prune_on_persist(self):
        # An artifact memory among real ones is auto-pruned by persist().
        recs = [
            MemoryRecord(title="Real", content="We chose Postgres.", type="decision"),
            MemoryRecord(title="junk", content="| col a | col b | col c |", type="error"),
        ]
        distill.persist(recs)
        names = {m.title for m in store.load_all()}
        self.assertIn("Real", names)
        self.assertNotIn("junk", names)

    def test_auto_prune_spares_legit_memory_mentioning_index(self):
        from foldcrumbs import audit
        # A real foldcrumbs design memory mentions MEMORY.md — must NOT be pruned.
        self._write_raw("decision_arch.md", "Dual-layer architecture",
                        "Durable layer is MEMORY.md; live state is HANDOFF.md.", "decision")
        self.assertNotIn("decision_arch.md", audit.audit()["pollution"])
        self.assertEqual(audit.prune_artifacts(), [])
        self.assertTrue((Path(self.dir) / "decision_arch.md").exists())


class TestLifecycle(TmpStore):
    def _make(self, title="Old fact", content="We deploy on Fridays."):
        rec = MemoryRecord(title=title, content=content, type="fact")
        store.write_memory(rec)
        return rec.filename()

    def test_forget_soft_keeps_file_drops_from_index_and_recall(self):
        name = self._make()
        store.rebuild_index()
        self.assertEqual(store.forget(name), "deleted")
        self.assertTrue((Path(self.dir) / name).exists())
        self.assertEqual(store.get(name).status, "deleted")
        self.assertNotIn(name, (Path(self.dir) / "MEMORY.md").read_text())
        self.assertEqual(store.search("deploy fridays"), [])

    def test_forget_hard_removes_file(self):
        name = self._make()
        self.assertEqual(store.forget(name, hard=True), "removed")
        self.assertFalse((Path(self.dir) / name).exists())

    def test_forget_unknown_returns_none(self):
        self.assertIsNone(store.forget("fact_nope.md"))

    def test_supersede_marks_old_and_links_new(self):
        old = self._make("Launch is GitHub only", "PyPI publishing is deferred.")
        new = self._make("Published to PyPI", "foldcrumbs is on PyPI now.")
        self.assertTrue(store.supersede(old, new))
        old_rec, new_rec = store.get(old), store.get(new)
        self.assertEqual(old_rec.status, "superseded")
        self.assertEqual(old_rec.superseded_by, new_rec.id)
        self.assertEqual(old_rec.compute_confidence(), 0.0)
        idx = (Path(self.dir) / "MEMORY.md").read_text()
        self.assertNotIn(old, idx)
        self.assertIn(new, idx)

    def test_supersede_unknown_or_self_fails(self):
        name = self._make()
        self.assertFalse(store.supersede(name, "fact_nope.md"))
        self.assertFalse(store.supersede(name, name))

    def test_forgotten_memory_is_prunable(self):
        from foldcrumbs import audit
        name = self._make()
        store.forget(name)
        res = audit.prune(apply=True)
        self.assertIn(name, res["removed"])
        self.assertFalse((Path(self.dir) / name).exists())


class TestSearch(TmpStore):
    def test_search_ranks_relevant(self):
        store.upsert(MemoryRecord(title="Recall via grep",
                                  content="Recall uses grep, no vector DB.", type="decision"))
        store.upsert(MemoryRecord(title="Atomic writes",
                                  content="Use os.replace.", type="instruction"))
        hits = store.search("vector db", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "Recall via grep")

    def test_search_unicode_words(self):
        # Accented words must survive tokenization ([a-z0-9]+ would split
        # "città" into "citt" and lose the word-overlap match).
        store.upsert(MemoryRecord(title="Config della città",
                                  content="La città usa il fuso orario di Roma.",
                                  type="fact"))
        store.upsert(MemoryRecord(title="Atomic writes",
                                  content="Use os.replace.", type="instruction"))
        hits = store.search("fuso orario città", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "Config della città")

    def test_search_type_filter(self):
        store.upsert(MemoryRecord(title="Grep decision",
                                  content="Recall uses grep.", type="decision"))
        store.upsert(MemoryRecord(title="Grep fact",
                                  content="Recall uses grep too.", type="fact"))
        hits = store.search("grep", limit=5, types=["fact"])
        self.assertEqual([m.title for m in hits], ["Grep fact"])

    def test_search_tag_filter(self):
        store.upsert(MemoryRecord(title="Tagged", content="Recall uses grep.",
                                  type="decision", tags=["arch"]))
        store.upsert(MemoryRecord(title="Untagged", content="Recall uses grep here.",
                                  type="decision"))
        hits = store.search("grep", limit=5, tags=["ARCH"])
        self.assertEqual([m.title for m in hits], ["Tagged"])


class TestHandoff(TmpStore):
    def test_write_read(self):
        store.write_handoff("# Resume point\n\n- You were editing store.py")
        self.assertIn("Resume point", store.read_handoff())

    def test_handoff_not_indexed_or_searched(self):
        store.write_handoff("# Resume point\n\n- secret working state")
        store.upsert(MemoryRecord(title="A fact", content="grep is recall", type="fact"))
        # Handoff file must not appear as a memory.
        titles = [m.title for m in store.load_all()]
        self.assertNotIn("Resume point", titles)
        self.assertEqual(len(store.load_all()), 1)
        idx = store.rebuild_index().read_text()
        self.assertNotIn("HANDOFF", idx)


class TestInstaller(unittest.TestCase):
    def test_merge_preserves_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="ccmem_install_") as d:
            path = Path(d) / "settings.json"
            runtime = Path(d) / "runtime"
            path.write_text(json.dumps({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "node existing.js"}]}]}}))
            changes = install.install_hooks(path, runtime_root=runtime)
            self.assertTrue(changes)
            s = json.loads(path.read_text())
            cmds = [h["command"] for g in s["hooks"]["SessionStart"] for h in g["hooks"]]
            self.assertTrue(any("existing.js" in c for c in cmds))  # preserved
            self.assertTrue(any("session_start.py" in c for c in cmds))  # added
            self.assertTrue((runtime / "foldcrumbs" / "hooks" / "session_start.py").exists())
            self.assertEqual(
                install.install_hooks(path, runtime_root=runtime), []
            )  # idempotent

    def test_install_refreshes_hook_from_protected_checkout(self):
        with tempfile.TemporaryDirectory(prefix="ccmem_install_") as d:
            path = Path(d) / "hooks.json"
            runtime = Path(d) / "runtime"
            source_hook = (
                "/Users/me/Documents/claude/foldcrumbs/"
                "foldcrumbs/hooks/session_start.py"
            )
            path.write_text(json.dumps({"hooks": {"SessionStart": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": f'python3 "{source_hook}"'}],
            }]}}))

            changes = install.install_hooks(
                path, agent="codex", runtime_root=runtime
            )

            self.assertIn("SessionStart -> refreshed session_start.py", changes)
            settings = json.loads(path.read_text())
            commands = [
                hook["command"]
                for groups in settings["hooks"].values()
                for group in groups
                for hook in group["hooks"]
            ]
            self.assertFalse(any("/Documents/" in command for command in commands))
            self.assertTrue(any(str(runtime) in command for command in commands))
            self.assertTrue((runtime / "foldcrumbs" / "config.py").exists())

    def test_codex_mcp_refreshes_editable_install_and_preserves_options(self):
        with tempfile.TemporaryDirectory(prefix="ccmem_install_") as d:
            config_path = Path(d) / "config.toml"
            runtime = Path(d) / "runtime"
            config_path.write_text(
                "model = \"example\"\n\n"
                "[mcp_servers.foldcrumbs]\n"
                "command = \"python3\"\n"
                "args = [\"-m\", \"foldcrumbs.mcp_server\"]\n"
                "enabled = true\n\n"
                "[features]\n"
                "hooks = true\n"
            )

            status = install.install_codex_mcp_toml(config_path, runtime)

            updated = config_path.read_text()
            launcher = runtime / "foldcrumbs_mcp.py"
            self.assertIn("updated", status)
            self.assertIn(f'args = ["{launcher}"]', updated)
            self.assertNotIn('"-m", "foldcrumbs.mcp_server"', updated)
            self.assertIn("enabled = true", updated)
            self.assertIn("[features]", updated)
            self.assertTrue(launcher.exists())
            self.assertEqual(
                install.install_codex_mcp_toml(config_path, runtime),
                "already present",
            )


class TestHooksIsolation(TmpStore):
    def _run_hook(self, script, payload):
        return subprocess.run(
            [sys.executable, str(REPO / "foldcrumbs" / "hooks" / script)],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ}, timeout=30,
        )

    def test_session_start_emits_index(self):
        store.upsert(MemoryRecord(title="X", content="a fact", type="fact"))
        store.rebuild_index()
        r = self._run_hook("session_start.py",
                            {"session_id": "t", "cwd": "/x", "source": "startup"})
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertIn("foldcrumbs-index", out["hookSpecificOutput"]["additionalContext"])

    def test_hook_survives_garbage_stdin(self):
        r = subprocess.run(
            [sys.executable, str(REPO / "foldcrumbs" / "hooks" / "session_start.py")],
            input="not json", capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)


class TestMigration(unittest.TestCase):
    """Rename back-compat + engram -> foldcrumbs migration paths."""

    def test_install_clears_legacy_engram_hook_keeps_foreign(self):
        d = tempfile.mkdtemp(prefix="ccmem_mig_")
        sp = Path(d) / "settings.json"
        sp.write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [{"type": "command",
                "command": "/usr/local/bin/python3 /x/engram/engram/hooks/session_start.py"}]}],
            "PostToolUse": [{"hooks": [{"type": "command",
                "command": "node /y/graphify.js"}]}],
        }}))
        install.install_hooks(sp, "claude")
        s = json.loads(sp.read_text())
        cmds = [h["command"] for ev in s["hooks"].values()
                for g in ev for h in g["hooks"]]
        self.assertFalse(any("engram/hooks" in c for c in cmds))   # legacy gone
        self.assertTrue(any("foldcrumbs/hooks" in c for c in cmds))  # ours added
        self.assertTrue(any("graphify" in c for c in cmds))          # foreign kept

    def test_uninstall_removes_legacy_too(self):
        d = tempfile.mkdtemp(prefix="ccmem_mig_")
        sp = Path(d) / "settings.json"
        sp.write_text(json.dumps({"hooks": {
            "SessionEnd": [{"hooks": [{"type": "command",
                "command": "python3 /x/engram/engram/hooks/session_end.py"}]}],
        }}))
        install.uninstall_hooks(sp)
        s = json.loads(sp.read_text())
        cmds = [h["command"] for ev in s.get("hooks", {}).values()
                for g in ev for h in g["hooks"]]
        self.assertFalse(any("engram" in c for c in cmds))

    def test_foldcrumbs_dir_env_is_primary(self):
        d = tempfile.mkdtemp(prefix="ccmem_fc_")
        os.environ["FOLDCRUMBS_DIR"] = d
        try:
            import importlib
            from foldcrumbs import config as _c
            importlib.reload(_c)
            self.assertEqual(str(_c.memory_dir()), d)
        finally:
            os.environ.pop("FOLDCRUMBS_DIR", None)
            import importlib
            from foldcrumbs import config as _c
            importlib.reload(_c)


if __name__ == "__main__":
    unittest.main()
