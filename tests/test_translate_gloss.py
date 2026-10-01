"""The 17-rule normaliser, rule by rule, and the number reader.

Every case here was taken from the behaviour of Sano's `normalize.go`, so a
regression in this file means the narration changes.
"""

from __future__ import annotations

import pytest

from sachnoi.translate import gloss
from sachnoi.translate.gloss import (
    SCRIPT_STEPS,
    int_to_viet,
    normalize_script,
    normalize_title,
    roman_to_int,
    split_sentences,
)


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "không"),
        (1, "một"),
        (6, "sáu"),
        (9, "chín"),
        (10, "mười"),
        (11, "mười một"),
        (15, "mười lăm"),
        (20, "hai mươi"),
        (21, "hai mươi mốt"),  # native Vietnamese uses "mốt", not "một"
        (25, "hai mươi lăm"),
        (100, "một trăm"),
        (105, "một trăm lẻ năm"),
        (111, "một trăm mười một"),
        (1000, "một nghìn"),
        (1865, "một nghìn tám trăm sáu mươi lăm"),
        (2024, "hai nghìn không lẻ hai mươi bốn"),
        (1_000_000, "một triệu"),
        (1_234_567, "một triệu hai trăm ba mươi bốn nghìn năm trăm sáu mươi bảy"),
    ],
)
def test_int_to_viet(n: int, expected: str) -> None:
    assert int_to_viet(n) == expected


def test_int_to_viet_handles_negatives_and_zero() -> None:
    assert int_to_viet(-3) == "-ba"
    assert int_to_viet(0) == "không"


@pytest.mark.parametrize(
    ("roman", "value"), [("I", 1), ("IV", 4), ("IX", 9), ("XV", 15), ("XX", 20), ("ABC", 0), ("", 0)]
)
def test_roman_to_int(roman: str, value: int) -> None:
    assert roman_to_int(roman) == value


# --------------------------------------------------------------------------
# Rule 1-3: characters, quotes, page furniture
# --------------------------------------------------------------------------


def test_control_and_invisible_characters_are_removed() -> None:
    assert gloss.strip_control_chars("\ufeff\ufeffDòng có\u200b ký tự\u2060 ẩn\x00") == (
        "Dòng có ký tự ẩn"
    )


def test_tabs_and_newlines_survive() -> None:
    assert gloss.strip_control_chars("a\tb\nc") == "a\tb\nc"


def test_quotes_are_dropped_but_the_words_stay() -> None:
    assert gloss.strip_quotes('Cô giáo dặn “phải chọn” và "làm đúng".') == (
        "Cô giáo dặn phải chọn và làm đúng."
    )


@pytest.mark.parametrize("line", ["12", "- 13 -", "– 14 –", "Trang 15", "Page 16"])
def test_page_number_lines_are_dropped(line: str) -> None:
    out = gloss.drop_page_number_lines(f"Nội dung một.\n{line}\nNội dung hai.")
    assert "Nội dung một." in out and "Nội dung hai." in out
    assert line.strip() not in out


def test_page_number_lookalike_is_kept() -> None:
    # "12 người" is a sentence, not a page number.
    assert "12 người" in gloss.drop_page_number_lines("Có 12 người.")


# --------------------------------------------------------------------------
# Rule 4-5: section numbers and list markers
# --------------------------------------------------------------------------


def test_multi_level_section_number_is_dropped() -> None:
    assert gloss.strip_section_number_lines("1.2.3. Tên mục") == "Tên mục"
    assert gloss.strip_section_number_lines("4.1 Tên mục") == "Tên mục"


def test_single_level_number_is_not_a_section_number() -> None:
    # "3.5 năm kinh nghiệm" and "2026 là năm" are prose, not headings.
    assert gloss.strip_section_number_lines("3.5 năm kinh nghiệm") == "3.5 năm kinh nghiệm"
    assert gloss.strip_section_number_lines("2026 là năm bản lề") == "2026 là năm bản lề"


def test_section_number_can_be_spoken_instead() -> None:
    assert gloss.strip_section_number_lines("1.6. Lập kế hoạch", keep=True) == (
        "một chấm sáu. Lập kế hoạch"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("- Ý thứ nhất", "Ý thứ nhất"),
        ("• Chuẩn bị tài liệu", "Chuẩn bị tài liệu"),
        ("+ Kiểm tra lại", "Kiểm tra lại"),
        ("1. Đọc kỹ đề", "Thứ nhất, Đọc kỹ đề"),
        ("4) Viết bản nháp", "Thứ tư, Viết bản nháp"),
        ("11. Tổng kết", "Thứ mười một, Tổng kết"),
        ("a) Đọc kỹ đề", "Đọc kỹ đề"),
    ],
)
def test_list_markers_become_words(raw: str, expected: str) -> None:
    assert gloss.convert_list_markers(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "Khách quen - khách mới",  # dash with no space is not a bullet
        "-5 độ vào buổi sáng",  # no space after the dash
        "2026. Một năm mới",  # 4 digits is not a list number
    ],
)
def test_list_markers_left_alone(raw: str) -> None:
    assert gloss.convert_list_markers(raw) == raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The number is dropped but the ordinal is not duplicated.
        ("1. Thứ nhất, đọc kỹ đề", "Thứ nhất, đọc kỹ đề"),
        ("1. Đầu tiên là chuẩn bị", "Đầu tiên là chuẩn bị"),
    ],
)
def test_an_existing_ordinal_is_not_doubled(raw: str, expected: str) -> None:
    assert gloss.convert_list_markers(raw) == expected


# --------------------------------------------------------------------------
# Rule 6-7: abbreviations and the pronunciation dictionary
# --------------------------------------------------------------------------


def test_abbreviations_expand() -> None:
    assert gloss.expand_abbreviations("TP.HCM, xem tr. 42") == (
        "Thành phố Hồ Chí Minh, xem trang 42"
    )


def test_abbreviation_never_eats_into_another_word() -> None:
    # Regression guard for the right-boundary rule: a bare "tr" used to turn
    # "triệu" into "trangiệu" and "trắng" into "trangắng".
    assert gloss.expand_abbreviations("300 triệu/năm") == "300 triệu/năm"
    assert gloss.expand_abbreviations("khoảng trắng") == "khoảng trắng"
    # And a left boundary is still required: STP. must not become SThành phố.
    assert gloss.expand_abbreviations("STP.") == "STP."


def test_author_initials_are_not_expanded() -> None:
    # These are novels; a table full of corporate shorthand would corrupt names.
    assert gloss.expand_abbreviations("H. G. Wells") == "H. G. Wells"


def test_pronunciation_dictionary_replaces_whole_tokens() -> None:
    assert gloss.apply_pronunciations("Do KPI cao") == "Do ca pê i cao"
    assert gloss.apply_pronunciations("do kpi cao") == "do kpi cao"  # case-sensitive


def test_all_caps_lines_are_not_spelled_out() -> None:
    # A printed heading should not become a stream of lowercase letters.
    assert gloss.apply_pronunciations("BA CA KHOAN CHI PHI") == "BA CA KHOAN CHI PHI"


def test_glossary_file_is_wellformed() -> None:
    tables = gloss.load_glossary()
    assert tables["abbreviations"] and tables["pronunciations"]
    # Abbreviations must be unambiguous: no key may be a suffix of another after
    # the longest-first ordering is applied.
    for key in tables["abbreviations"]:
        assert key == key.strip()


# --------------------------------------------------------------------------
# Rule 8-9: ampersands and arrows
# --------------------------------------------------------------------------


def test_ampersand_becomes_viet() -> None:
    assert gloss.expand_ampersands("A & B & C") == "A và B và C"
    assert gloss.expand_ampersands("cuối &") == "cuối &"  # needs a letter both sides


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mưa lớn → đường ngập", "Mưa lớn, dẫn tới đường ngập"),
        ("Làm nhanh ⇒ sai sót", "Làm nhanh, dẫn tới sai sót"),
        ("Nhập => xuất", "Nhập, dẫn tới xuất"),
        ("Bước một -> bước hai", "Bước một, dẫn tới bước hai"),
        ("→ Kết quả tốt hơn", "Kết quả tốt hơn"),
        ("Ai làm? → Trưởng nhóm.", "Ai làm? Trưởng nhóm."),
        ("Ngủ muộn, → dẫn đến mệt mỏi", "Ngủ muộn, dẫn đến mệt mỏi"),
        ("Làm kỹ → vì vậy ít sai", "Làm kỹ, vì vậy ít sai"),
        ("Dòng một →\nDòng hai", "Dòng một\nDòng hai"),
    ],
)
def test_arrows(raw: str, expected: str) -> None:
    assert gloss.expand_arrows(raw) == expected


# --------------------------------------------------------------------------
# Rule 9b/10: slashes and ranges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Đọc sách/tạp chí mỗi tối", "Đọc sách, tạp chí mỗi tối"),
        ("Chọn có/không", "Chọn có hoặc không"),
        ("online/offline", "online hoặc offline"),
        ("A/B", "A hoặc B"),
        ("và/hoặc", "và hoặc"),
        ("300 triệu/năm", "300 triệu một năm"),
        ("15k/giờ", "15k một giờ"),
        ("1%/tháng", "1% một tháng"),
        ("5 trang/ngày", "5 trang một ngày"),
        ("60 km/giờ", "60 km một giờ"),
        ("12 người/nhóm", "12 người, nhóm"),
        ("ngày/tháng/năm", "ngày, tháng, năm"),
        ("24/7", "24 7"),
        ("12/09/2026", "12/09/2026"),
        ("1/3", "1/3"),
    ],
)
def test_slash_resolution(raw: str, expected: str) -> None:
    assert gloss.expand_slashes(raw) == expected


def test_urls_are_never_touched() -> None:
    url = "Xem https://sepay.vn/bai/hoc để biết thêm"
    assert gloss.expand_slashes(url) == url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4-6 tháng sau đó", "4 đến 6 tháng sau đó"),
        ("2 - 4 năm.", "2 đến 4 năm."),
        ("20–30 tuổi", "20 đến 30 tuổi"),
        ("5-10%", "5 đến 10%"),
        ("2-3, có khi 4-5.", "2 đến 3, có khi 4 đến 5."),
    ],
)
def test_small_ranges(raw: str, expected: str) -> None:
    assert gloss.expand_small_ranges(raw) == expected


@pytest.mark.parametrize("raw", ["12-09-2026", "090-123-45", "1.2-1.5", "2020-2025"])
def test_ranges_that_are_not_ranges(raw: str) -> None:
    assert gloss.expand_small_ranges(raw) == raw


# --------------------------------------------------------------------------
# Rule 11-15: colons, romans, grades, the letter P
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Nhớ một nguyên tắc: phục vụ tốt hơn mong đợi.", "Nhớ một nguyên tắc. Phục vụ tốt hơn mong đợi."),
        ("Ba việc:lắng nghe, ghi chép", "Ba việc. Lắng nghe, ghi chép"),
        ("Lưu ý : Ghi rõ nguồn", "Lưu ý. Ghi rõ nguồn"),
        ("Ai làm?: trưởng nhóm", "Ai làm? Trưởng nhóm"),
        (": mở đầu", "Mở đầu"),
    ],
)
def test_colon_becomes_a_sentence_break(raw: str, expected: str) -> None:
    assert gloss.expand_colons(raw) == expected


@pytest.mark.parametrize("raw", ["10:30", "Cuộc họp lúc 10:30", "tỉ lệ 3:1", "https://a.vn/b"])
def test_colons_that_are_not_sentence_breaks(raw: str) -> None:
    assert gloss.expand_colons(raw) == raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Chương I: Mở đầu", "Chương một: Mở đầu"),
        ("Phần II nói về", "Phần hai nói về"),
        ("Bài XV tổng kết", "Bài mười lăm tổng kết"),
        ("Chapter IV summary", "Chapter bốn summary"),
    ],
)
def test_roman_chapter_numbers(raw: str, expected: str) -> None:
    assert gloss.expand_heading_romans(raw) == expected


@pytest.mark.parametrize("raw", ["Bài viết này hay", "Phần mềm quản lý", "Mục lục đầu sách"])
def test_roman_words_that_are_not_roman(raw: str) -> None:
    assert gloss.expand_heading_romans(raw) == raw


def test_grade_signs() -> None:
    assert gloss.expand_grade_signs("xếp hạng A-, A hoặc B") == "xếp hạng A trừ, A hoặc B"
    assert gloss.expand_grade_signs("kết thúc ở A+") == "kết thúc ở A cộng"


def test_a_spaced_dash_is_not_a_grade() -> None:
    assert gloss.expand_grade_signs("khách quen - khách mới") == "khách quen - khách mới"


def test_marketing_p_and_lone_p() -> None:
    assert gloss.expand_marketing_p("Mô hình 4P và 7P") == "Mô hình bốn Pê và bảy Pê"
    assert gloss.expand_lone_p("chữ P đầu tiên") == "chữ Pê đầu tiên"
    assert gloss.expand_lone_p("P P") == "Pê Pê"
    # A lower-case p is usually "4 phút", and a digit before P is a code.
    assert gloss.expand_marketing_p("nghỉ 4p") == "nghỉ 4p"
    assert gloss.expand_marketing_p("mã 104P") == "mã 104P"
    assert gloss.expand_lone_p("PR và SEO") == "PR và SEO"


# --------------------------------------------------------------------------
# Rule 16: the number spelling this project adds on top of Sano
# --------------------------------------------------------------------------


def test_standalone_numbers_are_spelled_out() -> None:
    assert gloss.expand_standalone_numbers("Có 3 người đàn ông.") == "Có ba người đàn ông."
    assert gloss.expand_standalone_numbers("3 chuyến tàu, 12 người") == (
        "ba chuyến tàu, mười hai người"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "1.5 kg",  # decimal
        "Cuộc họp lúc 10:30",  # clock time
        "ngày 12/09/2026",  # date
        "tăng 5% rồi",  # percentage
        "giá 2,500 đồng",  # grouped thousands
        "mã 007",  # zero-padded code
        "xem https://a.vn/b?q=12",  # url
    ],
)
def test_numbers_the_tts_reads_better_are_left_alone(raw: str) -> None:
    assert gloss.expand_standalone_numbers(raw) == raw


def test_a_year_is_spelled_out() -> None:
    assert "năm một nghìn tám trăm sáu mươi lăm" in gloss.expand_standalone_numbers(
        "xuất bản năm 1865."
    )


# --------------------------------------------------------------------------
# Rule 17: paragraphs
# --------------------------------------------------------------------------


def test_paragraphs_are_kept_and_squeezed() -> None:
    assert gloss.collapse_spaces_keep_lines(
        "Dòng  một   nhiều   space\n\n\n\nDòng hai\n   \nDòng ba"
    ) == "Dòng một nhiều space\n\nDòng hai\n\nDòng ba"


def test_single_newlines_survive_inside_a_paragraph() -> None:
    # The paragraph contract: \n\n is a pause, \n is a line break.
    assert gloss.collapse_spaces_keep_lines("A.\nB.") == "A.\nB."
    assert gloss.collapse_spaces_keep_lines("\n\nNội dung.\n\n") == "Nội dung."


# --------------------------------------------------------------------------
# The whole pipeline
# --------------------------------------------------------------------------


def test_pipeline_is_idempotent_on_its_own_output() -> None:
    text = "Chương 1\n\nCô bé nghĩ một cuốn sách không có hình vẽ thì vô dụng. 4-6 tháng sau."
    once = normalize_script(text)
    assert normalize_script(once) == once


def test_normalize_script_is_deterministic() -> None:
    text = "TP.HCM & QLĐT → 3 người: 10:30, 12/09, 5%."
    assert normalize_script(text) == normalize_script(text)


def test_script_steps_are_documented_and_ordered() -> None:
    assert len(SCRIPT_STEPS) == 17
    assert SCRIPT_STEPS[0] == "control-chars"
    assert SCRIPT_STEPS[-1] == "numbers"


def test_empty_input() -> None:
    assert normalize_script("") == ""
    assert normalize_title("") == ""


def test_titles_keep_their_digits_and_lose_their_markup() -> None:
    # A chapter track is labelled, not read aloud, so "Chương 3" stays as it is.
    assert normalize_title("## Chương 3") == "Chương 3"
    assert normalize_title("  Tiêu đề có khoảng trắng  ") == "Tiêu đề có khoảng trắng"
    assert normalize_title("1.6. Quản lý thời gian: Lập kế hoạch") == (
        "1.6. Quản lý thời gian: Lập kế hoạch"
    )
    assert normalize_title("Chương I: Mở đầu") == "Chương một: Mở đầu"


# --------------------------------------------------------------------------
# Sentence splitting
# --------------------------------------------------------------------------


def test_sentences_keep_their_punctuation() -> None:
    assert split_sentences("Tôi đến. Bạn đi! Ai đó?") == ["Tôi đến.", "Bạn đi!", "Ai đó?"]


def test_a_closing_quote_may_trail_the_full_stop() -> None:
    assert split_sentences('Bà nói "Đến đi." Rồi bà quay.') == ['Bà nói "Đến đi."', "Rồi bà quay."]


def test_a_short_fragment_is_glued_to_the_previous_sentence() -> None:
    # Otherwise a list number becomes its own "sentence" and its own audio chunk.
    assert split_sentences("Câu một. 2. Câu hai.") == ["Câu một. 2.", "Câu hai."]


def test_ellipsis_ends_a_sentence() -> None:
    assert split_sentences("Rồi sao… Không biết.") == ["Rồi sao…", "Không biết."]


def test_no_sentences_at_all() -> None:
    assert split_sentences("") == []
