"""Text normalisation and tokenisation shared by BM25, product matching and the evaluator.

Chinese has no spaces, and no segmenter (e.g. jieba) is installed, so BM25 indexes CJK character unigrams and bigrams
plus whole alphanumeric model codes (``6204-2rs``, ``m8x20``) and their parts (``6204``, ``2rs``).
"""
from __future__ import annotations

import re
import unicodedata

# A few domain spellings mapped to the catalogue's notation (written before the evaluation run).
_SYNONYMS = [
    (re.compile(r"(\d)\s*平方"), r"\1mm2"),   # 2.5平方 -> 2.5mm2
    (re.compile(r"(\d)\s*极"), r"\1p"),       # 4极 -> 4p
    (re.compile(r"毫米"), "mm"),
]
_CJK = re.compile(r"[一-鿿]+")
_CODE = re.compile(r"[a-z0-9]+(?:[-./][a-z0-9]+)*")


def normalize(text: str) -> str:
    """NFKC, lower case, ``×``/``*`` -> ``x``, collapse whitespace, apply the domain synonyms."""
    text = unicodedata.normalize("NFKC", text).lower().replace("×", "x").replace("*", "x")
    for pattern, repl in _SYNONYMS:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()


def compact(text: str) -> str:
    """Normalised and without spaces: used to compare short answers (``25 x 3 FKM`` == ``25x3fkm``)."""
    return normalize(text).replace(" ", "")


def tokenize(text: str) -> list[str]:
    norm = normalize(text)
    tokens: list[str] = []
    for code in _CODE.findall(norm):
        tokens.append(code)
        parts = [p for p in re.split(r"[-./x]", code) if p]
        if len(parts) > 1:
            tokens.extend(parts)
    for run in _CJK.findall(norm):
        tokens.extend(run)
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def contains_token(haystack: str, token: str) -> bool:
    """``token`` occurs in normalised ``haystack`` and is not glued to other letters/digits at an ASCII edge."""
    start = haystack.find(token)
    while start != -1:
        end = start + len(token)
        before_ok = start == 0 or not (token[0].isascii() and token[0].isalnum()) or not _glue(haystack[start - 1])
        after_ok = end == len(haystack) or not (token[-1].isascii() and token[-1].isalnum()) or not _glue(haystack[end])
        if before_ok and after_ok:
            return True
        start = haystack.find(token, start + 1)
    return False


def _glue(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == ".")
