# Reading-text normalisation

`src/sachnoi/translate/gloss.py` turns source text into something a Vietnamese
TTS can read aloud without sounding like a document reader. Every rule is a
regex or a lookup table: no model, no network, byte-identical on every run.

It is a re-implementation of the pipeline Sano (`tanviet12/sano-sach-noi`) runs
in `internal/bookmaker/normalize.go`, plus the one rule Sano deliberately
delegates to its TTS and this project must not.

## Order is load-bearing

`normalize_script()` runs these 17 steps in this order. Changing the order
changes the narration.

| # | Step | Example |
|---|---|---|
| 1 | control and invisible characters | `\ufeffDòng\u200bcó` → `Dòngcó` |
| 2 | quote marks | `dặn “phải chọn”` → `dặn phải chọn` |
| 3 | page-number lines | `- 13 -` → dropped |
| 4 | multi-level section numbers | `1.2.3. Tên` → `Tên` |
| 5 | list markers | `4) Viết` → `Thứ tư, Viết` |
| 6 | dotted abbreviations | `TP.HCM` → `Thành phố Hồ Chí Minh` |
| 7 | pronunciation dictionary | `KPI` → `ca pê i` |
| 8 | ampersands | `A&B&C` → `A và B và C` |
| 9 | arrows, slashes, small ranges | `4-6 tháng` → `4 đến 6 tháng` |
| 10 | ranges | (same) |
| 11 | colon → sentence break | `nguyên tắc: phục vụ` → `nguyên tắc. Phục vụ` |
| 12 | roman chapter numbers | `Chương I:` → `Chương một:` |
| 13 | grade signs | `A-` → `A trừ` |
| 14 | marketing models | `4P` → `bốn Pê` |
| 15 | lone capital P | `chữ P` → `chữ Pê` |
| 16 | **standalone digits** | `3 người` → `ba người` |
| 17 | collapse spaces, keep paragraphs | `\n\n\n\n` → `\n\n` |

The order is not arbitrary. Abbreviations run before the dictionary so
`TP.HCM` becomes words rather than a stream of letters; the dictionary runs
before `&` so `R&D` is replaced whole rather than half-expanded; colons run
before romans so `Chương I: Mở đầu` keeps its punctuation.

## Step 16 is the addition

Sano has no general digit-to-word pass — it hands plain digits to the TTS. This
project spells a number out when it stands alone in a telling:

```
Có 3 người đàn ông và 12 người phụ nữ.
→ Có ba người đàn ông và mười hai người phụ nữ.
```

`int_to_viet()` handles the irregularities native Vietnamese uses: `hai mươi
mốt` (21), `hai mươi lăm` (25), `một trăm lẻ năm` (105), `hai nghìn không lẻ
hai mươi bốn` (2024).

Left as digits on purpose, because the TTS reads them correctly and a wrong
guess is worse than the digit:

- decimals `1.5`, clock times `10:30`, dates `12/09/2026`
- percentages `5%`, grouped thousands `2,500`
- zero-padded codes `007`
- anything inside a URL or an email address

## The glossary is deliberately small

`glossary_vi_en.json` has 19 abbreviations and 60 pronunciations. Sano's own
table is full of corporate shorthand (`PGS.`, `ThS.`, `ĐH`, `THPT`) and this
corpus is novels: a table like that corrupts author names. `GS.` and `TS.` are
absent precisely because they would turn *H. G. Wells* into *huyện G. Wells* and
*T. S. Eliot* into *Tiến sĩ S. Eliot*.

Two boundary rules keep the small table safe:

- a left boundary that is not a word character, so `STP.` never becomes
  `SThành phố`;
- for a key with no dot in it, a right boundary that is not a letter, so a bare
  `tr` can never eat the start of `triệu` or `trắng`.

Add entries to the JSON, not to the code.

## `/` means five different things

| Input | Output | Reading |
|---|---|---|
| `Đọc sách/tạp chí` | `Đọc sách, tạp chí` | a list |
| `Chọn có/không` | `Chọn có hoặc không` | a choice |
| `300 triệu/năm` | `300 triệu một năm` | a rate |
| `24/7` | `24 7` | an idiom |
| `12/09/2026` | unchanged | a date |

## `:` becomes a full stop

The TTS pauses ~0.4 s at `.` and ~0.25 s at `:`, so a colon reads as if the
sentence never ended. Promoting it and capitalising the next letter is the single
biggest improvement to how the narration sounds. Times (`10:30`), ratios (`3:1`)
and URLs are excluded, and so is a colon at the start of a line.

## Paragraphs and lines

Step 17 is the contract: `\n\n` is a pause, `\n` is a line break. Runs of spaces
inside a line collapse to one; runs of blank lines collapse to exactly one; the
file never starts or ends with a blank line.

`render_chapter_markdown()` then keeps the source's hard line breaks and splits
each line into sentences. That is what makes verse work: *Truyện Kiều* is one
câu thơ per line with no sentence-final punctuation at all, so collapsing the
lines would produce one 4,000-word run-on paragraph and no TTS could place a
pause in it. Prose is already unwrapped upstream, so keeping its breaks costs
nothing.

## Testing

`tests/test_translate_gloss.py` has one case per rule, taken from Sano's own
behaviour table, plus a check that the pipeline is idempotent on its own output
and deterministic across runs. A regression there means the narration changes.
