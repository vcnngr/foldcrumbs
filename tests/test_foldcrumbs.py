"""Regression tests for foldcrumbs (stdlib unittest, no external deps).

Run: python3 -m unittest discover -s tests
"""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Point every store-locating name at a throwaway directory before the package
# is imported (config resolves STATE_DIR at import time).
#
# Clearing them is not enough, and that is the whole trap: with nothing set,
# STATE_DIR falls back to the real ~/.foldcrumbs. Per-class isolation is not
# enough either — a class that sets only the legacy ENGRAM_* names is silently
# overridden by a FOLDCRUMBS_* one exported in the developer's shell, and the
# suite then writes their actual backend config, runtime snapshot and
# memories. This covers every class, including ones written later that forget
# to isolate themselves.
_SUITE_SANDBOX = tempfile.mkdtemp(prefix="foldcrumbs_suite_")
for _var in ("FOLDCRUMBS_DIR", "ENGRAM_DIR"):
    os.environ.pop(_var, None)
os.environ["FOLDCRUMBS_STATE_DIR"] = str(Path(_SUITE_SANDBOX) / "state")
os.environ.pop("ENGRAM_STATE_DIR", None)
os.environ["CLAUDE_CONFIG_DIR"] = str(Path(_SUITE_SANDBOX) / "config")

from foldcrumbs import distill, install, redact, store  # noqa: E402
from foldcrumbs.schema import MemoryRecord  # noqa: E402


class TestSuiteIsolation(unittest.TestCase):
    """The suite must not be able to touch a real store, however it is run."""

    def test_the_suite_never_resolves_to_a_real_store(self):
        import importlib
        from foldcrumbs import config
        importlib.reload(config)
        for path in (config.STATE_DIR, config.memory_dir(), config.claude_config_dir()):
            self.assertTrue(
                str(path).startswith(_SUITE_SANDBOX),
                f"{path} is outside the suite sandbox {_SUITE_SANDBOX}",
            )

    def test_the_real_state_dir_is_never_written(self):
        # The concrete failure this guards: a class isolating only the legacy
        # names let install.configure_backend() overwrite the developer's real
        # llm-backend choice and stage a runtime snapshot beside it.
        real = Path.home() / ".foldcrumbs"
        before = sorted(p.name for p in real.iterdir()) if real.is_dir() else None
        install.configure_backend("codex", bin_path="/nowhere/codex")
        after = sorted(p.name for p in real.iterdir()) if real.is_dir() else None
        self.assertEqual(before, after)


class TmpStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccmem_test_")
        self._state = tempfile.mkdtemp(prefix="ccmem_test_state_")
        # FOLDCRUMBS_* take precedence over the legacy ENGRAM_* names, so
        # setting only the latter leaves a developer who exported either one
        # pointed at their real store.
        self._saved = {k: os.environ.get(k) for k in
                       ("ENGRAM_DIR", "ENGRAM_STATE_DIR", "CLAUDE_CONFIG_DIR",
                        "FOLDCRUMBS_DIR", "FOLDCRUMBS_STATE_DIR")}
        for k in ("FOLDCRUMBS_DIR", "FOLDCRUMBS_STATE_DIR"):
            os.environ.pop(k, None)
        os.environ["ENGRAM_DIR"] = self.dir
        # Isolate the federation registry too. Without this, search() —
        # federated by default — would consult the developer's real
        # ~/.foldcrumbs and read their actual stores during a test run.
        os.environ["ENGRAM_STATE_DIR"] = self._state
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self._state) / "config")
        import importlib
        from foldcrumbs import config as _c
        importlib.reload(_c)
        # Fail loudly rather than quietly touching real data.
        assert str(Path.home()) not in str(_c.STATE_DIR), "test escaped its sandbox"
        assert str(Path.home()) not in str(_c.memory_dir()), "test escaped its sandbox"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib
        from foldcrumbs import config as _c
        importlib.reload(_c)


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
        # dir instead of snapping back to ~/.foldcrumbs. It must be the
        # FOLDCRUMBS_* name: setting only the legacy ENGRAM_* one is exactly
        # how this class used to write the developer's real state dir whenever
        # a FOLDCRUMBS_STATE_DIR existed to outrank it.
        self._saved_env = {k: os.environ.get(k) for k in
                           ("FOLDCRUMBS_STATE_DIR", "ENGRAM_STATE_DIR")}
        os.environ["FOLDCRUMBS_STATE_DIR"] = str(self._dir)
        os.environ.pop("ENGRAM_STATE_DIR", None)
        importlib.reload(config)
        assert config.STATE_DIR == self._dir, "backend test escaped its temp dir"

    def tearDown(self):
        import importlib
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
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


class TestImportStore(TmpStore):
    def _make_src(self) -> Path:
        src = Path(tempfile.mkdtemp(prefix="ccmem_src_"))
        for rec in (
            MemoryRecord(title="Uses Postgres", content="We use Postgres.",
                         type="decision"),
            MemoryRecord(title="Use stdlib", content="Hooks use only stdlib here.",
                         type="decision"),
        ):
            (src / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        # Noise that must be skipped: index, handoffs, non-frontmatter, non-active.
        (src / "MEMORY.md").write_text("# index\n", encoding="utf-8")
        (src / "HANDOFF.md").write_text("# resume\n", encoding="utf-8")
        (src / "HANDOFF.engram-2026-07-06.md").write_text("# old\n", encoding="utf-8")
        (src / "notes.md").write_text("just a note, no frontmatter\n", encoding="utf-8")
        gone = MemoryRecord(title="Old way", content="We used MySQL.", type="decision")
        gone.status = "superseded"
        (src / gone.filename()).write_text(gone.to_markdown(), encoding="utf-8")
        return src

    def test_dry_run_plans_without_writing(self):
        src = self._make_src()
        # Target already holds a near-duplicate of one source memory.
        store.upsert(MemoryRecord(title="Use stdlib only",
                                  content="Hooks use only stdlib here now.",
                                  type="decision"))
        plan = store.import_store(src)
        self.assertEqual(plan["created"], ["decision_uses_postgres.md"])
        self.assertEqual(plan["validated"], ["decision_use_stdlib.md"])
        self.assertEqual(sorted(plan["skipped"]),
                         ["decision_old_way.md", "notes.md"])
        # Dry-run: nothing was written.
        self.assertEqual(len(store.load_all()), 1)

    def test_apply_merges_and_rebuilds_index(self):
        src = self._make_src()
        plan = store.import_store(src, apply=True)
        self.assertEqual(len(plan["created"]), 2)
        titles = {m.title for m in store.load_all()}
        self.assertEqual(titles, {"Uses Postgres", "Use stdlib"})
        idx = (Path(self.dir) / "MEMORY.md").read_text()
        self.assertIn("Uses Postgres", idx)

    def test_apply_is_idempotent(self):
        src = self._make_src()
        store.import_store(src, apply=True)
        plan = store.import_store(src, apply=True)
        self.assertEqual(plan["created"], [])
        self.assertEqual(len(plan["validated"]), 2)
        self.assertEqual(len(store.load_all()), 2)


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


class TestStoreContainment(TmpStore):
    """Filename-addressed operations must not reach outside the store."""

    def test_get_refuses_to_escape_the_store(self):
        outside = Path(self.dir).parent / "victim.txt"
        outside.write_text("not a memory\n", encoding="utf-8")
        # from_markdown parses any text into an "Untitled" record, so without a
        # containment check this resolves and forget --hard would unlink it.
        for name in ("../victim.txt", str(outside), "sub/../../victim.txt"):
            self.assertIsNone(store.get(name), name)

    def test_hard_forget_cannot_delete_an_outside_file(self):
        outside = Path(self.dir).parent / "victim2.txt"
        outside.write_text("keep me\n", encoding="utf-8")
        self.assertIsNone(store.forget("../victim2.txt", hard=True))
        self.assertTrue(outside.is_file())

    def test_forget_still_works_on_a_real_memory(self):
        rec = MemoryRecord(title="Doomed", content="Body here.", type="fact")
        store.upsert(rec)
        self.assertEqual(store.forget(rec.filename()), "deleted")


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


class TestSurface(unittest.TestCase):
    def setUp(self):
        from foldcrumbs import surface
        self.surface = surface
        self.dir = Path(tempfile.mkdtemp(prefix="ccmem_surface_")) / "commands"

    def test_install_creates_all_commands_with_marker(self):
        actions = self.surface.install_commands(self.dir)
        self.assertEqual(set(actions.values()), {"created"})
        self.assertEqual(set(actions), {"remember.md", "recall.md",
                                        "forget.md", "foldcrumbs.md"})
        for name in actions:
            text = (self.dir / name).read_text(encoding="utf-8")
            self.assertIn(self.surface.MARKER, text)
            self.assertTrue(text.startswith("---"), name)
            self.assertIn("allowed-tools:", text)
        # Frontmatter must stay valid YAML even when the description contains
        # ": " — values are emitted as quoted scalars.
        mem = (self.dir / "foldcrumbs.md").read_text(encoding="utf-8")
        self.assertIn(
            'description: "Project memory dashboard: status, health, resume point"',
            mem)

    def test_reinstall_is_idempotent_and_refreshes_stale(self):
        self.surface.install_commands(self.dir)
        actions = self.surface.install_commands(self.dir)
        self.assertEqual(set(actions.values()), {"unchanged"})
        # A stale managed file (older template) gets refreshed in place.
        stale = self.dir / "recall.md"
        stale.write_text(f"old body\n<!-- {self.surface.MARKER} -->\n",
                         encoding="utf-8")
        actions = self.surface.install_commands(self.dir)
        self.assertEqual(actions["recall.md"], "refreshed")
        self.assertIn("foldcrumbs recall", stale.read_text(encoding="utf-8"))

    def test_user_owned_file_never_touched(self):
        self.dir.mkdir(parents=True)
        mine = self.dir / "remember.md"
        mine.write_text("my own command\n", encoding="utf-8")
        actions = self.surface.install_commands(self.dir)
        self.assertEqual(actions["remember.md"], "skipped (user file)")
        self.assertEqual(mine.read_text(encoding="utf-8"), "my own command\n")
        # Uninstall must not remove it either.
        removed = self.surface.uninstall_commands(self.dir)
        self.assertNotIn("remember.md", removed)
        self.assertTrue(mine.exists())

    def test_uninstall_removes_only_managed(self):
        self.surface.install_commands(self.dir)
        removed = self.surface.uninstall_commands(self.dir)
        self.assertEqual(sorted(removed), ["foldcrumbs.md", "forget.md",
                                           "recall.md", "remember.md"])
        self.assertEqual(list(self.dir.glob("*.md")), [])

    def test_skill_install_refresh_and_user_protection(self):
        d = Path(tempfile.mkdtemp(prefix="ccmem_skill_")) / "skills" / "foldcrumbs"
        self.assertEqual(self.surface.install_skill(d), "created")
        text = (d / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(self.surface.MARKER, text)
        self.assertIn("name: foldcrumbs", text)
        self.assertEqual(self.surface.install_skill(d), "unchanged")
        # Stale managed copy refreshes; user copy is protected.
        (d / "SKILL.md").write_text(f"old\n<!-- {self.surface.MARKER} -->\n",
                                    encoding="utf-8")
        self.assertEqual(self.surface.install_skill(d), "refreshed")
        (d / "SKILL.md").write_text("mine\n", encoding="utf-8")
        self.assertEqual(self.surface.install_skill(d), "skipped (user file)")
        self.assertFalse(self.surface.uninstall_skill(d))
        self.assertTrue((d / "SKILL.md").exists())

    def test_skill_uninstall_removes_managed_and_empty_dir(self):
        d = Path(tempfile.mkdtemp(prefix="ccmem_skill_")) / "skills" / "foldcrumbs"
        self.surface.install_skill(d)
        self.assertTrue(self.surface.uninstall_skill(d))
        self.assertFalse(d.exists())

    def test_codex_prompts_no_frontmatter_and_managed_cycle(self):
        d = Path(tempfile.mkdtemp(prefix="ccmem_codexp_")) / "prompts"
        actions = self.surface.install_codex_prompts(d)
        self.assertEqual(set(actions.values()), {"created"})
        text = (d / "remember.md").read_text(encoding="utf-8")
        self.assertFalse(text.startswith("---"))  # no Claude frontmatter
        self.assertIn(self.surface.MARKER, text)
        self.assertIn("$ARGUMENTS", text)
        # Codex namespaces prompt files: the header must advertise the real
        # invocation name, /prompts:<stem>, not /<stem>.
        self.assertIn("/prompts:remember", text)
        self.assertEqual(set(self.surface.install_codex_prompts(d).values()),
                         {"unchanged"})
        removed = self.surface.uninstall_codex_prompts(d)
        self.assertEqual(len(removed), 4)

    def test_opencode_commands_merge_preserves_user_keys(self):
        import json as _json
        d = Path(tempfile.mkdtemp(prefix="ccmem_ocode_"))
        cfg = d / "opencode.json"
        cfg.write_text(_json.dumps(
            {"mcp": {"x": {}}, "command": {"remember": {"template": "mine"}}}),
            encoding="utf-8")
        added = self.surface.install_opencode_commands(cfg)
        self.assertEqual(
            {n for n, a in added.items() if a == "created"},
            {"foldcrumbs", "forget", "recall"})
        self.assertEqual(added["remember"], "skipped (user command)")
        out = _json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(out["command"]["remember"]["template"], "mine")  # user's
        self.assertIn("foldcrumbs recall", out["command"]["recall"]["template"])
        self.assertIn("x", out["mcp"])  # unrelated config untouched
        # Idempotent; a stale marked template is refreshed on reinstall.
        again = self.surface.install_opencode_commands(cfg)
        self.assertNotIn("created", again.values())
        out["command"]["recall"]["template"] = f"old <!-- {self.surface.MARKER} -->"
        cfg.write_text(_json.dumps(out), encoding="utf-8")
        self.assertEqual(
            self.surface.install_opencode_commands(cfg)["recall"], "refreshed")
        final = _json.loads(cfg.read_text(encoding="utf-8"))
        self.assertIn("foldcrumbs recall", final["command"]["recall"]["template"])
        # Uninstall removes ours, keeps the user's same-name command.
        removed = self.surface.uninstall_opencode_commands(cfg)
        self.assertEqual(sorted(removed), ["foldcrumbs", "forget", "recall"])
        out = _json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(out["command"], {"remember": {"template": "mine"}})

    def test_opencode_uninstall_spares_user_command_mentioning_foldcrumbs(self):
        # Ownership = our marker, NOT the word "foldcrumbs": a user command
        # that happens to call foldcrumbs must survive uninstall.
        import json as _json
        d = Path(tempfile.mkdtemp(prefix="ccmem_ocode_"))
        cfg = d / "opencode.json"
        cfg.write_text(_json.dumps({"command": {
            "recall": {"template": "run foldcrumbs recall and summarize"}}}),
            encoding="utf-8")
        self.assertEqual(self.surface.uninstall_opencode_commands(cfg), [])
        out = _json.loads(cfg.read_text(encoding="utf-8"))
        self.assertIn("recall", out["command"])

    def test_codex_prompts_dir_honours_codex_home(self):
        os.environ["CODEX_HOME"] = "/tmp/fc-codex-home"
        try:
            self.assertEqual(self.surface.codex_prompts_dir(),
                             Path("/tmp/fc-codex-home/prompts"))
        finally:
            os.environ.pop("CODEX_HOME", None)
        self.assertEqual(self.surface.codex_prompts_dir(),
                         Path.home() / ".codex" / "prompts")

    def test_commands_dir_honours_claude_config_dir(self):
        from foldcrumbs import config as cfg
        os.environ["CLAUDE_CONFIG_DIR"] = "/tmp/fc-test-instance"
        try:
            self.assertEqual(self.surface.commands_dir(),
                             Path("/tmp/fc-test-instance/commands"))
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.assertEqual(self.surface.commands_dir(),
                         cfg.claude_config_dir() / "commands")


class TestClaudeMcp(unittest.TestCase):
    def _fake_claude(self, get_rc: int, add_rc: int,
                     get_scope: str = "User", get_extra: str = "") -> str:
        d = Path(tempfile.mkdtemp(prefix="ccmem_mcp_"))
        script = d / "claude"
        script.write_text(
            "#!/bin/sh\n"
            f'if [ "$2" = "get" ]; then echo "Scope: {get_scope} config"; '
            f'echo "Command: {get_extra}"; '
            f"exit {get_rc}; fi\n"
            f'if [ "$2" = "add" ]; then exit {add_rc}; fi\n'
            'if [ "$2" = "remove" ]; then exit 0; fi\n'
            "exit 1\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return str(script)

    def test_missing_cli_returns_manual_snippet(self):
        out = install.install_claude_mcp(claude_bin="/definitely/not/a/claude")
        self.assertIn("mcpServers", out)
        self.assertIn("foldcrumbs", out)

    def test_already_registered_is_idempotent(self):
        # "Already registered" requires scope AND command/args to match.
        rt = Path(tempfile.mkdtemp(prefix="ccmem_rt_"))
        cmd = install._mcp_command(rt)
        out = install.install_claude_mcp(
            runtime_root=rt,
            claude_bin=self._fake_claude(0, 0, get_extra=" ".join(cmd)))
        self.assertEqual(out, "already registered")

    def test_shadowed_scope_is_replaced_on_add_failure(self):
        # get shows the OTHER scope (project shadowed by user); first add
        # fails because the shadowed entry exists -> remove + retry.
        d = Path(tempfile.mkdtemp(prefix="ccmem_mcp_"))
        state = d / "state"
        script = d / "claude"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$2" = "get" ]; then echo "Scope: User config"; exit 0; fi\n'
            f'if [ "$2" = "add" ]; then if [ -f "{state}" ]; then exit 0; '
            f'else touch "{state}"; exit 1; fi; fi\n'
            'if [ "$2" = "remove" ]; then exit 0; fi\n'
            "exit 1\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        out = install.install_claude_mcp(claude_bin=str(script), scope="project")
        self.assertEqual(out, "refreshed (project scope)")

    def test_stale_registration_is_refreshed(self):
        # Same scope but an old interpreter/runtime path -> remove + re-add.
        rt = Path(tempfile.mkdtemp(prefix="ccmem_rt_"))
        out = install.install_claude_mcp(
            runtime_root=rt,
            claude_bin=self._fake_claude(0, 0,
                                         get_extra="/old/python /old/launcher.py"))
        self.assertEqual(out, "refreshed (user scope)")

    def test_registers_at_requested_scope(self):
        out = install.install_claude_mcp(claude_bin=self._fake_claude(1, 0),
                                         scope="project")
        self.assertEqual(out, "registered (project scope)")

    def test_scope_mismatch_still_registers(self):
        # Server exists at user scope; a --local install must still create the
        # project-scoped entry instead of reporting "already registered".
        out = install.install_claude_mcp(
            claude_bin=self._fake_claude(0, 0, get_scope="User"),
            scope="project")
        self.assertEqual(out, "registered (project scope)")

    def test_add_failure_falls_back_to_snippet(self):
        out = install.install_claude_mcp(claude_bin=self._fake_claude(1, 1))
        self.assertIn("failed", out)
        self.assertIn("mcpServers", out)

    def test_uninstall_paths(self):
        self.assertIn("remove manually",
                      install.uninstall_claude_mcp(claude_bin="/not/a/claude"))
        self.assertEqual(install.uninstall_claude_mcp(
            claude_bin=self._fake_claude(0, 0)), "removed")


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


class _FederationEnv(unittest.TestCase):
    """Isolated registry + fake home, so no test touches the real ~/.claude."""

    def setUp(self):
        import importlib
        from foldcrumbs import config as _c, federation as _f
        self.config, self.federation = _c, _f
        self._state = Path(tempfile.mkdtemp(prefix="ccmem_state_"))
        self._home = Path(tempfile.mkdtemp(prefix="ccmem_home_"))
        self._saved = {k: os.environ.get(k) for k in
                       ("ENGRAM_STATE_DIR", "CLAUDE_CONFIG_DIR", "FOLDCRUMBS_DIR",
                        "ENGRAM_DIR", "FOLDCRUMBS_STATE_DIR")}
        os.environ.pop("FOLDCRUMBS_STATE_DIR", None)
        os.environ["ENGRAM_STATE_DIR"] = str(self._state)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self._home / ".claude")
        os.environ.pop("FOLDCRUMBS_DIR", None)
        os.environ.pop("ENGRAM_DIR", None)
        importlib.reload(_c)

    def tearDown(self):
        import importlib
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(self.config)

    def _root(self, name):
        return self._home / name


class TestFederationRegistry(_FederationEnv):
    """Root registry: stable ids, one shard per root, no shared manifest."""

    def test_register_creates_marker_and_shard(self):
        ref = self.federation.register(self._root(".claude"))
        self.assertIsNotNone(ref)
        self.assertTrue((ref.path / self.federation.ROOT_MARKER).is_file())
        self.assertTrue((self._state / "roots" / f"{ref.id}.json").is_file())

    def test_register_is_idempotent_and_keeps_id(self):
        a = self.federation.register(self._root(".claude"))
        b = self.federation.register(self._root(".claude"))
        self.assertEqual(a.id, b.id)
        self.assertEqual(a.registered_at, b.registered_at)
        self.assertEqual(len(list((self._state / "roots").glob("*.json"))), 1)

    def test_id_survives_a_move(self):
        # The id lives in the root, not in a hash of its path: renaming the
        # config dir must not mint a second identity for the same store.
        old = self._root(".claude-work")
        ref = self.federation.register(old)
        new = self._root(".claude-renamed")
        old.rename(new)
        moved = self.federation.register(new)
        self.assertEqual(ref.id, moved.id)
        self.assertEqual(moved.path, new)

    def test_one_shard_per_root_no_shared_manifest(self):
        for name in (".claude", ".claude-work", ".claude-peo"):
            self.federation.register(self._root(name))
        shards = sorted(p.name for p in (self._state / "roots").glob("*.json"))
        self.assertEqual(len(shards), 3)
        labels = {r.label for r in self.federation.iter_roots()}
        self.assertEqual(labels, {"claude", "claude-work", "claude-peo"})

    def test_current_root_sorts_first(self):
        for name in (".claude-work", ".claude", ".claude-peo"):
            self.federation.register(self._root(name))
        roots = self.federation.iter_roots()
        self.assertTrue(roots[0].is_current())
        self.assertEqual(roots[0].label, "claude")

    def test_memory_dir_derives_per_root(self):
        ref = self.federation.register(self._root(".claude-work"))
        got = ref.memory_dir("/tmp/proj")
        self.assertEqual(
            got,
            self._root(".claude-work") / "projects"
            / self.config.encode_cwd("/tmp/proj") / "memory",
        )

    def test_explicit_dir_root_ignores_cwd(self):
        pinned = self._home / "pinned-memory"
        ref = self.federation.register(pinned, mode="explicit")
        self.assertEqual(ref.memory_dir("/tmp/a"), pinned)
        self.assertEqual(ref.memory_dir("/tmp/b"), pinned)

    def test_unregister_hides_root_but_keeps_store(self):
        ref = self.federation.register(self._root(".claude-peo"))
        (ref.path / "keep.txt").write_text("x", encoding="utf-8")
        self.assertTrue(self.federation.unregister(ref.id))
        self.assertIsNone(self.federation.get_root(ref.id))
        self.assertTrue((ref.path / "keep.txt").is_file())

    def test_unavailable_root_is_reported_not_dropped(self):
        # A root we cannot read must stay visible-but-stale, never silently
        # vanish: disappearing entries read as "that memory was deleted".
        ref = self.federation.register(self._root(".claude-work"))
        import shutil
        shutil.rmtree(ref.path)
        again = [r for r in self.federation.iter_roots() if r.id == ref.id]
        self.assertEqual(len(again), 1)
        self.assertFalse(again[0].available())

    def test_empty_root_is_available(self):
        ref = self.federation.register(self._root(".claude-work"))
        self.assertTrue(ref.available())

    def test_corrupt_shard_is_skipped_not_fatal(self):
        self.federation.register(self._root(".claude"))
        (self._state / "roots" / "broken.json").write_text("{not json",
                                                           encoding="utf-8")
        self.assertEqual(len(self.federation.iter_roots()), 1)

    def test_concurrent_marker_creation_agrees_on_one_id(self):
        # Read-then-write would let both processes mint an id and register the
        # same root twice. Creation goes through an atomic link, so the loser
        # adopts the winner's id.
        import threading
        root = self._root(".claude-work")
        root.mkdir(parents=True)
        ids, barrier = [], threading.Barrier(8)

        def worker():
            barrier.wait()
            ids.append(self.federation.ensure_marker(root)["id"])

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ids), 8)
        self.assertEqual(len(set(ids)), 1)

    def test_marker_id_with_traversal_is_rejected(self):
        # An id becomes a filename under roots/; a hand-edited marker holding
        # "../.." must never let a write escape the registry directory.
        root = self._root(".claude")
        root.mkdir(parents=True)
        (root / self.federation.ROOT_MARKER).write_text(
            json.dumps({"id": "../../escape"}), encoding="utf-8")
        self.assertIsNone(self.federation.read_marker(root))
        self.assertFalse(self.federation.valid_id("../../escape"))
        self.assertIsNone(self.federation.shard_path("../../escape"))
        ref = self.federation.register(root)
        self.assertTrue(self.federation.valid_id(ref.id))
        self.assertEqual((self._state / "roots" / f"{ref.id}.json").parent,
                         self._state / "roots")

    def test_relative_path_is_stored_absolute(self):
        cwd = os.getcwd()
        os.chdir(self._home)
        try:
            ref = self.federation.register(Path("./.claude-work"))
        finally:
            os.chdir(cwd)
        self.assertTrue(ref.path.is_absolute())
        self.assertNotIn("..", str(ref.path))

    def test_named_path_is_config_mode_even_under_explicit_dir(self):
        # `roots add ~/.claude-work` while FOLDCRUMBS_DIR is set must not tag
        # that config root as a pinned single-store root.
        os.environ["FOLDCRUMBS_DIR"] = str(self._home / "pinned")
        try:
            ref = self.federation.register(self._root(".claude-work"))
        finally:
            os.environ.pop("FOLDCRUMBS_DIR", None)
        self.assertEqual(ref.mode, "config")

    def test_mode_collision_is_refused(self):
        root = self._root(".claude-work")
        self.federation.register(root, mode="config")
        with self.assertRaises(self.federation.FederationConflict):
            self.federation.register(root, mode="explicit")

    def test_clone_of_live_root_gets_a_new_id(self):
        import shutil
        ref = self.federation.register(self._root(".claude-work"))
        clone = self._root(".claude-copy")
        shutil.copytree(ref.path, clone)
        cloned = self.federation.register(clone)
        self.assertNotEqual(cloned.id, ref.id)
        self.assertEqual(self.federation.read_marker(ref.path), ref.id)
        self.assertEqual(len(self.federation.iter_roots()), 2)

    def test_unregister_current_root_stays_removed(self):
        ref = self.federation.register(self._root(".claude"))
        self.assertTrue(ref.is_current())
        self.assertTrue(self.federation.unregister(ref.id))
        self.assertEqual(
            [r for r in self.federation.iter_roots() if r.id == ref.id], [])
        # The marker survives: leaving the shared view must not cost the root
        # the identity it would need to rejoin with its history intact.
        self.assertTrue((ref.path / self.federation.ROOT_MARKER).is_file())

    def test_unregister_survives_the_repair_path(self):
        # Without a tombstone the removed instance's own next command would
        # quietly put the shard back.
        ref = self.federation.register(self._root(".claude"))
        self.federation.unregister(ref.id)
        self.assertIsNone(self.federation.ensure_registered())
        self.assertEqual(
            [r for r in self.federation.iter_roots() if r.id == ref.id], [])

    def test_explicit_add_revokes_a_removal(self):
        ref = self.federation.register(self._root(".claude"))
        self.federation.unregister(ref.id)
        again = self.federation.register(self._root(".claude"))
        self.assertEqual(again.id, ref.id)
        self.assertFalse(self.federation.is_tombstoned(ref.id))
        self.assertIn(ref.id, [r.id for r in self.federation.iter_roots()])

    def test_unregister_foreign_root_keeps_its_marker(self):
        ref = self.federation.register(self._root(".claude-work"))
        self.assertTrue(self.federation.unregister(ref.id))
        self.assertTrue((ref.path / self.federation.ROOT_MARKER).is_file())
        self.assertTrue(self.federation.is_tombstoned(ref.id))

    def test_symlink_alias_is_not_a_clone(self):
        # Paths are stored unresolved on purpose, so an alias reaches one root
        # under two names. Re-iding the original here would be destructive.
        ref = self.federation.register(self._root(".claude-work"))
        alias = self._root(".claude-alias")
        os.symlink(ref.path, alias)
        same = self.federation.register(alias)
        self.assertEqual(same.id, ref.id)
        self.assertEqual(self.federation.read_marker(ref.path), ref.id)

    def test_marker_publish_failure_registers_nothing(self):
        # A phantom id — returned but never written — would be minted again on
        # the next run, giving one root two shards.
        root = self._root(".claude")
        root.mkdir(parents=True)
        real_link = os.link

        def no_links(src, dst, *a, **kw):
            raise OSError(45, "Operation not supported")

        os.link = no_links
        try:
            self.assertIsNone(self.federation.ensure_marker(root))
            self.assertIsNone(self.federation.register(root))
        finally:
            os.link = real_link
        self.assertFalse((self._state / "roots").exists()
                         and any((self._state / "roots").glob("*.json")))

    def test_marker_creation_is_create_once_under_contention(self):
        # Force every thread to be inside the link window together, so the
        # test proves exclusion rather than happening to serialise.
        import threading
        root = self._root(".claude-work")
        root.mkdir(parents=True)
        real_link, n = os.link, 6
        barrier = threading.Barrier(n)
        outcomes, lock = [], threading.Lock()

        def racing_link(src, dst, *a, **kw):
            barrier.wait()
            try:
                real_link(src, dst, *a, **kw)
            except FileExistsError:
                with lock:
                    outcomes.append("lost")
                raise
            with lock:
                outcomes.append("won")

        ids = []
        os.link = racing_link
        try:
            def worker():
                ids.append(self.federation.ensure_marker(root)["id"])
            threads = [threading.Thread(target=worker) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            os.link = real_link
        self.assertEqual(outcomes.count("won"), 1)
        self.assertEqual(outcomes.count("lost"), n - 1)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(ids[0], self.federation.read_marker(root))

    def test_shard_filename_mismatch_is_skipped(self):
        ref = self.federation.register(self._root(".claude-work"))
        stray = self._state / "roots" / "0123456789abcdef.json"
        stray.write_text(json.dumps(ref.to_dict()), encoding="utf-8")
        ids = [r.id for r in self.federation.iter_roots()]
        self.assertEqual(ids.count(ref.id), 1)
        self.assertNotIn("0123456789abcdef", ids)

    def test_ensure_registered_repairs_missing_shard(self):
        ref = self.federation.register(self._root(".claude"))
        (self._state / "roots" / f"{ref.id}.json").unlink()
        self.assertIsNotNone(self.federation.ensure_registered())
        self.assertTrue((self._state / "roots" / f"{ref.id}.json").is_file())

    def test_ensure_registered_never_opts_in_on_its_own(self):
        # No marker means the user never joined (or explicitly left): a stray
        # CLI call must not resurrect the root.
        self._root(".claude").mkdir(parents=True)
        self.assertIsNone(self.federation.ensure_registered())
        self.assertEqual(list((self._state / "roots").glob("*.json")), [])
        self.assertEqual(self.federation.iter_roots(), [])

    def test_clone_of_a_removed_root_gets_a_new_id(self):
        # Removal deletes the shard, but the original's identity is still live
        # on disk: the tombstone has to carry the path or the copy would claim
        # the original's id.
        import shutil
        ref = self.federation.register(self._root(".claude-work"))
        self.federation.unregister(ref.id)
        clone = self._root(".claude-copy")
        shutil.copytree(ref.path, clone)
        cloned = self.federation.register(clone)
        self.assertNotEqual(cloned.id, ref.id)
        self.assertEqual(self.federation.read_marker(ref.path), ref.id)

    def test_stale_shard_left_by_a_failed_removal_stays_hidden(self):
        ref = self.federation.register(self._root(".claude-work"))
        self.federation.unregister(ref.id)
        # Simulate the tombstone-written / shard-unlink-failed window.
        (self._state / "roots" / f"{ref.id}.json").write_text(
            json.dumps(ref.to_dict()), encoding="utf-8")
        self.assertNotIn(ref.id, [r.id for r in self.federation.iter_roots()])
        self.assertIsNone(self.federation.get_root(ref.id))

    def test_unregister_fails_loudly_when_it_cannot_record_intent(self):
        ref = self.federation.register(self._root(".claude-work"))
        real = self.federation._write_json

        def boom(target, payload):
            if target.name.endswith(".removed"):
                raise OSError("read-only registry")
            return real(target, payload)

        self.federation._write_json = boom
        try:
            self.assertFalse(self.federation.unregister(ref.id))
        finally:
            self.federation._write_json = real
        # Still registered: a removal that cannot be recorded is not a removal.
        self.assertIn(ref.id, [r.id for r in self.federation.iter_roots()])

    def test_unresolvable_identity_refuses_registration(self):
        # Both answers are destructive here: "clone" rewrites a live root's
        # marker, "not a clone" overwrites the original's shard with the wrong
        # path. So neither is taken.
        ref = self.federation.register(self._root(".claude-work"))
        other = self._root(".claude-elsewhere")
        other.mkdir(parents=True)
        (other / self.federation.ROOT_MARKER).write_text(
            (ref.path / self.federation.ROOT_MARKER).read_text(), encoding="utf-8")
        real = os.path.samefile

        def boom(a, b):
            raise OSError("cannot stat")

        os.path.samefile = boom
        try:
            self.assertIsNone(self.federation._detect_clone(ref.id, other))
            self.assertIsNone(self.federation.register(other))
        finally:
            os.path.samefile = real
        # The original's shard still points at the original.
        self.assertEqual(self.federation.get_root(ref.id).path, ref.path)

    def test_unreadable_tombstone_fails_closed(self):
        import shutil
        ref = self.federation.register(self._root(".claude-work"))
        self.federation.unregister(ref.id)
        (self._state / "roots" / f"{ref.id}.removed").write_text(
            "{ truncated", encoding="utf-8")
        clone = self._root(".claude-copy")
        shutil.copytree(ref.path, clone)
        # Cannot tell a copy from a rejoin: refuse rather than hand two live
        # roots the same id.
        self.assertIsNone(self.federation._detect_clone(ref.id, clone))
        self.assertIsNone(self.federation.register(clone))

    def test_mutations_refuse_when_the_registry_cannot_be_locked(self):
        ref = self.federation.register(self._root(".claude"))
        real = self.federation._registry_lock

        @contextlib.contextmanager
        def unlockable():
            yield False

        self.federation._registry_lock = unlockable
        try:
            self.assertIsNone(self.federation.register(self._root(".claude-work")))
            self.assertFalse(self.federation.unregister(ref.id))
            self.assertIsNone(self.federation.ensure_registered())
        finally:
            self.federation._registry_lock = real
        # Nothing moved: the root is still registered, and no new one appeared.
        self.assertEqual([r.id for r in self.federation.iter_roots()], [ref.id])

    def test_mkdir_lock_excludes_and_never_steals(self):
        lockdir = self._state / "roots" / "probe.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05  # don't sit out the real one
        try:
            with self.federation._mkdir_lock(lockdir) as first:
                self.assertTrue(first)
                with self.federation._mkdir_lock(lockdir) as second:
                    self.assertFalse(second)   # already held
            self.assertFalse(lockdir.exists())  # released

            # An old lock is NOT stolen: age cannot tell a dead holder from a
            # slow one, and two live holders lose a tombstone or a shard.
            lockdir.mkdir()
            (lockdir / "owner-someone-else").write_text("x", encoding="utf-8")
            old = time.time() - 86400
            os.utime(lockdir, (old, old))
            with self.federation._mkdir_lock(lockdir) as stolen:
                self.assertFalse(stolen)
            self.assertTrue(lockdir.exists())  # and left alone
        finally:
            self.federation._LOCK_WAIT_SECONDS = wait

    def test_lock_never_displaces_an_existing_holder(self):
        # An empty lock dir means a holder whose marker was deleted by hand,
        # still inside its critical section. It must not be taken over — which
        # is exactly what a rename-based publication would do.
        lockdir = self._state / "roots" / "probe3.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        lockdir.mkdir()
        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05
        try:
            with self.federation._mkdir_lock(lockdir) as held:
                self.assertFalse(held)
        finally:
            self.federation._LOCK_WAIT_SECONDS = wait
        self.assertTrue(lockdir.exists())

    def test_lock_backs_off_when_another_marker_appears(self):
        # The window between mkdir and writing the marker: if a manual removal
        # let someone else in, the late writer must not believe it holds it.
        lockdir = self._state / "roots" / "probe4.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        real_write = Path.write_text

        def intruding_write(self_path, *a, **kw):
            out = real_write(self_path, *a, **kw)
            if self_path.name.startswith("owner-"):
                other = self_path.parent / "owner-someoneelse"
                if not other.exists():
                    real_write(other, "x", encoding="utf-8")
            return out

        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05
        Path.write_text = intruding_write
        try:
            with self.federation._mkdir_lock(lockdir) as held:
                self.assertFalse(held)   # saw the intruder, backed off
        finally:
            Path.write_text = real_write
            self.federation._LOCK_WAIT_SECONDS = wait
        # It withdrew its own marker and left the intruder's alone.
        self.assertEqual([p.name for p in lockdir.glob("owner-*")],
                         ["owner-someoneelse"])

    def test_transient_failure_does_not_strand_the_lock(self):
        # Stale locks are never broken, so anything left behind by a failed
        # acquisition would make the registry permanently unmutatable.
        lockdir = self._state / "roots" / "probe5.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        real_write = Path.write_text

        def failing_write(self_path, *a, **kw):
            if self_path.name.startswith("owner-"):
                raise OSError("no space left on device")
            return real_write(self_path, *a, **kw)

        Path.write_text = failing_write
        try:
            with self.federation._mkdir_lock(lockdir) as held:
                self.assertFalse(held)
        finally:
            Path.write_text = real_write
        self.assertFalse(lockdir.exists())
        # And the next attempt still works.
        with self.federation._mkdir_lock(lockdir) as held:
            self.assertTrue(held)

    def test_mutual_backoff_does_not_strand_the_lock(self):
        # Both contenders withdrawing must not leave an empty directory that
        # nobody can ever take.
        lockdir = self._state / "roots" / "probe6.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        real_write = Path.write_text
        injected = []

        def intruding_write(self_path, *a, **kw):
            out = real_write(self_path, *a, **kw)
            if self_path.name.startswith("owner-") and not injected:
                injected.append(True)
                real_write(self_path.parent / "owner-other", "x", encoding="utf-8")
            return out

        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05
        Path.write_text = intruding_write
        try:
            with self.federation._mkdir_lock(lockdir) as held:
                self.assertFalse(held)
            # The intruder's marker is what keeps the dir alive here; clear it
            # the way the other contender's withdrawal would.
            (lockdir / "owner-other").unlink()
            lockdir.rmdir()
        finally:
            Path.write_text = real_write
            self.federation._LOCK_WAIT_SECONDS = wait
        with self.federation._mkdir_lock(lockdir) as held:
            self.assertTrue(held)

    def test_flock_failure_never_falls_back_to_a_second_mechanism(self):
        # Two lock domains exclude nobody: if flock is available but fails,
        # the answer is "not locked", not "try another lock".
        if self.federation.fcntl is None:
            self.skipTest("no fcntl on this platform")
        real_flock = self.federation.fcntl.flock

        def refuse(*a, **kw):
            raise OSError("flock unavailable")

        self.federation.fcntl.flock = refuse
        try:
            with self.federation._registry_lock() as locked:
                self.assertFalse(locked)
            self.assertFalse((self._state / "roots" / ".lock.d").exists())
        finally:
            self.federation.fcntl.flock = real_flock

    def test_mkdir_lock_does_not_release_someone_elses_lock(self):
        # A holder whose lock was cleared by hand and retaken must not remove
        # the new holder's directory. Ownership lives in the *filename*, so
        # release can never unlink a marker that isn't this holder's own —
        # there is no read-then-unlink window to lose.
        lockdir = self._state / "roots" / "probe2.d"
        lockdir.parent.mkdir(parents=True, exist_ok=True)
        with self.federation._mkdir_lock(lockdir) as held:
            self.assertTrue(held)
            for stale in lockdir.glob("owner-*"):
                stale.unlink()                       # cleared by hand
            (lockdir / "owner-newholder").write_text("x", encoding="utf-8")
        self.assertTrue(lockdir.exists())
        self.assertTrue((lockdir / "owner-newholder").is_file())

    def test_mode_conflict_is_reported(self):
        root = self._root(".claude")
        self.federation.register(root)          # recorded as config
        os.environ["FOLDCRUMBS_DIR"] = str(root)
        try:
            msg = self.federation.mode_conflict()
        finally:
            os.environ.pop("FOLDCRUMBS_DIR", None)
        self.assertIsNotNone(msg)
        self.assertIn("config", msg)

    def test_marker_registry_split_is_detected(self):
        # The invisible case: this instance federated into another state dir,
        # so no scan of *this* registry could ever reveal the other roots.
        root = self._root(".claude")
        self.federation.register(root)
        marker = root / self.federation.ROOT_MARKER
        data = json.loads(marker.read_text())
        data["registry"] = "/elsewhere/.foldcrumbs"
        marker.write_text(json.dumps(data), encoding="utf-8")
        msg = self.federation.state_dir_conflict()
        self.assertIsNotNone(msg)
        self.assertIn("/elsewhere/.foldcrumbs", msg)

    def test_state_dir_conflict_detected(self):
        ref = self.federation.register(self._root(".claude-work"))
        shard = self._state / "roots" / f"{ref.id}.json"
        data = json.loads(shard.read_text())
        data["state_dir"] = "/somewhere/else"
        shard.write_text(json.dumps(data), encoding="utf-8")
        msg = self.federation.state_dir_conflict()
        self.assertIsNotNone(msg)
        self.assertIn("/somewhere/else", msg)

    def test_no_conflict_when_all_agree(self):
        self.federation.register(self._root(".claude"))
        self.federation.register(self._root(".claude-work"))
        self.assertIsNone(self.federation.state_dir_conflict())


class TestIndexShards(_FederationEnv):
    """Per-root index shards and their total ordering."""

    def setUp(self):
        super().setUp()
        from foldcrumbs import index_shard, store
        self.index_shard, self.store = index_shard, store
        self.proj = self._home / "proj"
        self.proj.mkdir(parents=True, exist_ok=True)

    def _memory(self, root, title, body, created, type_="fact"):
        from foldcrumbs.schema import MemoryRecord
        mem_dir = root.memory_dir(self.proj)
        mem_dir.mkdir(parents=True, exist_ok=True)
        rec = MemoryRecord(title=title, content=body, type=type_)
        text = rec.to_markdown().replace(
            f"created_at: {rec.created_at.isoformat()}", f"created_at: {created}")
        (mem_dir / rec.filename()).write_text(text, encoding="utf-8")
        return rec.filename()

    def test_shard_is_written_next_to_the_untouched_local_index(self):
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "Recall is grep", "No vector DB here.",
                     "2026-01-01T00:00:00+00:00")
        index = self.store.rebuild_index(self.proj)
        body = index.read_text(encoding="utf-8")
        shard = self.index_shard.shard_path(ref.id, self.proj)
        self.assertTrue(shard.is_file())
        data = json.loads(shard.read_text())
        self.assertEqual(data["root_id"], ref.id)
        self.assertEqual(len(data["entries"]), 1)
        self.assertTrue(Path(data["entries"][0]["path"]).is_absolute())
        # The local index carries no trace of federation.
        self.assertNotIn(ref.id, body)
        self.assertNotIn("federated", body.lower())

    def test_read_shards_skips_current_and_unregistered_roots(self):
        mine = self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        for ref in (mine, other):
            d = self.index_shard.shards_dir(self.proj)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{ref.id}.json").write_text(json.dumps(
                {"root_id": ref.id, "version": self.index_shard.SHARD_VERSION,
                 "entries": []}), encoding="utf-8")
        got = [s["root_id"] for s in self.index_shard.read_shards(self.proj)]
        self.assertEqual(got, [other.id])          # current one excluded
        self.federation.unregister(other.id)
        self.assertEqual(self.index_shard.read_shards(self.proj), [])

    def test_unavailable_root_keeps_its_entries_flagged(self):
        import shutil
        other = self.federation.register(self._root(".claude-work"))
        d = self.index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{other.id}.json").write_text(json.dumps(
            {"root_id": other.id, "version": self.index_shard.SHARD_VERSION,
             "entries": [{"filename": "a.md"}]}), encoding="utf-8")
        shutil.rmtree(other.path)
        shards = self.index_shard.read_shards(self.proj)
        self.assertEqual(len(shards), 1)
        self.assertFalse(shards[0]["available"])
        self.assertEqual(len(shards[0]["entries"]), 1)  # not dropped

    def test_merge_order_is_total_and_root_independent(self):
        # Same data, shards presented in either order, must merge identically:
        # that is what lets every instance agree without a shared file.
        order = ["decision", "fact"]
        a = {"root_id": "aaaaaaaaaaaaaaaa", "label": "a", "entries": [
            {"filename": "x.md", "type": "fact", "created_at": "2026-01-01T00:00:00+00:00"},
            {"filename": "same.md", "type": "fact", "created_at": "2026-02-01T00:00:00+00:00"},
        ]}
        b = {"root_id": "bbbbbbbbbbbbbbbb", "label": "b", "entries": [
            {"filename": "same.md", "type": "fact", "created_at": "2026-02-01T00:00:00+00:00"},
            {"filename": "d.md", "type": "decision", "created_at": "2020-01-01T00:00:00+00:00"},
        ]}
        one = self.index_shard.merge_entries([a, b], order)
        two = self.index_shard.merge_entries([b, a], order)
        self.assertEqual([(e["root_id"], e["filename"]) for e in one],
                         [(e["root_id"], e["filename"]) for e in two])
        # decision first (type order), then facts newest-first, and the
        # colliding filename is broken by root id — not left to chance.
        self.assertEqual([e["filename"] for e in one],
                         ["d.md", "same.md", "same.md", "x.md"])
        self.assertEqual([e["root_id"] for e in one[1:3]],
                         ["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])

    def test_missing_created_at_uses_a_reproducible_timestamp(self):
        from foldcrumbs.schema import MemoryRecord
        ref = self.federation.register(self._root(".claude"))
        mem_dir = ref.memory_dir(self.proj)
        mem_dir.mkdir(parents=True, exist_ok=True)
        p = mem_dir / "fact_no_date.md"
        p.write_text("---\nname: No date\ntype: fact\n---\n\nBody.\n",
                     encoding="utf-8")
        rec = MemoryRecord.from_markdown(p.read_text(encoding="utf-8"))
        self.assertTrue(rec.created_at_missing)
        first = self.index_shard._stable_created_at(rec, p)
        again = MemoryRecord.from_markdown(p.read_text(encoding="utf-8"))
        self.assertEqual(first, self.index_shard._stable_created_at(again, p))

    def test_naive_timestamps_do_not_break_the_index_sort(self):
        from foldcrumbs.schema import MemoryRecord
        naive = MemoryRecord.from_markdown(
            "---\nname: Old\ntype: fact\ncreated_at: 2020-01-01T00:00:00\n---\n\nB.\n")
        aware = MemoryRecord(title="New", content="B.", type="fact")
        self.assertIsNotNone(naive.created_at.tzinfo)
        sorted([naive, aware], key=lambda m: m.created_at)  # must not raise

    def test_invalid_timestamp_is_treated_as_missing(self):
        from foldcrumbs.schema import MemoryRecord
        text = ("---\nname: Bad date\ntype: fact\ncreated_at: not-a-date\n"
                "---\n\nBody.\n")
        a = MemoryRecord.from_markdown(text)
        b = MemoryRecord.from_markdown(text)
        # Present but unparseable is invented too, and differently each time —
        # so it must be flagged exactly like an absent one.
        self.assertTrue(a.created_at_missing)
        self.assertNotEqual(a.created_at, b.created_at)
        p = self.proj / "bad.md"
        p.write_text(text, encoding="utf-8")
        self.assertEqual(self.index_shard._stable_created_at(a, p),
                         self.index_shard._stable_created_at(b, p))

    def test_paths_stay_absolute_under_a_relative_override(self):
        # Shard entries are read by *other* instances, from *their* cwd: a
        # relative path there would resolve somewhere else entirely.
        cwd = os.getcwd()
        os.chdir(self._home)
        os.environ["FOLDCRUMBS_DIR"] = "relative-memory"
        try:
            self.assertTrue(self.config.memory_dir().is_absolute())
            os.environ["CLAUDE_CONFIG_DIR"] = "relative-config"
            os.environ.pop("FOLDCRUMBS_DIR")
            self.assertTrue(self.config.claude_config_dir().is_absolute())
            self.assertTrue(self.config.memory_dir(self.proj).is_absolute())
        finally:
            os.environ.pop("FOLDCRUMBS_DIR", None)
            os.environ["CLAUDE_CONFIG_DIR"] = str(self._home / ".claude")
            os.chdir(cwd)

    def test_duplicate_entries_do_not_depend_on_arrival_order(self):
        order = ["fact"]
        dup = {"root_id": "aaaaaaaaaaaaaaaa", "label": "a", "entries": [
            {"filename": "x.md", "type": "fact", "title": "first",
             "created_at": "2026-01-01T00:00:00+00:00"},
            {"filename": "x.md", "type": "fact", "title": "second",
             "created_at": "2020-01-01T00:00:00+00:00"},
        ]}
        rows = self.index_shard.merge_entries([dup], order)
        flipped = dict(dup, entries=list(reversed(dup["entries"])))
        other = self.index_shard.merge_entries([flipped], order)
        self.assertEqual(len(rows), 1)
        # Not just the count: the *same* record must survive either way, or
        # its title and timestamp — and so its position — depend on arrival.
        self.assertEqual(rows, other)
        self.assertEqual(rows[0]["title"], "first")   # newest wins, by content

    def test_shard_validation_rejects_every_malformed_shape(self):
        other = self.federation.register(self._root(".claude-work"))
        d = self.index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        target = d / f"{other.id}.json"
        good = {"root_id": other.id, "version": self.index_shard.SHARD_VERSION,
                "entries": []}
        for broken in (
            {k: v for k, v in good.items() if k != "version"},   # no version
            dict(good, version="1"),                             # not an int
            dict(good, version=self.index_shard.SHARD_VERSION + 1),
            {k: v for k, v in good.items() if k != "entries"},   # no entries
            dict(good, entries={"not": "a list"}),
            dict(good, root_id="ffffffffffffffff"),              # id mismatch
        ):
            target.write_text(json.dumps(broken), encoding="utf-8")
            self.assertEqual(self.index_shard.read_shards(self.proj), [],
                             f"accepted malformed shard: {broken}")
        target.write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(len(self.index_shard.read_shards(self.proj)), 1)

    def test_shard_from_a_newer_format_is_skipped(self):
        other = self.federation.register(self._root(".claude-work"))
        d = self.index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{other.id}.json").write_text(json.dumps({
            "root_id": other.id, "version": self.index_shard.SHARD_VERSION + 1,
            "entries": [{"filename": "a.md"}]}), encoding="utf-8")
        self.assertEqual(self.index_shard.read_shards(self.proj), [])

    def _publish(self, ref, entries, **extra):
        d = self.index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        payload = {"root_id": ref.id, "version": self.index_shard.SHARD_VERSION,
                   "label": ref.label, "memory_dir": str(ref.memory_dir(self.proj)),
                   "entries": entries}
        payload.update(extra)
        (d / f"{ref.id}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_rendered_block_announces_paths_and_read_only(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [{"filename": "d.md", "type": "decision",
                               "title": "Recall is grep", "description": "No vector DB.",
                               "path": "/abs/d.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}])
        block = self.index_shard.render_block(self.proj, ["decision", "fact"])
        self.assertIn("<foldcrumbs-federated>", block)
        self.assertIn("claude-work", block)
        self.assertIn("/abs/d.md", block)          # greppable
        self.assertIn("READ-ONLY", block)          # ownership stated
        self.assertIn(str(other.memory_dir(self.proj)), block)

    def test_rendered_block_is_empty_without_other_instances(self):
        self.federation.register(self._root(".claude"))
        self.assertEqual(self.index_shard.render_block(self.proj, ["fact"]), "")

    def test_rendered_block_caps_and_says_what_it_dropped(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [
            {"filename": f"m{i:03d}.md", "type": "fact", "title": f"M{i}",
             "path": f"/abs/m{i:03d}.md",
             "created_at": f"2026-01-01T00:{i:02d}:00+00:00"}
            for i in range(50)
        ])
        block = self.index_shard.render_block(
            self.proj, ["fact"], max_entries=10)
        self.assertEqual(block.count("[claude-work]"), 10)
        # "further", not "older": type rank outranks date in the sort, so what
        # falls off the end is not necessarily the oldest.
        self.assertIn("40 further entries not shown", block)

    def test_rendered_block_flags_an_unreachable_root(self):
        import shutil
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [{"filename": "a.md", "type": "fact", "title": "A",
                               "path": "/abs/a.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}])
        shutil.rmtree(other.path)
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertIn("UNREACHABLE", block)
        self.assertIn("A", block)   # entries kept, not silently dropped

    def test_rendered_block_reports_a_stale_shard(self):
        other = self.federation.register(self._root(".claude-work"))
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        self._publish(other, [{"filename": "a.md", "type": "fact", "title": "A",
                               "path": "/abs/a.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}],
                      written_at=old)
        self.assertIn("last published 90d ago",
                      self.index_shard.render_block(self.proj, ["fact"]))

    def test_one_malformed_entry_does_not_erase_the_whole_view(self):
        # The hook swallows exceptions, so a TypeError in the sort would not
        # degrade the federated view — it would blank it.
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [
            {"filename": "bad.md", "type": "fact", "title": "Bad",
             "created_at": ["not", "a", "string"], "path": "/abs/bad.md"},
            {"filename": None},
            "not even a dict",
            {"filename": "good.md", "type": "fact", "title": "Good",
             "path": "/abs/good.md", "created_at": "2026-01-01T00:00:00+00:00"},
        ])
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertIn("Good", block)
        self.assertIn("Bad", block)     # kept, with its bad field coerced away
        rows = self.index_shard.merge_entries(
            self.index_shard.read_shards(self.proj), ["fact"])
        self.assertEqual({r["filename"] for r in rows}, {"bad.md", "good.md"})

    def test_availability_probe_is_time_bounded(self):
        other = self.federation.register(self._root(".claude-work"))
        real = type(other).available

        def hang(self_ref):
            time.sleep(5)
            return True

        type(other).available = hang
        try:
            start = time.monotonic()
            got = other.available_within(0.05)
        finally:
            type(other).available = real
        self.assertIsNone(got)                       # no answer, not a guess
        self.assertLess(time.monotonic() - start, 2)  # and it did not hang

    def test_unknown_publication_date_is_stated(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [{"filename": "a.md", "type": "fact", "title": "A",
                               "path": "/abs/a.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}])
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertIn("never reported when it was published", block)

    def test_long_entries_are_capped_by_size_too(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [
            {"filename": f"m{i}.md", "type": "fact", "title": f"M{i}",
             "description": "x" * 4000, "path": f"/abs/m{i}.md",
             "created_at": f"2026-01-01T00:{i:02d}:00+00:00"}
            for i in range(10)
        ])
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertLess(len(block), 20000)
        self.assertIn("further entries not shown", block)

    def test_shard_is_published_on_first_federated_read(self):
        # Federating an existing store must not look empty to everyone else
        # until its owner happens to write something.
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "Pre-existing", "Recorded before federating.",
                     "2026-01-01T00:00:00+00:00")
        shard = self.index_shard.shard_path(ref.id, self.proj)
        self.assertFalse(shard.exists())        # nothing rebuilt yet
        self.index_shard.ensure_shard(self.proj)
        self.assertTrue(shard.is_file())
        self.assertEqual(len(json.loads(shard.read_text())["entries"]), 1)

    def test_ensure_shard_does_not_republish_a_current_one(self):
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        first = self.index_shard.ensure_shard(self.proj)
        stamp = first.stat().st_mtime
        # Returns the existing shard without rewriting it: republishing on
        # every session would churn the file for no reader benefit.
        self.assertEqual(self.index_shard.ensure_shard(self.proj), first)
        self.assertEqual(first.stat().st_mtime, stamp)

    def test_the_store_is_read_while_the_lock_is_held(self):
        # The invariant that makes a stale overwrite impossible: entries are
        # scanned inside the critical section, so no other process can publish
        # between reading the store and writing the shard. Every version that
        # scanned first and checked afterwards had a blind spot.
        import contextlib as _c
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "First", "Body one.", "2026-01-01T00:00:00+00:00")
        real_lock = self.federation.file_lock
        added = []

        @_c.contextmanager
        def lock_then_mutate(path):
            with real_lock(path) as held:
                if held and not added:
                    # A write landing exactly here is invisible to any scan
                    # taken before the lock; it must be visible to this one.
                    added.append(self._memory(ref, "Second", "Body two.",
                                              "2026-01-02T00:00:00+00:00"))
                yield held

        self.federation.file_lock = lock_then_mutate
        try:
            self.index_shard.write_shard(self.proj)
        finally:
            self.federation.file_lock = real_lock
        shard = self.index_shard.shard_path(ref.id, self.proj)
        titles = {e["title"] for e in json.loads(shard.read_text())["entries"]}
        self.assertEqual(titles, {"First", "Second"})

    def test_publishing_does_not_hold_the_machine_wide_registry_lock(self):
        # The scan runs inside the lock, so a large store held the registry
        # long enough to stall every other instance's SessionStart. Only this
        # instance's own processes race for this shard.
        import contextlib as _c
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        taken = []
        real = self.federation.file_lock

        @_c.contextmanager
        def record(path):
            taken.append(Path(path))
            with real(path) as held:
                yield held

        self.federation.file_lock = record
        try:
            self.index_shard.write_shard(self.proj)
        finally:
            self.federation.file_lock = real
        self.assertEqual(len(taken), 1)
        self.assertEqual(taken[0].parent, self.index_shard.shards_dir(self.proj))
        self.assertNotEqual(taken[0].parent, self.federation.roots_dir())
        self.assertIn(ref.id, taken[0].name)

    def test_a_lock_held_elsewhere_does_not_hang_the_hook(self):
        # A bounded wait: an editor that will not start is worse than a
        # publish that waits for the next session.
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        lock = self.index_shard.shards_dir(self.proj) / f".lock-{ref.id}"
        wait = self.federation._LOCK_WAIT_SECONDS
        self.federation._LOCK_WAIT_SECONDS = 0.05
        try:
            with self.federation.file_lock(lock) as held:
                self.assertTrue(held)
                start = time.monotonic()
                self.assertIsNone(self.index_shard.write_shard(self.proj))
                self.assertLess(time.monotonic() - start, 3)
        finally:
            self.federation._LOCK_WAIT_SECONDS = wait

    def test_a_filesystem_that_cannot_lock_fails_fast(self):
        # ENOLCK will never become success, so retrying it spends the whole
        # deadline on the session-start path for nothing.
        import errno as _e
        if self.federation.fcntl is None:
            self.skipTest("no fcntl on this platform")
        lock = self.index_shard.shards_dir(self.proj) / ".lock-probe"
        real = self.federation.fcntl.flock

        def unsupported(*a, **kw):
            raise OSError(_e.ENOLCK, "no locks available")

        self.federation.fcntl.flock = unsupported
        try:
            start = time.monotonic()
            with self.federation.file_lock(lock) as held:
                self.assertFalse(held)
            elapsed = time.monotonic() - start
        finally:
            self.federation.fcntl.flock = real
        self.assertLess(elapsed, self.federation._LOCK_WAIT_SECONDS / 2)

    def test_contention_is_still_waited_out(self):
        # The counterpart: a lock merely held by someone else must be retried,
        # not abandoned on the first refusal.
        import errno as _e
        if self.federation.fcntl is None:
            self.skipTest("no fcntl on this platform")
        lock = self.index_shard.shards_dir(self.proj) / ".lock-probe2"
        real = self.federation.fcntl.flock
        calls = []

        def busy_then_free(fd, op):
            calls.append(op)
            if len(calls) < 3:
                raise OSError(_e.EWOULDBLOCK, "would block")
            return real(fd, op)

        self.federation.fcntl.flock = busy_then_free
        try:
            with self.federation.file_lock(lock) as held:
                self.assertTrue(held)
        finally:
            self.federation.fcntl.flock = real
        self.assertGreaterEqual(len(calls), 3)

    def test_a_shard_already_matching_the_store_is_left_alone(self):
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        first = self.index_shard.write_shard(self.proj)
        stamp = first.stat().st_mtime
        self.assertEqual(self.index_shard.write_shard(self.proj), first)
        self.assertEqual(first.stat().st_mtime, stamp)   # no churn

    def test_an_edit_that_preserves_size_and_mtime_still_republishes(self):
        # A one-character change keeps the size; a restore or a sync can keep
        # the mtime. Deciding by stat alone would leave the old title
        # published for good, so the entries themselves are compared.
        ref = self.federation.register(self._root(".claude"))
        name = self._memory(ref, "Deploys run on Mondays", "Body.",
                            "2026-01-01T00:00:00+00:00")
        self.index_shard.write_shard(self.proj)
        shard = self.index_shard.shard_path(ref.id, self.proj)
        path = ref.memory_dir(self.proj) / name
        before = path.stat()
        path.write_text(path.read_text(encoding="utf-8")
                        .replace("Mondays", "Fridays"), encoding="utf-8")
        # ns, not float seconds: restoring with float leaves st_mtime_ns
        # different, which would move the stat signature and let this test
        # pass without the fix it exists to prove.
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.index_shard.ensure_shard(self.proj)
        titles = [e["title"] for e in json.loads(shard.read_text())["entries"]]
        self.assertEqual(titles, ["Deploys run on Fridays"])

    def test_deleting_a_memory_reaches_the_shard(self):
        # The version has to change when a store *shrinks*: a newest-mtime
        # counter goes down on delete, so the removed memory would stay
        # published forever.
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "Keep", "Body one.", "2026-01-01T00:00:00+00:00")
        name = self._memory(ref, "Drop", "Body two.", "2026-01-02T00:00:00+00:00")
        self.index_shard.write_shard(self.proj)
        shard = self.index_shard.shard_path(ref.id, self.proj)
        self.assertEqual(len(json.loads(shard.read_text())["entries"]), 2)
        (ref.memory_dir(self.proj) / name).unlink()
        self.index_shard.ensure_shard(self.proj)
        titles = [e["title"] for e in json.loads(shard.read_text())["entries"]]
        self.assertEqual(titles, ["Keep"])

    def test_shard_is_not_published_without_the_lock(self):
        import contextlib as _c
        ref = self.federation.register(self._root(".claude"))
        self._memory(ref, "A", "Body.", "2026-01-01T00:00:00+00:00")
        real = self.federation.file_lock

        @_c.contextmanager
        def unlockable(path):
            yield False

        self.federation.file_lock = unlockable
        try:
            self.assertIsNone(self.index_shard.write_shard(self.proj))
        finally:
            self.federation.file_lock = real
        self.assertFalse(self.index_shard.shard_path(ref.id, self.proj).exists())

    def test_federated_scan_reads_a_bounded_number_of_files(self):
        from foldcrumbs import store
        other = self.federation.register(self._root(".claude-work"))
        d = other.memory_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        cap = store._MAX_FEDERATED_SCAN
        reads = []
        real_read = Path.read_text

        def counting_read(self_path, *a, **kw):
            if self_path.suffix == ".md":
                reads.append(self_path.name)
            return real_read(self_path, *a, **kw)

        for i in range(cap + 25):
            # Unparseable on purpose: a file that fails to become a record
            # still cost a read, which is what the cap has to bound.
            (d / f"m{i:04d}.md").write_text("not frontmatter", encoding="utf-8")
        Path.read_text = counting_read
        try:
            list(store.iter_federated(self.proj))
        finally:
            Path.read_text = real_read
        self.assertLessEqual(len(reads), cap)

    def test_a_single_huge_entry_cannot_blow_the_size_cap(self):
        other = self.federation.register(self._root(".claude-work"))
        self._publish(other, [{"filename": "big.md", "type": "fact",
                               "title": "T" * 200, "description": "x" * 500000,
                               "path": "/abs/big.md",
                               "created_at": "2026-01-01T00:00:00+00:00"}])
        block = self.index_shard.render_block(self.proj, ["fact"])
        self.assertLess(len(block), self.index_shard._MAX_FEDERATED_CHARS + 2000)
        self.assertIn("/abs/big.md", block)   # the path survives the cut

    def test_unfederated_root_publishes_nothing(self):
        self._root(".claude").mkdir(parents=True)   # marker never created
        self.assertIsNone(self.index_shard.write_shard(self.proj))


class TestFederatedSearch(_FederationEnv):
    """Federated recall: the path OpenCode and Codex depend on."""

    def setUp(self):
        super().setUp()
        from foldcrumbs import store
        self.store = store
        self.proj = self._home / "proj"
        self.proj.mkdir(parents=True, exist_ok=True)

    def _write(self, ref, title, content, type_="fact"):
        from foldcrumbs.schema import MemoryRecord
        d = ref.memory_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        rec = MemoryRecord(title=title, content=content, type=type_)
        (d / rec.filename()).write_text(rec.to_markdown(), encoding="utf-8")
        return rec

    def test_search_finds_another_instances_memory(self):
        mine = self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(mine, "Local note", "Something about caching.")
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        hits = self.store.search("deploys on fridays", cwd=self.proj)
        self.assertTrue(hits)
        self.assertEqual(hits[0].title, "Deploy window")
        self.assertTrue(hits[0].is_foreign)
        self.assertEqual(hits[0].origin_root, "claude-work")
        self.assertTrue(Path(hits[0].origin_path).is_absolute())

    def test_search_can_stay_local(self):
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        self.assertEqual(
            self.store.search("fridays", cwd=self.proj, federated=False), [])

    def test_local_memory_wins_an_exact_tie(self):
        # Both scored identically, the actionable one first: only the local
        # copy can be forgotten or superseded from here.
        mine = self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        for ref in (mine, other):
            self._write(ref, "Same note", "Identical content here.")
        hits = self.store.search("identical content", cwd=self.proj)
        self.assertFalse(hits[0].is_foreign)
        self.assertTrue(hits[1].is_foreign)

    def test_unregistered_instance_is_not_searched(self):
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        self.federation.unregister(other.id)
        self.assertEqual(self.store.search("fridays", cwd=self.proj), [])

    def test_foreign_results_are_labelled_read_only(self):
        from foldcrumbs.profile import format_context_block
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        block = format_context_block(
            self.store.search("fridays", cwd=self.proj), heading="deploy")
        self.assertIn("from claude-work", block)
        self.assertIn("read-only", block)

    def test_write_paths_refuse_a_foreign_record(self):
        # The rendered blocks tell the model these are read-only; this is the
        # part that does not depend on the model believing it.
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Theirs", "Foreign body.")
        foreign = self.store.search("foreign body", cwd=self.proj)[0]
        self.assertTrue(foreign.is_foreign)
        for call in (
            lambda: self.store.write_memory(foreign, self.proj),
            lambda: self.store.upsert(foreign, self.proj),
            lambda: self.store.mark_superseded_on_disk(foreign, "x", self.proj),
        ):
            with self.assertRaises(self.store.ForeignMemoryError):
                call()
        # And nothing was created in this root as a side effect.
        self.assertEqual(list(self.store.iter_memories(self.proj)), [])

    def test_contradiction_with_a_foreign_memory_is_asserted_locally(self):
        from foldcrumbs import distill
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        stale = self._write(other, "PyPI publishing deferred",
                            "Publishing to PyPI is deferred for now.",
                            type_="decision")
        fresh = MemoryRecord(title="Published to PyPI",
                             content="Publishing to PyPI is done and released.",
                             type="fact")
        calls = []
        real_chat = distill.llm.chat

        def yes(*a, **kw):
            calls.append(1)
            return '{"supersedes": true}'

        distill.llm.chat = yes
        try:
            n = distill._auto_supersede([fresh], self.proj)
        finally:
            distill.llm.chat = real_chat
        self.assertEqual(n, 1)
        # Their file is untouched — only their instance may retire it.
        theirs = (other.memory_dir(self.proj) / stale.filename()).read_text()
        self.assertNotIn("status: superseded", theirs)
        # The claim is recorded on our own memory, and survives a round-trip.
        mine = list(self.store.iter_memories(self.proj))
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].supersedes_external,
                         [f"{other.id}:{stale.filename()}"])

    def test_several_foreign_claims_are_all_kept(self):
        # A single field would silently drop every claim after the first.
        from foldcrumbs import distill
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        a = self.federation.register(self._root(".claude-work"))
        b = self.federation.register(self._root(".claude-peo"))
        s1 = self._write(a, "Deploy deferred", "Deploying is deferred for now.",
                         type_="decision")
        s2 = self._write(b, "Deploy postponed", "Deploying is postponed.",
                         type_="decision")
        fresh = MemoryRecord(title="Deployed",
                             content="Deploying is done and released now.",
                             type="fact")
        real_chat = distill.llm.chat
        distill.llm.chat = lambda *a, **k: '{"supersedes": true}'
        try:
            distill._auto_supersede([fresh], self.proj)
        finally:
            distill.llm.chat = real_chat
        claims = set(list(self.store.iter_memories(self.proj))[0].supersedes_external)
        self.assertEqual(claims, {f"{a.id}:{s1.filename()}",
                                  f"{b.id}:{s2.filename()}"})

    def test_claims_are_not_attributed_by_label(self):
        # Two instances can share a label: "~/a/.claude" and "~/b/.claude" are
        # both "claude". Keying on the label would contest the wrong memory.
        from foldcrumbs import index_shard
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        (self._home / "a").mkdir(exist_ok=True)
        (self._home / "b").mkdir(exist_ok=True)
        one = self.federation.register(self._home / "a" / ".claude")
        two = self.federation.register(self._home / "b" / ".claude")
        self.assertEqual(one.label, two.label)     # the ambiguity is real
        target = self._write(two, "Deploy Mondays", "Deploys run Mondays.")
        claim = MemoryRecord(title="Moved to Friday", content="Now Fridays.",
                             type="fact")
        claim.supersedes_external = [f"{two.id}:{target.filename()}"]
        self.store.write_memory(claim, self.proj)
        d = index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        for ref in (one, two):
            (d / f"{ref.id}.json").write_text(json.dumps({
                "root_id": ref.id, "version": index_shard.SHARD_VERSION,
                "label": ref.label, "entries": [
                    {"filename": target.filename(), "type": "fact",
                     "title": f"Deploy Mondays ({ref.id[:4]})",
                     "path": "/abs/x.md",
                     "created_at": "2026-01-01T00:00:00+00:00"}]}),
                encoding="utf-8")
        block = index_shard.render_block(self.proj, ["fact"])
        # Exactly one of the two same-named roots is contested — the right one.
        self.assertEqual(block.count("your store records this as obsolete"), 1)
        contested_line = [ln for ln in block.splitlines()
                          if "records this as obsolete" in ln][0]
        self.assertIn(two.id[:4], contested_line)

    def test_federated_view_shows_a_contested_entry(self):
        from foldcrumbs import index_shard
        from foldcrumbs.schema import MemoryRecord
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        stale = self._write(other, "Deploy on Mondays", "Deploys run Mondays.")
        claim = MemoryRecord(title="Deploys moved to Friday",
                             content="Deploys now run on Fridays.", type="fact")
        claim.supersedes_external = [f"{other.id}:{stale.filename()}"]
        self.store.write_memory(claim, self.proj)
        self.store.rebuild_index(self.proj)
        d = index_shard.shards_dir(self.proj)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{other.id}.json").write_text(json.dumps({
            "root_id": other.id, "version": index_shard.SHARD_VERSION,
            "label": "claude-work", "entries": [
                {"filename": stale.filename(), "type": "fact",
                 "title": "Deploy on Mondays", "path": "/abs/x.md",
                 "created_at": "2026-01-01T00:00:00+00:00"}]}),
            encoding="utf-8")
        block = index_shard.render_block(self.proj, ["fact"])
        self.assertIn("your store records this as obsolete", block)
        self.assertIn("Deploys moved to Friday", block)

    def test_answer_attributes_foreign_memories(self):
        # Without the label the model can present another instance's
        # conclusion as this store's own.
        from foldcrumbs import cli, llm
        self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(other, "Deploy window", "Deploys run on Fridays only.")
        seen = {}
        real = llm.chat

        def capture(messages, **kw):
            seen["prompt"] = messages[-1]["content"]
            return "ok"

        llm.chat = capture
        cwd = os.getcwd()
        os.chdir(self.proj)
        try:
            import argparse
            cli._cmd_answer(argparse.Namespace(question="when do deploys run",
                                               limit=5))
        finally:
            llm.chat = real
            os.chdir(cwd)
        self.assertIn("from claude-work", seen["prompt"])
        self.assertIn("read-only", seen["prompt"])

    def test_iter_memories_stays_local(self):
        # The write paths are all built on it; federating it would let a
        # foreign record be validated or superseded under this root.
        mine = self.federation.register(self._root(".claude"))
        other = self.federation.register(self._root(".claude-work"))
        self._write(mine, "Mine", "Local body.")
        self._write(other, "Theirs", "Foreign body.")
        titles = {m.title for m in self.store.iter_memories(self.proj)}
        self.assertEqual(titles, {"Mine"})
        dup = self.store.find_duplicate(
            self._write(other, "Theirs", "Foreign body."), cwd=self.proj)
        self.assertIsNone(dup)   # never matches across roots


class TestRootsCLI(_FederationEnv):
    """`foldcrumbs roots` end-to-end, on the same isolated registry."""

    def _run(self, *argv):
        import contextlib
        import io
        from foldcrumbs import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_add_list_remove_roundtrip(self):
        code, out = self._run("roots", "add", str(self._root(".claude-work")))
        self.assertEqual(code, 0)
        self.assertIn("registered claude-work", out)

        _, out = self._run("roots")
        self.assertIn("claude-work", out)
        rid = [r.id for r in self.federation.iter_roots()
               if r.label == "claude-work"][0]

        code, out = self._run("roots", "remove", rid)
        self.assertEqual(code, 0)
        self.assertIn("store is untouched", out)
        _, out = self._run("roots")
        self.assertNotIn("claude-work", out)

    def test_remove_unknown_id_fails_cleanly(self):
        code, out = self._run("roots", "remove", "deadbeefdeadbeef")
        self.assertEqual(code, 1)
        self.assertIn("no registered root", out)

    def test_mode_collision_is_reported_not_raised(self):
        self._run("roots", "add", str(self._root(".claude-work")))
        code, out = self._run("roots", "add", str(self._root(".claude-work")),
                              "--mode", "explicit")
        self.assertEqual(code, 1)
        self.assertIn("refused:", out)

    def test_cli_repairs_a_missing_shard(self):
        self._run("roots", "add", str(self._root(".claude")))
        rid = self.federation.read_marker(self._root(".claude"))
        (self._state / "roots" / f"{rid}.json").unlink()
        self._run("roots")  # any command triggers the repair
        self.assertTrue((self._state / "roots" / f"{rid}.json").is_file())

    def test_cli_does_not_resurrect_a_removed_root(self):
        self._run("roots", "add", str(self._root(".claude")))
        rid = self.federation.read_marker(self._root(".claude"))
        self._run("roots", "remove", rid)
        _, out = self._run("roots")
        self.assertFalse((self._state / "roots" / f"{rid}.json").exists())
        self.assertIn("no federated roots", out)


if __name__ == "__main__":
    unittest.main()
