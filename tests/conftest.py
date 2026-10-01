"""Shared pytest fixtures. Network-dependent tests opt in with `@needs_network`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

#: A short, genuinely public-domain Vietnamese passage (Ngô Tất Tố, 1894-1945)
#: in the exact wikitext shape vi.wikisource.org uses. Kept short so the repo
#: stays light; the live check over the whole work is `scripts/verify_wikisource.py`.
VI_WIKITEXT_FIXTURE = """{{đầu đề
 | tựa đề     = [[../]]
 | tác giả    = Ngô Tất Tố
 | phần       = I
 | trước      =
 | sau        = [[../II|II]]
}}
{{văn|
{{drop cap|B}}ắt đầu từ gà gáy một tiếng, trâu bò lục tục kéo thợ cày đến đoạn đường phía trong điếm tuần.

Mọi ngày, giờ ấy, những con vật này cũng như những người cổ cày, vai bừa kia, đã lần lượt đi mò ra ruộng làm việc cho chủ.<ref>Số liệu theo sổ địa chính.</ref>

<poem style="margin: 1em 4em; text-align:center;">
Gà gáy giục. Trời sáng mờ mờ.
</poem>

== Ghi chú ==
{{-}}Xem [[Tắt đèn/II|chương sau]].
{{Tham khảo}}
* [[Ngô Tất Tố]]
[[Thể loại:Văn học Việt Nam]]
"""

EN_WIKITEXT_FIXTURE = """'''Alice''' was beginning to get very tired of sitting by her sister on the bank, and of having nothing to do: once or twice she had peeped into the book her sister was reading, but it had no pictures or conversations in it, "and what is the use of a book," thought Alice "without pictures or conversations?"

So she was considering in her own mind (as well as she could, for the hot day made her feel very sleepy and stupid), whether the pleasure of making a daisy-chain would be worth the trouble of getting up and picking the daisies, when suddenly a White Rabbit with pink eyes ran close by her.

There was nothing so very remarkable in that; nor did Alice think it so very much out of the way to hear the Rabbit say to itself, "Oh dear! Oh dear! I shall be late!"

== Notes ==
{{-}}This is the public domain text.
"""


@pytest.fixture
def vi_wikitext() -> str:
    return VI_WIKITEXT_FIXTURE


@pytest.fixture
def en_wikitext() -> str:
    return EN_WIKITEXT_FIXTURE


@pytest.fixture
def catalog_entry() -> dict:
    return {
        "slug": "vi-tat-den",
        "title": "Tắt đèn",
        "author": "Ngô Tất Tố",
        "translator": "",
        "language": "vi",
        "source_language": "vi",
        "narrator": "VN-01",
        "license": "public-domain",
        "license_url": "https://creativecommons.org/publicdomain/mark/1.0/",
        "rights_note": "Ngô Tất Tố (1894-1945). Public domain.",
        "source_url": "https://vi.wikisource.org/wiki/T%E1%BA%AFt_%C4%91%C3%A8n",
    }


def needs_network(func):
    """Mark a test that talks to Wikimedia. Skipped unless SACHNOI_TEST_NET=1."""
    import os

    return pytest.mark.skipif(
        os.environ.get("SACHNOI_TEST_NET") != "1",
        reason="network test; set SACHNOI_TEST_NET=1 to run",
    )(func)
