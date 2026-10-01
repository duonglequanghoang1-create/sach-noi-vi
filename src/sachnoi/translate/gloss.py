"""Reading-text normalisation, Vietnamese style, with pure regex.

This is a faithful re-implementation of the pipeline Sano
(`tanviet12/sano-sach-noi`) runs in `internal/bookmaker/normalize.go`, plus the
one rule Sano deliberately delegates to its TTS and this repo must not:
**digits in narrative prose are spelled out**.

Nothing here calls a model or the network. Every rule is a regex or a table, so
the output is byte-identical across runs -- which is what makes the narration
reproducible.

Pipeline order matters and is documented on :func:`normalize_script`. In short:

    1  control / invisible characters      10  small numeric ranges "2-3"
    2  straight and curly quotes          11  ":" promoted to a sentence break
    3  page-number / running-head lines   12  roman numerals in headings
    4  multi-level section numbers        13  grade signs A- / B+
    5  list markers -> "Thứ nhất, ..."    14  marketing "4P" -> "bốn Pê"
    6  dotted abbreviations               15  lone capital "P" -> "Pê"
    7  acronym pronunciation dictionary   16  standalone digits -> Vietnamese
    8  "&" -> "và"                        17  collapse spaces, keep paragraphs
    9  arrows / "/" / "->"
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Iterable

__all__ = [
    "Gloss",
    "int_to_viet",
    "roman_to_int",
    "normalize_script",
    "normalize_title",
    "split_sentences",
    "load_glossary",
    "SCRIPT_STEPS",
]

DATA_FILE = Path(__file__).with_name("glossary_vi_en.json")

# --------------------------------------------------------------------------
# Small linguistic tables
# --------------------------------------------------------------------------

VIET_UNITS = (
    "không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín",
)

#: Reading for 1..20, used for chapter numbers in titles.
ROMAN_WORDS = (
    "", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín", "mười",
    "mười một", "mười hai", "mười ba", "mười bốn", "mười lăm",
    "mười sáu", "mười bảy", "mười tám", "mười chín", "hai mươi",
)

ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def int_to_viet(n: int) -> str:
    """Read a non-negative integer as Vietnamese words.

    Handles the irregularities native Vietnamese uses: ``hai mươi mốt`` (21),
    ``hai mươi lăm`` (25), ``một trăm lẻ năm`` (105), and the ``linh`` join
    between four-digit groups (``năm 1865`` -> ``một ngàn tám trăm sáu mươi lăm``).
    """
    if n < 0:
        return "-" + int_to_viet(-n)
    if n < 10:
        return VIET_UNITS[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        if tens == 1:
            head = "mười"
        else:
            head = f"{VIET_UNITS[tens]} mươi"
        if ones == 0:
            return head
        if ones == 1 and tens > 1:
            return f"{head} mốt"
        if ones == 5:
            return f"{head} lăm"
        return f"{head} {VIET_UNITS[ones]}"
    if n < 1000:
        hundreds, rem = divmod(n, 100)
        head = f"{VIET_UNITS[hundreds]} trăm"
        if rem == 0:
            return head
        if rem < 10:
            return f"{head} lẻ {VIET_UNITS[rem]}"
        return f"{head} {int_to_viet(rem)}"
    if n < 1_000_000:
        head, rem = divmod(n, 1000)
        if rem == 0:
            return f"{int_to_viet(head)} nghìn"
        if rem < 100:
            # 2024 is read "hai nghìn không lẻ hai mươi tư", not "... hai mươi tư".
            return f"{int_to_viet(head)} nghìn không lẻ {int_to_viet(rem)}"
        return f"{int_to_viet(head)} nghìn {int_to_viet(rem)}"
    if n < 1_000_000_000:
        millions, rem = divmod(n, 1_000_000)
        out = f"{int_to_viet(millions)} triệu"
        return out if rem == 0 else f"{out} {int_to_viet(rem)}"
    # >= a billion: name each group of three digits.
    labels = ["", " nghìn ", " triệu ", " tỷ "]
    groups: list[int] = []
    rest = n
    while rest:
        rest, group = divmod(rest, 1000)
        groups.append(group)
    out = []
    for i in range(len(groups) - 1, -1, -1):
        group = groups[i]
        if group == 0 and not out:
            continue
        out.append("không" if group == 0 else int_to_viet(group))
        if labels[i]:
            out.append(labels[i])
    return "".join(out).strip() or str(n)


def roman_to_int(s: str) -> int:
    """Right-to-left subtractive roman parse. 0 for anything invalid."""
    s = s.upper()
    if not s or any(ch not in ROMAN_VALUES for ch in s):
        return 0
    total = prev = 0
    for ch in reversed(s):
        v = ROMAN_VALUES[ch]
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total


# --------------------------------------------------------------------------
# Compiled rules
# --------------------------------------------------------------------------

# 1. C0 controls except tab/newline, DEL, ZWSP/ZWNJ/ZWJ, word joiner, BOM.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200d\u2060\ufeff]")

# 2. Sano strips quote marks entirely: VieNeu reads them out as "dấu ngoặc kép".
_QUOTES = {ch: "" for ch in ('"', "\u201c", "\u201d", "\u201e", "\u201f")}

# 3. A line that is nothing but a page number / running-head number.
PAGE_NUMBER_LINE_RE = re.compile(r"^[\s]*(trang\s+|page\s+|tr\.?)?[-–—]?\s*\d{1,4}\s*[-–—]?[\s]*$", re.IGNORECASE)

# 4. Multi-level section numbers at the start of a body line: "4.1.2. Tên".
#    Single-level "1. Tên" is deliberately NOT handled here; step 5 owns that,
#    so an ordinary numbered list does not turn into a heading.
_SECTION_NUM_PREFIX_RE = re.compile(r"^[\s]*(\d{1,3}(?:\.\d{1,3})*)(\.[\s]*|[\s]+)([^\W\d_])")
_MULTILEVEL_LINE_RE = re.compile(r"^[\s]*\d{1,3}(?:\.\d{1,3})+(?:\.[\s]*|[\s]+)\D")

# 5. List markers.
_BULLET_LINE_RE = re.compile(r"^[\s]*[-–—•●▪◦○∙·+*]\s+(\S.*)$")
_NUMBERED_LINE_RE = re.compile(r"^[\s]*(\d{1,2})[.)]\s+([^\W\d_].*)$", re.UNICODE)
_LETTERED_LINE_RE = re.compile(r"^[\s]*[a-zA-ZđĐ]\)\s+(\S.*)$")
_ORDINAL_START_RE = re.compile(r"^(thứ\s|đầu tiên|trước hết|cuối cùng|sau cùng)", re.IGNORECASE)

# 8. "&" between two alphanumerics, repeated to fix "A&B&C".
_AMPERSAND_RE = re.compile(r"([^\W_])[ \t]*&[ \t]*([^\W_])", re.UNICODE)

# 9a. Arrows, including the ASCII "->" and "=>".
_ARROW_RE = re.compile(r"[ \t]*(?:→|⇒|⟶|➔|➜|=>|[ \t]->)[ \t]*")
#: Words that make an arrow redundant: "A -> dẫn tới B" would stutter.
_ARROW_CONNECTIVES = (
    "dẫn đến", "dẫn tới", "từ đó", "do đó", "vì vậy", "vì", "mà", "và", "thì", "để",
    "nên", "nơi", "đồng thời", "tức là", "nghĩa là", "chính là", "là", "rồi", "sau đó",
)

# 9b. "/" in its many meanings.
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\S+@\S+\.\S+")
# The left side may be a number or a percentage ("1%/tháng"), so it is not
# restricted to a leading letter the way the right side is.
_SLASH_RE = re.compile(r"([^\W_]+%?)[ \t]*/[ \t]*([^\W_]+)", re.UNICODE)
_MEASURE_SLASH_RE = re.compile(
    r"(\d[\d.,]*%?(?:[ \t]*[-–][ \t]*\d[\d.,]*%?)?[ \t]+\w+)[ \t]*/[ \t]*(\w+)", re.UNICODE
)
_NUMERIC_TOKEN_RE = re.compile(r"^[\d.,]+$")
#: A count with an optional unit: 200, 15k, 7kg, 5%. A leading digit is
#: required, otherwise "ngày/tháng" becomes "ngày một tháng".
_QUANTITY_TOKEN_RE = re.compile(r"^\d[\d.,]*[^\W_]{0,3}%?$", re.UNICODE)
#: Units that make "X/Y" a rate: "3 ngày/tuần" -> "3 ngày một tuần".
_PER_UNITS = frozenset(
    """năm tháng quý tuần ngày giờ phút giây người lần lượt khách đơn ca suất
    buổi kỳ giờ suốt đêm""".split()
)
_AMOUNT_WORDS = frozenset(
    """triệu tỷ tỉ nghìn ngàn trăm đồng đ k usd/vnd đô""".split()
)
#: Paired opposites: "có/không" is a choice, not a division.
_CHOICE_PAIRS = frozenset(
    """có/không đúng/sai nam/nữ online/offline offline/online được/không
    thắng/thua mua/bán lời/lỗ lãi/lỗ tăng/giảm trước/sau trong/ngoài
    yes/no on/off""".split()
)

# 10. Small numeric ranges, with boundaries that exclude dates, phone numbers
#     and decimals.
_SMALL_RANGE_RE = re.compile(
    r"(^|[^\d.,/:-])(\d{1,2})[ \t]*[-–][ \t]*(\d{1,2})($|[^\d/.,:-]|[.,](?:\s|$))"
)

# 11. Colon -> sentence break, so the TTS pauses instead of running on.
_COLON_RE = re.compile(r":[ \t]*")

# 12. Roman numerals after a chapter word. No \b: Python's \b is Unicode-aware
#     but the original Go had to avoid ASCII-only boundaries, and being explicit
#     is clearer. The trailing class requires a non-letter after the numeral so
#     "Bài viết" is not read as "Bài sáuết".
_HEADING_ROMAN_RE = re.compile(
    r"(chương|phần|mục|bài|chapter|phụ lục)\s+([IVXLCDM]+)([\s.,:;)\]!?]|$)", re.IGNORECASE
)

# 13. Grade signs. Only when the letter is glued to the sign, so "khách quen -
#     khách mới" keeps its dash.
_GRADE_MINUS_RE = re.compile(r"\b([A-Z])-([\s,.;:!?)\]→]|$)")
_GRADE_PLUS_RE = re.compile(r"\b([A-Z])\+([\s,.;:!?)\]→]|$)")

# 14/15. The letter P, which VieNeu reads as "phê" (pe) or "phút".
_MARKETING_P_RE = re.compile(r"(^|[^\w\d])(\d{1,2})Ps?([^\w]|$)", re.UNICODE)
_LONE_P_RE = re.compile(r"(^|[^\w\d&])P([^\w\d&]|$)", re.UNICODE)

# 16. Standalone digits to spell out. Paired with `_PROTECTED_NUMBER_RE`, which
#     shields the shapes that must stay numeric, so this only ever sees a digit
#     token that is genuinely standing on its own.
_NUMBER_TOKEN_RE = re.compile(r"(?<![\w])([0-9]{1,9})(?![\w])", re.UNICODE)
#: Shapes we must not touch: decimals, clock times, dates, thousand separators.
_PROTECTED_NUMBER_RE = re.compile(
    r"\d{1,2}:\d{2}"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    r"|\d{1,3}(?:\.\d{3})+(?:,\d+)?"
    r"|\d+[.,]\d+"
    r"|\d+(?:[.,]\d+)?\s*%"
)


# --------------------------------------------------------------------------
# Individual rules
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_glossary() -> tuple[dict[str, str], dict[str, str]]:
    """Load `glossary_vi_en.json` once. Keys are matched literally:
    abbreviations need a non-letter left boundary, pronunciations are whole
    case-sensitive tokens."""
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return dict(data.get("abbreviations", {})), dict(data.get("pronunciations", {}))


def load_glossary() -> dict[str, dict[str, str]]:
    """Public view of the reading tables, for tests and for the docs generator."""
    abbrev, pronun = _load_glossary()
    return {"abbreviations": abbrev, "pronunciations": pronun}


def strip_control_chars(s: str) -> str:
    """Remove control and invisible characters, keeping tab and newline."""
    return CONTROL_CHARS_RE.sub("", s)


def strip_quotes(s: str) -> str:
    """Drop quote marks but keep the quoted words.

    The Vietnamese TTS pronounces a quote character out loud ("dấu ngoặc kép"),
    which is noise in a narration. Losing the marks is the smaller loss.
    """
    for ch, rep in _QUOTES.items():
        s = s.replace(ch, rep)
    return re.sub(r"[ \t]{2,}", " ", s)


def drop_page_number_lines(s: str) -> str:
    """Remove lines that are only a page number: `12`, `- 13 -`, `Trang 14`."""
    return "\n".join(ln for ln in s.split("\n") if not PAGE_NUMBER_LINE_RE.match(ln))


def _split_section_number(line: str) -> str | None:
    """Return the line without its leading `1.2.3` prefix, or None.

    The gate that stops `3.5 năm kinh nghiệm` and `2026 là năm` from being read
    as section numbers: the separator must contain a dot, or the numeral must be
    multi-level *and* followed by an upper-case letter.
    """
    m = _SECTION_NUM_PREFIX_RE.match(line)
    if not m:
        return None
    nums, sep, first = m.group(1), m.group(2), m.group(3)
    if "." not in sep and not ("." in nums and first.isupper()):
        return None
    # Start at group 3: the first letter belongs to the title, not the number.
    return line[m.start(3) :].strip()


def strip_section_number_lines(s: str, *, keep: bool = False) -> str:
    """Remove (or speak) multi-level section numbers like `4.1.2. Tên`."""
    out: list[str] = []
    for line in s.split("\n"):
        if not _MULTILEVEL_LINE_RE.match(line):
            out.append(line)
            continue
        rest = _split_section_number(line)
        if rest is None:
            out.append(line)
        elif keep:
            out.append(_speak_section_number(line))
        else:
            out.append(rest)
    return "\n".join(out)


def _speak_section_number(line: str) -> str:
    """`1.6. Lập kế hoạch` -> `một chấm sáu. Lập kế hoạch`."""
    m = re.match(r"^[\s]*(\d+(?:\.\d+)*)\.?[\s]*", line)
    if not m:
        return line.strip()
    spoken = " chấm ".join(int_to_viet(int(p)) for p in m.group(1).split("."))
    rest = line[m.end() :].strip()
    return f"{spoken}. {rest}" if rest else spoken


def _viet_ordinal(n: int) -> str:
    """1 -> `nhất`, 4 -> `tư`, otherwise the plain reading."""
    return {1: "nhất", 4: "tư"}.get(n, int_to_viet(n))


def convert_list_markers(s: str) -> str:
    """Turn list markers into words the narrator can say.

    `- Ý thứ nhất`      -> `Ý thứ nhất`
    `1. Đọc kỹ đề`     -> `Thứ nhất, Đọc kỹ đề`
    `4) Viết bản nháp` -> `Thứ tư, Viết bản nháp`
    `a) Đọc kỹ đề`     -> `Đọc kỹ đề`

    A line that already starts with an ordinal is left alone, so
    `1. Thứ nhất, đọc kỹ đề` does not become `Thứ nhất, Thứ nhất, ...`.
    """
    out: list[str] = []
    for line in s.split("\n"):
        m = _NUMBERED_LINE_RE.match(line)
        if m:
            if _ORDINAL_START_RE.match(m.group(2)):
                out.append(m.group(2))
            else:
                out.append(f"Thứ {_viet_ordinal(int(m.group(1)))}, {m.group(2)}")
            continue
        m = _BULLET_LINE_RE.match(line)
        if m:
            out.append(m.group(1))
            continue
        m = _LETTERED_LINE_RE.match(line)
        if m:
            out.append(m.group(1))
            continue
        out.append(line)
    return "\n".join(out)


def expand_abbreviations(s: str) -> str:
    """Expand abbreviations from `glossary_vi_en.json`. Longest keys first.

    Two guards, both needed:

    * a left boundary that is *not a word character*, which is what keeps
      ``STP.`` from becoming ``SThành phố``;
    * for a key with no dot in it, a right boundary that is *not a letter*,
      which is what keeps a bare ``tr`` from eating the start of ``triệu`` and
      ``trắng``. A dotted key like ``TP.`` already terminates itself, so it
      only needs the left guard.
    """
    table, _ = _load_glossary()
    for key in sorted(table, key=len, reverse=True):
        tail = "" if "." in key else r"(?![^\W\d_])"
        pattern = re.compile(r"(^|[^\w])" + re.escape(key) + tail, re.UNICODE)
        s = pattern.sub(lambda m, rep=table[key]: m.group(1) + rep, s)
    return s


def _is_shouted(line: str) -> bool:
    """A short all-caps line is a printed heading, not prose to spell out."""
    if len(line.split()) < 3:
        return False
    letters = [c for c in line if c.isalpha()]
    if not letters:
        return False
    return sum(1 for c in letters if c.isupper()) * 5 >= len(letters) * 4


def apply_pronunciations(s: str) -> str:
    """Replace whole tokens found in the pronunciation dictionary.

    Case-sensitive and exact: `PR` and `pr` are different tokens, and a line
    printed in all caps is skipped so a heading does not turn into lowercase
    letters spelled out.
    """
    table, pronun = _load_glossary()
    del table
    if not pronun:
        return s
    token_re = re.compile(r"[^\W_]+(?:&[^\W_]+)*", re.UNICODE)
    out: list[str] = []
    for line in s.split("\n"):
        if _is_shouted(line):
            out.append(line)
            continue
        out.append(token_re.sub(lambda m: pronun.get(m.group(0), m.group(0)), line))
    return "\n".join(out)


def expand_ampersands(s: str, *, passes: int = 3) -> str:
    """`R&D` is already expanded by the dictionary; `A&B&C` -> `A và B và C`."""
    for _ in range(passes):
        new = _AMPERSAND_RE.sub(r"\1 và \2", s)
        if new == s:
            break
        s = new
    return s


def _starts_with_connective(s: str) -> bool:
    low = s.strip().lower()
    return any(low == c or low.startswith(c + " ") or low.startswith(c + ",") for c in _ARROW_CONNECTIVES)


def expand_arrows(s: str) -> str:
    """Turn arrows into a word, or drop them when the sentence already has one.

    `Mưa lớn -> đường ngập`         -> `Mưa lớn, dẫn tới đường ngập`
    `Ngủ muộn -> dẫn đến mệt mỏi`   -> `Ngủ muộn, dẫn đến mệt mỏi`
    `-> Kết quả tốt hơn`            -> `Kết quả tốt hơn`
    """
    out: list[str] = []
    for line in s.split("\n"):
        parts = _ARROW_RE.split(line)
        if len(parts) == 1:
            out.append(line)
            continue
        buf = parts[0]
        for after in parts[1:]:
            tail = buf.rstrip(" \t,")
            last_char = tail[-1] if tail else ""
            if not tail:
                # Arrow at line start: the arrow is punctuation, drop it.
                buf = after
            elif not after.strip():
                # Arrow at line end: drop it, but keep the text before it.
                buf = tail
            elif last_char in ".!?;:":
                buf = tail + " " + after
            elif _starts_with_connective(after):
                buf = tail + ", " + after
            else:
                buf = tail + ", dẫn tới " + after
        out.append(buf.rstrip(" ,"))
    return "\n".join(out)


def _is_single_upper(s: str) -> bool:
    return len(s) == 1 and s.isupper() and s.isalpha()


def _read_slash(m: re.Match[str]) -> str:
    left, right = m.group(1), m.group(2)
    low_l, low_r = left.lower(), right.lower()
    if left == "24" and right == "7":
        return "24 7"
    if _NUMERIC_TOKEN_RE.match(left) and _NUMERIC_TOKEN_RE.match(right):
        return m.group(0)  # a date or a fraction: let the TTS read it
    if low_r in _PER_UNITS and (_QUANTITY_TOKEN_RE.match(left) or low_l in _AMOUNT_WORDS):
        return f"{left} một {right}"
    if low_l == "và" and low_r == "hoặc":
        return f"{left} {low_r}"
    if f"{low_l}/{low_r}" in _CHOICE_PAIRS or (_is_single_upper(left) and _is_single_upper(right)):
        return f"{left} hoặc {right}"
    return f"{left}, {right}"


def _map_outside(pattern: re.Pattern[str], text: str, fn) -> str:
    """Apply `fn` to every span of `text` that `pattern` does not cover."""
    protected = [m.span() for m in pattern.finditer(text)]
    out: list[str] = []
    pos = 0
    for start, end in protected:
        if start > pos:
            out.append(fn(text[pos:start]))
        out.append(text[start:end])
        pos = end
    if pos < len(text):
        out.append(fn(text[pos:]))
    return "".join(out)


def expand_slashes(s: str) -> str:
    """Resolve `/` by context: a rate, a choice, or just a pause.

    `Đọc sách/tạp chí`  -> `Đọc sách, tạp chí`
    `Chọn có/không`      -> `Chọn có hoặc không`
    `300 triệu/năm`      -> `300 triệu một năm`
    `12/09/2026`         -> unchanged (a date)
    URLs are never touched.
    """

    def convert(seg: str) -> str:
        def measure(m: re.Match[str]) -> str:
            if m.group(2).lower() in _PER_UNITS:
                return f"{m.group(1)} một {m.group(2)}"
            return m.group(0)

        seg = _MEASURE_SLASH_RE.sub(measure, seg)
        for _ in range(4):
            new = _SLASH_RE.sub(_read_slash, seg)
            if new == seg:
                break
            seg = new
        return seg

    return _map_outside(_URL_RE, s, convert)


def expand_small_ranges(s: str, *, passes: int = 2) -> str:
    """`4-6 tháng` -> `4 đến 6 tháng`. Dates and phone numbers are excluded."""
    for _ in range(passes):
        new = _SMALL_RANGE_RE.sub(r"\1\2 đến \3\4", s)
        if new == s:
            break
        s = new
    return s


def expand_colons(s: str) -> str:
    """Promote `:` to a full stop so the TTS pauses instead of running on.

    `Nhớ một nguyên tắc: phục vụ tốt hơn.` -> `Nhớ một nguyên tắc. Phục vụ tốt hơn.`
    `10:30` and `https://...` are left alone.
    """
    return _map_outside(_URL_RE, s, _expand_colons_in_segment)


def _expand_colons_in_segment(seg: str) -> str:
    out: list[str] = []
    prev = 0
    for loc in _COLON_RE.finditer(seg):
        start, end = loc.span()
        before = seg[prev:start]
        head = before.rstrip(" \t")
        out.append(head)
        rest = seg[end:]
        first = rest[:1]
        if start == end - 1 and before[-1:].isdigit() and first.isdigit():
            out.append(seg[start:end])  # 10:30, 3:1
        elif not head:
            pass  # colon at line start: just drop it
        elif not rest or rest[0] == "\n":
            if head[-1:] not in ".!?;,":
                out.append(".")
        elif head[-1:] in ".!?;,":
            out.append(" ")
        else:
            out.append(". ")
        if first.islower():
            out.append(first.upper())
            prev = end + 1
        else:
            prev = end
    out.append(seg[prev:])
    return "".join(out)


def expand_heading_romans(s: str) -> str:
    """`Chương I: Mở đầu` -> `Chương một: Mở đầu`. Only 1..20 are spelled out.

    `Bài viết` and `Phần mềm` must not match, which the required delimiter after
    the numeral guarantees.
    """
    def repl(m: re.Match[str]) -> str:
        n = roman_to_int(m.group(2))
        if n <= 0 or n >= len(ROMAN_WORDS):
            return m.group(0)
        return f"{m.group(1)} {ROMAN_WORDS[n]}{m.group(3)}"

    return _HEADING_ROMAN_RE.sub(repl, s)


def expand_grade_signs(s: str) -> str:
    """`A-` -> `A trừ`, `B+` -> `B cộng`. Only when glued to the letter."""
    s = _GRADE_MINUS_RE.sub(r"\1 trừ\2", s)
    s = _GRADE_PLUS_RE.sub(r"\1 cộng\2", s)
    return s


def expand_marketing_p(s: str) -> str:
    """`4P` -> `bốn Pê`. Only an upper-case P: `4p` is usually 4 phút."""

    def repl(m: re.Match[str]) -> str:
        return f"{m.group(1)}{int_to_viet(int(m.group(2)))} Pê{m.group(3)}"

    return _MARKETING_P_RE.sub(repl, s)


def expand_lone_p(s: str, *, passes: int = 2) -> str:
    """A bare capital `P` -> `Pê`. Two passes for `P P` -> `Pê Pê`."""
    for _ in range(passes):
        new = _LONE_P_RE.sub(r"\1Pê\2", s)
        if new == s:
            break
        s = new
    return s


def expand_standalone_numbers(
    s: str,
    *,
    max_digits: int = 6,
) -> str:
    """Spell out digits that stand alone in narrative prose.

    The rule the brief asks for: a number standing on its own in a telling
    should be read as a word, so `Có 3 người đàn ông` becomes
    `Có ba người đàn ông`.

    Left as digits on purpose, because the TTS already reads them correctly and
    a wrong guess is worse than the digit itself:

    * decimals `1.5`, clock times `10:30`, dates `12/09/2026`;
    * percentages `5%` and grouped thousands `1,250`;
    * zero-padded tokens like `007`, which are codes, not counts;
    * anything longer than `max_digits`;
    * anything inside a URL or an email address.
    """
    if not s:
        return s

    def convert_unprotected(seg: str) -> str:
        def repl(m: re.Match[str]) -> str:
            digits = m.group(1)
            if len(digits) > max_digits:
                return m.group(0)
            if digits[0] == "0" and len(digits) > 1:
                return m.group(0)  # a code, not a count
            return int_to_viet(int(digits))

        return _NUMBER_TOKEN_RE.sub(repl, seg)

    def convert(seg: str) -> str:
        # Shield the shapes the TTS handles better than we do, then spell the rest.
        return _map_outside(_PROTECTED_NUMBER_RE, seg, convert_unprotected)

    return _map_outside(_URL_RE, s, convert)


def collapse_spaces_keep_lines(s: str) -> str:
    """Squeeze runs of spaces inside a line, keep exactly one blank between
    paragraphs, and drop leading/trailing blank lines.

    This is the paragraph contract: `\n\n` is a pause, `\n` is a line break.
    """
    out: list[str] = []
    prev_blank = True  # treat the start of the string as a blank line
    for line in s.split("\n"):
        line = " ".join(line.split())
        if not line:
            if prev_blank:
                continue
            out.append("")
            prev_blank = True
            continue
        out.append(line)
        prev_blank = False
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

#: Ordered pipeline, mirroring Sano's `Normalizer.script` with step 16 added.
SCRIPT_STEPS = (
    "control-chars",
    "quotes",
    "page-numbers",
    "section-numbers",
    "list-markers",
    "abbreviations",
    "pronunciations",
    "ampersands",
    "arrows",
    "slashes",
    "ranges",
    "colons",
    "roman-headings",
    "grade-signs",
    "marketing-p",
    "lone-p",
    "numbers",
)


def normalize_script(text: str, *, keep_heading_numbers: bool = False) -> str:
    """Turn raw source text into a Vietnamese reading script.

    Runs the 17 rules in :data:`SCRIPT_STEPS` order and returns the result with
    one blank line between paragraphs. The order is load-bearing: abbreviations
    run before the pronunciation dictionary so `TP.HCM` becomes words rather
    than a stream of letters, the dictionary runs before `&` so `R&D` is
    replaced whole, and colons run before roman headings so `Chương I: Mở đầu`
    keeps its punctuation.
    """
    if not text:
        return ""
    s = strip_control_chars(text)
    s = strip_quotes(s)
    s = drop_page_number_lines(s)
    s = strip_section_number_lines(s, keep=keep_heading_numbers)
    s = convert_list_markers(s)
    s = expand_abbreviations(s)
    s = apply_pronunciations(s)
    s = expand_ampersands(s)
    s = expand_arrows(s)
    s = expand_slashes(s)
    s = expand_small_ranges(s)
    s = expand_colons(s)
    s = expand_heading_romans(s)
    s = expand_grade_signs(s)
    s = expand_marketing_p(s)
    s = expand_lone_p(s)
    s = expand_standalone_numbers(s)
    s = collapse_spaces_keep_lines(s)
    return s.strip()


def normalize_title(title: str) -> str:
    """Normalise a chapter title for display and for the M4B track name.

    Lighter than :func:`normalize_script` on purpose: no dictionary, no
    spoken-symbol rules, no digit spelling. A chapter track is labelled, not
    read aloud as prose, so `Chương 3` should stay `Chương 3`.
    """
    if not title:
        return ""
    s = strip_control_chars(title)
    s = strip_quotes(s)
    s = expand_abbreviations(s)
    s = expand_heading_romans(s)
    s = expand_grade_signs(s)
    s = expand_marketing_p(s)
    s = collapse_spaces_keep_lines(s)
    return re.sub(r"^[\s#=*_]+|[\s#=*_]+$", "", s).strip()


_SENTENCE_RE = re.compile(r"[^.!?…]+(?:[.!?…]+[\"'”’»)\]]*)?|[.!?…]+")
_ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)


def split_sentences(text: str) -> list[str]:
    """Split a paragraph into sentences, keeping the punctuation.

    A fragment with fewer than three letters/digits, or a bare run of
    punctuation, is glued onto the previous sentence so a list number never
    becomes its own unit.
    """
    out: list[str] = []
    for raw in _SENTENCE_RE.findall(text):
        piece = raw.strip()
        if not piece:
            continue
        if out and (len(_ALNUM_RE.findall(piece)) < 3 or piece[0] in ".!?…"):
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return out


class Gloss:
    """Stateful wrapper so a whole document is normalised with one lookup table.

    The rules themselves are pure functions; this class only exists to avoid
    re-reading the glossary JSON per line and to give the pipeline a stable
    object to hand around.
    """

    def __init__(self, *, keep_heading_numbers: bool = False) -> None:
        self.keep_heading_numbers = keep_heading_numbers
        self.abbreviations, self.pronunciations = _load_glossary()

    def script(self, text: str) -> str:
        return normalize_script(text, keep_heading_numbers=self.keep_heading_numbers)

    def title(self, title: str) -> str:
        return normalize_title(title)

    def sentences(self, paragraph: str) -> list[str]:
        return split_sentences(paragraph)

    def __call__(self, text: str) -> str:  # pragma: no cover - convenience
        return self.script(text)
