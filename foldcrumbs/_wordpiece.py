"""WordPiece BERT tokenizer — pure stdlib, zero dependencies.

Replicates the BERT BasicTokenizer (lowercase, NFD + accent stripping,
control-char drop, whitespace normalization, CJK character spacing,
punctuation splitting) plus greedy longest-match-first WordPiece. This is
the piece that lets the optional bundled embedding model run WITHOUT
adding `tokenizers`/HF to the install: vocab.txt is the only artifact and
the code is ours.

Validated ID-for-ID against the official HF tokenizer (tokenizer.json) on
the cases pinned in tests/test_wordpiece.py.
"""
from __future__ import annotations

import unicodedata


def _load_vocab(path: str) -> dict[str, int]:
    vocab = {}
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            vocab[line.rstrip("\n")] = i
    return vocab


def _is_whitespace(ch: str) -> bool:
    # BERT: '\t', '\n', '\r' are control chars but count as whitespace.
    if ch in ("\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) == "Zs"


def _is_control(ch: str) -> bool:
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch).startswith("C")


def _is_punctuation(ch: str) -> bool:
    cp = ord(ch)
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    return unicodedata.category(ch).startswith("P")


def _is_chinese_char(cp: int) -> bool:
    # BERT _is_chinese_char ranges (CJK Unified Ideographs + extensions).
    return (
        (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF)
        or (0x20000 <= cp <= 0x2A6DF) or (0x2A700 <= cp <= 0x2B73F)
        or (0x2B740 <= cp <= 0x2B81F) or (0x2B820 <= cp <= 0x2CEAF)
        or (0xF900 <= cp <= 0xFAFF) or (0x2F800 <= cp <= 0x2FA1F)
    )


def _clean(text: str) -> str:
    """BERT BasicTokenizer clean_text + CJK spacing + whitespace normalize."""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    out = []
    for c in text:
        cp = ord(c)
        if cp == 0 or cp == 0xFFFD or _is_control(c):
            continue
        if _is_whitespace(c):
            out.append(" ")
        else:
            out.append(c)
    text = "".join(out)
    # CJK: put spaces around every ideograph so each becomes its own token
    out = []
    for c in text:
        if _is_chinese_char(ord(c)):
            out.append(" ")
            out.append(c)
            out.append(" ")
        else:
            out.append(c)
    return "".join(out).lower().strip()


def _split_on_punc(word: str) -> list[str]:
    """BERT _run_split_on_punc: punctuation starts a new chunk."""
    if word in ("[UNK]", "[CLS]", "[SEP]", "[PAD]", "[MASK]"):
        return [word]
    chunks: list[list[str]] = [[]]
    for ch in word:
        if _is_punctuation(ch):
            chunks.append([ch])
            chunks.append([])
        else:
            chunks[-1].append(ch)
    return ["".join(c) for c in chunks if c]


def _basic_tokenize(text: str) -> list[str]:
    words = []
    for word in _clean(text).split():
        words.extend(_split_on_punc(word))
    return words


def _wordpiece_tokens(word: str, vocab: dict[str, int], unk: str = "[UNK]",
                      max_len: int = 100) -> list[str]:
    """Greedy longest-match-first WordPiece on one pre-tokenized word."""
    if len(word) > max_len:
        return [unk]
    toks: list[str] = []
    start = 0
    n = len(word)
    while start < n:
        end = n
        matched = None
        while start < end:
            sub = word[start:end]
            if start > 0:
                sub = "##" + sub
            if sub in vocab:
                matched = sub
                break
            end -= 1
        if matched is None:
            return [unk]
        toks.append(matched)
        start = end
    return toks


def tokenize(text: str, vocab: dict[str, int]) -> list[str]:
    pieces: list[str] = []
    for word in _basic_tokenize(text):
        pieces.extend(_wordpiece_tokens(word, vocab))
    return pieces


def encode(text: str, vocab: dict[str, int], max_len: int = 256,
           cls: str = "[CLS]", sep: str = "[SEP]") -> tuple[list[int], list[int]]:
    """Return (input_ids, attention_mask) BERT-style, truncated/padded to max_len."""
    ids_toks = [vocab.get(t, vocab.get("[UNK]", 0)) for t in tokenize(text, vocab)]
    ids_toks = ids_toks[: max_len - 2]
    ids = [vocab[cls]] + ids_toks + [vocab[sep]]
    mask = [1] * len(ids)
    pad = max_len - len(ids)
    ids += [vocab.get("[PAD]", 0)] * pad
    mask += [0] * pad
    return ids, mask
