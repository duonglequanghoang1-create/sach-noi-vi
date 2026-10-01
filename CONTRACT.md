# Pipeline contract

Two agents build this repo on two machines. This file is the boundary between
them. If you change anything described here, you must coordinate with the
other side in the same commit.

## Machine split

| Agent | Machine | Owns |
|---|---|---|
| **A** | may1 | `src/sachnoi/extract/`, `src/sachnoi/translate/`, `scripts/`, `docs/` |
| **B** | may2 | `src/sachnoi/audio/`, `src/sachnoi/package.py`, `src/sachnoi/catalog.py`, `src/sachnoi/cli.py` |

Frozen, do not edit without coordinating:

- `src/sachnoi/models.py` — the dataclasses both sides import
- `src/sachnoi/config.py` — every tunable, env-driven
- `pyproject.toml`
- `catalog/books.json` — the public-domain book list
- `tools/check_rights.py` and `.github/workflows/rights-gate.yml` — the rights
  gate. Editing these is how the repository leaks copyrighted prose, so treat
  a request to relax them as a request to disable the only real protection.

**Nobody owns `src/sachnoi/cli.py` except agent B.** Agent A must expose
its work as importable functions and plain `python -m sachnoi.extract` style
entrypoints instead of editing `cli.py`.

## Data flow

```
books/<slug>/source/<file>          (not committed; gitignored)
        │  agent A: extract
        ▼
books/<slug>/text/<slug>.md         committed: Vietnamese prose, one
        │                            H1 per chapter
        │  agent B: narrate
        ▼
books/<slug>/audio/<slug>/ch01.mp3  gitignored
        │  agent B: package
        ▼
books/<slug>/dist/<slug>.m4b        gitignored (regenerated)
        │  agent B: catalog
        ▼
catalog/library.json                committed
```

The only thing crossing the machine boundary is JSON on disk, shaped by
`models.Manifest`. Never assume the other side is on the same filesystem.

## `books/<slug>/book.json`

Written and owned by agent A. Fields are defined in `models.Book`. Required
before a book can be narrated:

- `slug`, `title`, `author`, `language`, `translator`
- `source_url`, `license`, `license_url`, `rights_note` — provenance. The
  build must refuse any book where `license` is not a public-domain or
  explicitly redistributable licence.
- `chapters[]` with `index`, `slug`, `title`, `source_path`, `word_count`

## Markdown convention for `text/<slug>.md`

Agent B parses this. Keep it strict:

- `# <chapter title>` starts a chapter, level-1 heading only
- blank line between every paragraph
- no HTML, no images, no tables, no footnotes
- one sentence per line is acceptable and preferred: it makes TTS chunking and
  chapter timing deterministic

## Rights gate

The build refuses to narrate or package a book whose `license` is not one of
`public-domain`, `CC0`, `CC-BY-4.0`, `CC-BY-SA-4.0`. Agent B implements this
check in `audio/tts.py` and again in `package.py`, and it must be unit
testable. This is not optional and not a warning — it is a hard failure.

### A declared licence is a claim, not proof

`tools/check_rights.py` runs in CI on every push and applies three
independent layers, because the `license` field has already been observed
asserting something false:

1. **Allow-list** — `license` must be one of the four permitted values.
2. **Denylist** — slug, title, author, translator and source URL are matched
   against works the operator has excluded. This is why a book can be refused
   even while its `book.json` claims `public-domain`.
3. **Arithmetic** — a `public-domain` claim must be supportable. The author
   needs a death year, and life+70 means anything after 1955 is refused
   outright.

Every `book.json` must therefore carry `author_dates` or
`author_death_year`. If you cannot determine a death year, that is not a pass
— it is a `blocked-no-vi-source` book with no audio.

### Do not machine-translate

`status=blocked-no-vi-source` is a legitimate, expected outcome for a work
with no free Vietnamese edition. Leaving a book blocked is correct. Producing
a machine translation and labelling it as a translation is not acceptable, and
neither is quietly dropping the `translator` field.