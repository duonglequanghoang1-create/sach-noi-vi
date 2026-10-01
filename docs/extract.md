# Extract and translate (agent A)

The stage that turns a source into `books/<slug>/text/<slug>.md` and
`books/<slug>/book.json`. See [sources.md](sources.md) for what the Wikisource
backend reads and [gloss.md](gloss.md) for the 17 normalisation rules.

## Run it

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[extract]'

python3 scripts/extract_book.py                    # every book in the catalog
python3 scripts/extract_book.py --slug truyen-kieu
python3 scripts/extract_book.py --probe            # fetch and report, write nothing
python3 scripts/extract_book.py --offline          # rebuild from books/*/source/
python3 -m sachnoi.extract probe                   # same, without the script
python3 scripts/verify_wikisource.py               # live check on two real works
python3 -m pytest tests -q
```

Useful flags: `--translate-mode {off,gloss,command,http}`, `--min-words`,
`--max-chapter-words`, `--no-cache`, `--dry-run`.

## Pipeline

```
catalog/books.json
   │
   ├─ 1. rights gate        check_license() raises RightsError on anything that is
   │                         not public-domain / CC0 / CC-BY-4.0 / CC-BY-SA-4.0,
   │                         BEFORE a single byte is fetched
   │
   ├─ 2. source             books/<slug>/source/ if the operator put a file there,
   │                         else the Wikisource page named by wikisource_url
   │
   ├─ 3. provenance         the author the source's own header names, and a death
   │                         year, are resolved; a mismatch is reported loudly
   │
   ├─ 4. translate          mode `off` by default. No machine translation, ever
   │
   ├─ 5. length             a chapter over --max-chapter-words is split at paragraph
   │                         edges and labelled "(tiếp N)"; recorded in the manifest
   │
   └─ 6. output             text/<slug>.md, text/chNN.md, book.json, manifest.json
```

Exit codes: `0` everything built, `1` a hard failure, `2` at least one book was
blocked and the rest still built.

## The rights gate

`check_license()` is a hard failure, never a warning, and it fires before the
fetch — including before the existence of the file is checked.

`models.py` is frozen by CONTRACT.md and has no `author_dates` field, but
`tools/check_rights.py` needs a death year to check a `public-domain` claim
arithmetically. `save_book()` merges those keys into the JSON after
`Book.save()`. That is safe for agent B: `Book.from_dict()` drops unknown keys,
so `Book.load()` sees exactly the dataclass it expects.

Two things the build refuses to do:

- **write a public-domain claim it cannot check.** No verifiable death year means
  `status=blocked-unverifiable-author`, not a hopeful guess.
- **follow the catalog when it disagrees with the source about the author.**
  *Chinh phụ ngâm* is credited to Nguyễn Trãi in the catalog, but the page's own
  `{{đầu đề}}` says Đặng Trần Côn. `book.json` keeps the catalog's `author` so
  the catalog↔book mapping stays intact, records `source_author`, and takes the
  death year from the person who actually wrote the work.

## `TranslateConfig.mode`

| mode | what it does | when |
|---|---|---|
| `off` (default) | copies Vietnamese through; only invisible characters are removed | every catalog entry |
| `gloss` | the deterministic 17-rule normaliser | a Vietnamese source that is messy |
| `command` | pipes each paragraph-aligned batch through `SACHNOI_MT_COMMAND` on stdin/stdout | operator has accepted the rights question |
| `http` | POSTs `{"text", "target_lang", "model"}` to `SACHNOI_MT_URL` | same |

`command` and `http` both fail loudly — non-zero exit, empty output, non-200,
unparseable body, missing field. A silent pass-through would produce an
untranslated book that claims to be translated, which is the one outcome this
project must not ship. `book.json` records `translate_mode`, and a manifest note
records that machine-translated text is a derivative work whose redistribution
rights are unconfirmed.

CONTRACT.md forbids machine-translating a work to make it look narratable. A
book with no Vietnamese edition becomes `status=blocked-no-vi-source` with
`chapters: []` and a `rights_note` saying so — not a translation.

## The markdown contract

`render_chapter_markdown()` is the only writer, so every backend produces the
same shape:

```markdown
# Chương một

Câu một.
Câu hai.

Câu ba.
```

- exactly one level-1 heading per chapter, no other heading level
- a blank line between every paragraph, never two
- one sentence per line (a câu thơ for verse)
- no HTML, images, links, tables, footnotes, emphasis markers or wiki markup
- every line trimmed

`tests/test_contract.py` asserts all of this against the books this repo actually
ships, so a regression fails there rather than in the middle of a narration.

## What agent B gets

`book.json` with `chapters[]` carrying `index` (1-based), `slug`
(`chuong-01`, stable across reruns), `title`, `source_path` (a per-chapter file
under `text/`) and `word_count`. `audio_path` and `duration_sec` are left `null`
for the TTS stage to fill.

Plus `manifest.json` — a `models.Manifest` with `stage="extract"`, a UTC
timestamp, and notes recording anything that needs explaining: a chapter count
that differs from the catalog, a mechanical length split, an authority mismatch.
