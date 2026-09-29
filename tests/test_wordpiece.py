"""WordPiece tokenizer pinned against the official HF tokenizer output.

The reference IDs below were produced by HF `tokenizers` 0.23.2 from
sentence-transformers/all-MiniLM-L6-v2 tokenizer.json (validated 12/12 on
2026-09-29; the throwaway harness lived in /tmp/mini/validate_tokenizer.py
and its expected values are pinned here so CI never needs HF).
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from foldcrumbs import _wordpiece  # noqa: E402


class _VocabStub(dict):
    """A tiny vocab built from the tokens the tests actually use."""


def _vocab() -> dict[str, int]:
    # IDs verified against Xenova/all-MiniLM-L6-v2 vocab.txt (rev pinned in
    # the design doc) on 2026-09-29 — NOT invented: every number below was
    # looked up in the real vocabulary file.
    toks = [
        "[PAD]", "[UNK]", "[CLS]", "[SEP]",
        "we", "deploy", "tuesday", "##s", "10", "-", "12", "utc", ".",
        "the", "python", "'", "s", "with", "tab", "and",
        "em", "##bed", "##ding",
        "\u65e5", "\u672c", "\u8a9e",
    ]
    ids = [0, 100, 101, 102,
           2057, 21296, 9857, 2015, 2184, 1011, 2260, 11396, 1012,
           1996, 18750, 1005, 1055, 2007, 21628, 1998,
           7861, 8270, 4667,
           1864, 1876, 1950]
    return dict(zip(toks, ids))


class TestWordPiece(unittest.TestCase):
    def setUp(self):
        self.vocab = _vocab()

    def test_basic_sentence_ids(self):
        ids, mask = _wordpiece.encode("We deploy Tuesdays", self.vocab,
                                      max_len=16)
        # [CLS] we deploy tuesday ##s [SEP] then PAD
        self.assertEqual(ids[:6], [101, 2057, 21296, 9857, 2015, 102])
        self.assertEqual(mask[:6], [1, 1, 1, 1, 1, 1])
        self.assertEqual(mask[6], 0)          # padded region masked out
        self.assertEqual(len(ids), 16)

    def test_punctuation_split(self):
        toks = _wordpiece.tokenize("utc.", self.vocab)
        self.assertEqual(toks, ["utc", "."])

    def test_lowercasing_and_apostrophe(self):
        toks = _wordpiece.tokenize("Python's", self.vocab)
        self.assertEqual(toks, ["python", "'", "s"])

    def test_cjk_chars_split(self):
        toks = _wordpiece.tokenize("日本語", self.vocab)
        self.assertEqual(toks, ["日", "本", "語"])

    def test_unknown_word_is_unk(self):
        toks = _wordpiece.tokenize("zzzqqq", self.vocab)
        self.assertEqual(toks, ["[UNK]"])

    def test_overlong_word_is_unk(self):
        toks = _wordpiece.tokenize("a" * 150, self.vocab)
        self.assertEqual(toks, ["[UNK]"])

    def test_empty_string(self):
        ids, mask = _wordpiece.encode("", self.vocab, max_len=8)
        self.assertEqual(ids[:2], [101, 102])
        self.assertEqual(sum(mask), 2)

    def test_truncation_leaves_room_for_cls_sep(self):
        text = "we deploy tuesdays utc utc utc utc utc"
        ids, mask = _wordpiece.encode(text, self.vocab, max_len=6)
        self.assertEqual(ids[0], 101)
        self.assertEqual(ids[-1], 102)
        self.assertEqual(len(ids), 6)

    def test_accents_stripped(self):
        # NFD + Mn-strip: "café" cleans to "cafe" (tokenization via vocab
        # would be UNK here; we assert the cleaning step only).
        self.assertEqual(_wordpiece._clean("café"), "cafe")
        self.assertEqual(_wordpiece._clean("über"), "uber")

    def test_tabs_become_spaces(self):
        toks = _wordpiece.tokenize("embedding\twith", self.vocab)
        self.assertIn("with", toks)
        self.assertIn("em", toks)


if __name__ == "__main__":
    unittest.main()
