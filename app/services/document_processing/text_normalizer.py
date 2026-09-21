from __future__ import annotations

import re

_IRREGULAR_CHARS = {
    "\u00A0": " ",
    "\u2002": " ",
    "\u2003": " ",
    "\u2009": " ",
    "\u202F": " ",
    "\u00AD": "",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201A": "'",
    "\u201B": "'",
    "\u201C": '"',
    "\u201D": '"',
    "\u201E": '"',
    "\u201F": '"',
    "\u2032": "'",
    "\u2033": '"',
}


def _replace_irregular_chars(text: str) -> str:
    for char, replacement in _IRREGULAR_CHARS.items():
        text = text.replace(char, replacement)
    return text


def _fix_hyphenation(text: str) -> str:
    lowercase = r"a-z\u03b1-\u03c9"
    return re.sub(rf"(?<=[{lowercase}])-[ \t]*\n[ \t]*(?=[{lowercase}])", "", text)


def normalize_extracted_text(text: str, *, join_line_wrapped: bool = False) -> str:
    text = text.lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x07", "")
    text = _replace_irregular_chars(text)
    text = _fix_hyphenation(text)
    text = re.sub(r"[ \f\v]+", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if join_line_wrapped:
        text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
        text = re.sub(r" {2,}", " ", text)
    return text.strip()
