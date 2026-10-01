# Sources

Agent A owns `src/sachnoi/extract/`. This is what it reads and what it found.

## Primary source: `vi.wikisource.org`

Every entry in `catalog/books.json` is a Vietnamese edition that already exists
on vi.wikisource.org, so the pipeline fetches and cleans; it never translates.
`TranslateConfig.mode` defaults to `off` and CONTRACT.md forbids machine
translation.

Fetch uses the MediaWiki API through `httpx`, with a descriptive User-Agent
(Wikimedia's UA policy) and exponential backoff on 429/5xx:

| Call | Why |
|---|---|
| `action=query&prop=info&redirects=1` | resolve the page title |
| `action=query&list=search` | fuzzy fallback for a mistyped catalog URL |
| `action=parse&prop=wikitext` | the text to clean |
| `action=query&prop=revisions&rvprop=ids\|timestamp\|user` | provenance: the exact revision that was read, recorded in `book.json` as `source_revision*` |
| `action=query&list=allpages&apprefix=Title/` | subpages, in natural order (`II` before `X`) |
| `action=query&prop=links&plnamespace=0` | an index page's sibling editions |
| `action=parse&prop=text` | the rendered page, for transclusion stubs |

`catalog/books.json` is frozen, so the search fallback exists because hand-written
URLs get mistyped. It only accepts a hit whose title is at least 0.72 similar
to the wanted one, so an unrelated result is a miss, not a wrong book.

## Four ways vi.wikisource stores a work

All four are in the catalog, so all four are handled:

1. **One page with `==` headings.** The easy case. `split_wikitext_sections()`
   cuts at `==` and `===`; `====` and deeper stay inside the chapter.
2. **A root page plus subpages.** `Truyện Kiều (bản Trương Vĩnh Ký 1911)` is a
   649-byte index whose six parts are `/Avant-propos`, `/Tích Túy Kiều`, …
3. **An index page linking to sibling editions.** `Lục Vân Tiên` links to
   `Lục Vân Tiên (bản Nôm 1916)` and `Lục Vân Tiên (bản Quốc ngữ 2082 câu)`.
   Followed only when the root is a stub **and** has no subpages — without that
   guard, case 2 would concatenate two different editions of Kiều.
4. **A transclusion stub.** The subpages of case 2 are 350-byte
   `<pages index="Kim Van Kieu truyen Truong Vinh Ky.pdf" from=207 to=219 />`.
   The wikitext has no text in it at all; the text only exists after template
   expansion, so the rendered HTML is read and walked by the EPUB parser.

### Templates that hold the text

Deleting `{{...}}` as furniture destroys the work on this wiki. Three kinds are
unwrapped instead:

| Template | What it is |
|---|---|
| `{{văn\| … }}` and friends | the chapter's entire prose |
| `{{drop cap\|B}}ắt đầu …` | the first letter of the first word — deleting it eats a character |
| `{{đầu đề\| phần = Chương 4 }}` | the chapter name; emitted as a real `==` heading |
| `{{nowrap\|…}}` and other wrappers | formatting only |

Unwrapping recurses, because the interesting ones nest:
`{{văn|{{drop cap|B}}ắt đầu …}}` is the ordinary shape of a chapter.

### Tables are the poem

The two-column Nôm/Vietnamese *Chinh phụ ngâm* editions put the whole work
inside `{| … |}`. Dropping tables as furniture deleted the book (it produced
1 word). Tables are flattened to one cell per line instead, which satisfies
CONTRACT.md's "no tables" — that rule is about markdown table syntax, not about
discarding a syllable.

## `Lục Vân Tiên` is blocked, and that is correct

The catalog says its root page is 265 bytes with the content in subpages. There
are **no** subpages. Both editions on vi.wikisource are ~1,000-byte stubs that
link to `nomna.org`; stripping the wikitext yields 197 words of editorial notes.

`MIN_TOTAL_WORDS` (250) rejects that, so the book is written as
`status=blocked-no-vi-source` rather than narrated. The floor sits between the
two real cases in the catalog: *Đại Đồng phong cảnh phú* is a genuine 312-word
work and passes, `Lục Vân Tiên` is a 197-word stub and does not.

## Chapter counts that do not match the catalog

Reported, never papered over. `expect_chapters` in the catalog is a hint from a
one-off check, not a contract.

| Book | Catalog expects | Extracted | Why |
|---|---|---|---|
| Truyện Kiều | 92 | 6 | the page is a single 22,780-word `<poem>` with no headings at all |
| Chinh phụ ngâm | 20 | 1 | the poem is one table with no per-đoạn markup |
| Chinh phụ ngâm (Đoàn Thị Điểm) | 20 | 1 | same |
| Truyện Kiều (Trương Vĩnh Ký 1911) | 6 | 75 | the six subpages expand to 71 real headings, then split for length |
| Đại Đồng phong cảnh phú | 1 | 1 | matches |

A chapter longer than `--max-chapter-words` (default 6000) is split
mechanically at paragraph boundaries, and every part is labelled
`<title> (tiếp N)`. The fact is recorded in `manifest.json`. No chapter titles
are invented: a work with no headings gets a work-sized chapter, not a fake
table of contents.

## The other backends

`pdftotext -layout` (poppler) is the primary PDF path, PyMuPDF the fallback, and
the PyMuPDF bookmark outline wins over both when it is present and yields real
prose. `ebooklib` + BeautifulSoup for EPUB, with `h1`–`h6` for structure,
images/tables/footnotes/nav dropped at the DOM level, and `<ruby>`/`<rt>`
unwrapped. `pdf.py` and `txt.py` are the ones that share the wrapped-line
repair, which is where most of the real bugs were.

## Sources that are deliberately not used

`catalog/books.json` → `khong_duoc_lam.giang_keu_trong_drive_google` lists nine
books in the user's Google Drive that are still under copyright. They are not
fetched, not translated, not committed. This pipeline only ever reads
vi.wikisource.org and whatever an operator puts in `books/<slug>/source/`.
