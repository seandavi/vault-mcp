# DuckDB FTS: capabilities and limits

> **Research note** — resolves [#6](https://github.com/seandavi/vault-mcp/issues/6).
> This is the first file under `docs/research/`; the repo had no research-notes
> convention before this, so this file establishes one: one Markdown file per
> research ticket, every claim cited, empirical claims tagged with the version
> they were verified against.

**Versions pinned for all "verified locally" claims:**

- DuckDB **1.5.5** (Python client, CPython 3.11, macOS arm64, in-memory database)
- `fts` extension version **`6814ec9`** as reported by `duckdb_extensions()`,
  i.e. [duckdb/duckdb-fts @ `6814ec9a7d5f`](https://github.com/duckdb/duckdb-fts/tree/6814ec9a7d5fd63500176507262b0dbf7cea0095)
  (2026-02-16). All `file:line` citations below are into that commit unless
  marked "upstream main".
- Docs page: <https://duckdb.org/docs/stable/core_extensions/full_text_search>
  (fetched 2026-08-07; redirects to `/docs/current/core_extensions/full_text_search.html`).
- "Upstream main" = [duckdb-fts @ `a22606a6`](https://github.com/duckdb/duckdb-fts/tree/a22606a6b6f3f3fb5b7d77cf8585a9e8fb77168e)
  (2026-08-07) — a large rewrite **not shipped** with DuckDB 1.5.5.

**How the "index" actually works** (source): `PRAGMA create_fts_index` is a
pragma that expands into a SQL script (`extension/fts/fts_indexing.cpp:40-245`).
The script creates a schema `fts_<schema>_<table>` containing ordinary tables
(`docs`, `terms`, `dict`, `fields`, `stats`, `stopwords`) built with
`CREATE TABLE ... AS SELECT` from the base table, plus two macros: `tokenize`
and `match_bm25`. There is no index data structure beyond these tables — it is
a static snapshot queried with plain SQL. Verified locally (duckdb 1.5.5):
`information_schema.tables` for the fts schema lists exactly those six tables.

---

## 1. Stemmer and tokenizer options of `PRAGMA create_fts_index`

Named parameters registered: `stemmer`, `stopwords`, `ignore`, `strip_accents`,
`lower`, `overwrite` (`extension/fts/fts_extension.cpp:55-60`). Positional:
`input_table`, `input_id`, then one or more value columns (or `'*'` = all
VARCHAR columns, `fts_indexing.cpp:309-330`).

| Param | Default (source) | Notes |
|---|---|---|
| `stemmer` | `'porter'` (`fts_indexing.cpp:256`) | 27 Snowball stemmers + `'none'`. Verified locally: the error from `stem('x','bogus')` lists `arabic, basque, catalan, danish, dutch, english, finnish, french, german, greek, hindi, hungarian, indonesian, irish, italian, lithuanian, nepali, norwegian, porter, portuguese, romanian, russian, serbian, spanish, swedish, tamil, turkish` — matches the docs list. |
| `stopwords` | `'english'` (`fts_indexing.cpp:262`) | `'english'` = the 571-word SMART list inlined in the script (`fts_indexing.cpp:56-58`); `'none'`; or the qualified name of a table with a single VARCHAR column (`fts_indexing.cpp:61-64`). |
| `ignore` | `[0-9!@#$%^&*()_+={}\[\]:;<>,.?~\\/\|'"`-]+` (`fts_indexing.cpp:272`) | Regex of character runs replaced by a space **before** splitting. Note: **strips all digits**. See discrepancy below. |
| `strip_accents` | `true` (`fts_indexing.cpp:278`) | Applies DuckDB's `strip_accents()` before lowering. |
| `lower` | `true` (`fts_indexing.cpp:284`) | Unicode-aware `lower()` (verified: `Über` → dict term `über` when `strip_accents=0`). |
| `overwrite` | `false` (`fts_indexing.cpp:290`) | See §2. |

**Tokenizer pipeline** (`fts_indexing.cpp:66-78`), applied identically to
documents and to `match_bm25` query strings:

```
strip_accents(s) → lower(s) → regexp_replace(s, ignore, ' ', 'g') → split on \s+
→ stem(token, stemmer) → drop NULL/empty → drop stopwords   [stopword drop: index side only, fts_indexing.cpp:96-104]
```

Verified locally (duckdb 1.5.5) by dumping `fts_main_docs.dict` for a test
corpus: `Café naïve Über résumé 2024 hello-world foo_bar version 1.5.5` indexes
as `cafe, naiv, uber, resum, world, foo, bar, version` — digits and `1.5.5`
vanish entirely, `hello` is eaten by the SMART stopword list, `-` and `_` split
words.

**Docs discrepancy (verified):** the docs page (fetched 2026-08-07) still
documents the default `ignore` as `'(\\.|[^a-z])+'` ("ignoring all escaped and
non-alphabetic lowercase characters"). The shipped source and observed behavior
use the punctuation-and-digit class above. Practical consequences of the real
default: non-ASCII **letters are kept** (only `strip_accents`/`lower` normalize
them), but **digits are always stripped** — with the default `ignore`, queries
for `2024`, version numbers, or dates can never match (verified locally:
`match_bm25(name, '2024')` returns no rows against a document containing
"2024").

**Stopword gotchas for a notes vault:** the SMART list includes every single
ASCII letter and many content-ish words (`hello`, `help`, `value`, `novel`,
`new`, `right`, `welcome`, ...) — `fts_indexing.cpp:56-58`. For vault-mcp,
`stopwords='none'` or a curated list is worth considering.

## 2. Incremental updates: no — snapshot only; `overwrite` = drop + rebuild

**Docs claim:** "The FTS index will not update automatically when the input
table changes. A workaround of this limitation can be recreating the index to
refresh." (docs page, fetched 2026-08-07.)

**Verified locally (duckdb 1.5.5):**

- `INSERT` into the base table → new row is invisible to `match_bm25` and
  absent from `fts_main_<t>.docs`.
- `UPDATE` of an indexed column → stale: the **old** term still matches the
  row, the **new** term does not.
- Re-running `create_fts_index` without `overwrite` →
  `Catalog Error: a FTS index already exists on table 'main.docs'. Supply 'overwrite=1' ...`
  (thrown at `fts_indexing.cpp:296-302`).
- `create_fts_index(..., overwrite=1)` → correct fresh results.

`overwrite` is **not** an update mechanism: the generated script always begins
`DROP SCHEMA IF EXISTS <fts_schema> CASCADE; CREATE SCHEMA ...`
(`fts_indexing.cpp:45-46`); the flag merely bypasses the already-exists check.
Every (re)build is a full rebuild. `drop_fts_index` is literally
`DROP SCHEMA <fts_schema> CASCADE` (`fts_indexing.cpp:37`).

**Upstream main (unreleased in 1.5.5, docs-claim-level only):** duckdb-fts main
(`a22606a6`, README.md) adds `incremental = 1` — trigger-maintained indexes for
`INSERT`/`DELETE`, requiring a `NOT NULL`/`PRIMARY KEY` unique doc-id column.
Not available in the extension DuckDB 1.5.5 autoloads; do not design against it
yet.

**Implication for vault-mcp:** rebuild-with-the-index is the only option and it
is cheap (§6) — this fits the disposable TTL-rebuild design exactly.

## 3. `match_bm25` query capabilities — what exists and what does NOT

Macro signature (created per index, `fts_indexing.cpp:152`):

```sql
match_bm25(docname, query_string, fields := NULL, k := 1.2, b := 0.75, conjunctive := false)
```

It is a **scalar** macro: score for one document, or NULL if no query term
matches. There is no table-valued/top-k search function in this version; the
standard pattern is a subquery computing the score plus
`WHERE score IS NOT NULL ORDER BY score DESC LIMIT n`.

**Exists (verified locally, duckdb 1.5.5):**

- **Multi-term = bag-of-words OR.** Query is tokenized/stemmed with the same
  pipeline; score is the sum of per-term BM25 subscores over matching terms
  (`fts_indexing.cpp:153-208`). `'fox dog'` matched a fox-only doc and a
  dog-only doc.
- **Duplicate query terms are collapsed** — the tokens CTE is `SELECT DISTINCT`
  (`fts_indexing.cpp:154`). Verified: `'fox fox fox'` scores identically to `'fox'`.
- **`conjunctive := true`** requires every distinct post-tokenization query
  term to be present (`HAVING COUNT(DISTINCT termid) = (SELECT COUNT(*) FROM tokens)`,
  `fts_indexing.cpp:186`). Verified positive and negative cases.
- **`fields := 'col1,col2'`** restricts scoring to named indexed columns.
  Gotcha (verified): the list is split on `,` with **no whitespace trim**
  (`fts_indexing.cpp:159`) — `fields := 't1, t2'` silently fails to match
  field `t2`; `'t1,t2'` works.

**Does NOT exist (verified locally, duckdb 1.5.5):**

- **Phrase queries.** No positional data is stored at all — `terms` has only
  `docid, fieldid, termid` (verified via `information_schema.columns`;
  `fts_indexing.cpp:92-135`). `'"brown fox"'` scores identically to
  `'brown fox'` (quotes are stripped by the default `ignore` regex), and word
  order/adjacency is irrelevant.
- **Prefix/wildcard queries.** Term lookup is exact equality on the stemmed
  term (`dict.term = tokens.t`, `fts_indexing.cpp:165`). Verified: `'sear*'`
  matches nothing; `'search*'` matches only because `*` is stripped, leaving
  the ordinary term `search`.
- **Boolean operators.** `AND`/`OR`/`NOT` are just lowercased tokens (and all
  three are SMART stopwords, so they are never in the dict). Verified:
  `'fox AND dog'` returns scores identical to `'fox dog'`.

**Sharp edges around `conjunctive` (verified locally, duckdb 1.5.5):**

1. **Query-side tokens are NOT stopword-filtered** in this version (the tokens
   CTE at `fts_indexing.cpp:153-155` applies tokenize+stem only). Any query
   token that is not in the dict — a stopword, a typo, `AND`, an
   out-of-vocabulary word — makes the required count unsatisfiable, so
   `conjunctive := true` returns **zero rows**. Verified: with a doc
   `'the fox and the dog play'`, conjunctive `'fox dog'` matches, but
   conjunctive `'the fox dog'` and `'fox AND dog'` both return nothing.
   (Upstream main's README explicitly re-defines conjunctive as "all query
   terms **that remain after** tokenization, stopword removal, and stemming" —
   i.e. this is fixed after 1.5.5.)
2. **Filter-placement bug.** Putting the macro directly in the WHERE clause —
   `SELECT name FROM d WHERE fts_main_d.match_bm25(name, 'fox dog', conjunctive := true) IS NOT NULL`
   — returned a document containing only `fox` (wrong), while the same
   predicate applied to a subquery alias
   (`SELECT * FROM (SELECT name, match_bm25(...) AS s FROM d) WHERE s IS NOT NULL`)
   was correct, and the scalar score for the bad row is correctly NULL.
   Reproduced deterministically on duckdb 1.5.5; looks like a decorrelation
   bug interacting with the macro's `HAVING`. **Always compute the score in a
   subquery/CTE and filter on the alias.**

## 4. BM25 tuning parameters

- `k := 1.2` (this is Okapi *k1* — the parameter is literally named `k`; a
  `k1 :=` argument is rejected by the binder, verified locally) and
  `b := 0.75`. Defaults in the macro definition (`fts_indexing.cpp:152`) and
  in the docs table.
- Both are per-query overrides, no rebuild needed. Verified locally: `k := 0.5`
  and `b := 0` change scores as expected.
- Scoring formula (`fts_indexing.cpp:194`):
  `log((N - df + 0.5)/(df + 0.5) + 1) * tf*(k+1)/(tf + k*(1 - b + b*len/avgdl))`
  — the Lucene-style `+1` inside the log keeps IDF non-negative.

## 5. Unicode, diacritics, case

Verified locally (duckdb 1.5.5):

- Defaults (`strip_accents=1, lower=1`): `CAFÉ`, `café`, `cafe` all match a doc
  containing `Café` — accents folded, case folded, applied consistently to
  documents and queries.
- `strip_accents=0`: dict keeps `café`, `über`, `naïv`; query `café` matches,
  query `cafe` does **not** (and vice versa there is no folding either way).
  `lower()` remains Unicode-aware (`Über` → `über`).
- Non-ASCII **letters survive** the default `ignore` regex (contrary to the
  stale docs regex, §1); digits do not.
- CJK caveat (source-level, unverified empirically): splitting is
  whitespace-only after the `ignore` replacement (`fts_indexing.cpp:77`), so
  unsegmented CJK text becomes one giant token per run — effectively
  unsearchable. Upstream main adds an `'opensearch_standard'` tokenizer for
  this (README @ `a22606a6`); not in 1.5.5.

## 6. In-memory behavior, build cost, memory footprint

- Nothing about the extension is persistence-specific: the "index" is ordinary
  tables + macros in a schema of the same database (§ intro), so it works
  unmodified in `:memory:` databases and disappears with the connection.
  All experiments above ran on in-memory DBs.
- Build cost scales ~linearly. Measured locally (duckdb 1.5.5, Apple Silicon,
  in-memory; synthetic notes of 150 words drawn from a 5,000-word vocabulary;
  one run per process):

  | docs | `terms` rows | `create_fts_index` | 3-term top-10 query | DuckDB mem (`duckdb_memory()`) | peak RSS |
  |---:|---:|---:|---:|---:|---:|
  | 1,000 | 150,000 | 0.07 s | 7 ms | 13 MB | 132 MB |
  | 5,000 | 750,000 | 0.30 s | 8 ms | 49 MB | 223 MB |
  | 20,000 | 3,000,000 | 1.17 s | 11 ms | 181 MB | 515 MB |

  The `terms` table is one row per token occurrence (~60 bytes/occurrence
  in-memory at this scale), so footprint is proportional to total corpus tokens.
- Benchmark-harness caveat: the synthetic vocabulary must be alphabetic —
  a first attempt with tokens like `w00042` produced an **empty index**
  (digits stripped by default `ignore`, leaving single letters, which are SMART
  stopwords). A vivid demo of §1's digit gotcha.
- For vault-mcp scale (thousands of notes, hundreds of words each): full FTS
  rebuild is well under a second and a few tens of MB — rebuilding FTS as part
  of every TTL/on-write index rebuild is the right design; incremental
  maintenance is unnecessary even if it existed.

---

## Implications for the vault-mcp search tool surface (feeds #3)

1. **Rebuild, don't maintain.** Build the FTS index inside the same disposable
   rebuild as the rest of the DuckDB cache; use `overwrite=1` defensively.
2. **Promise bag-of-words ranked search only.** No phrase, prefix, wildcard, or
   boolean syntax exists — don't expose or imply operator syntax in the tool
   schema. Quotes/`*`/`AND` in user queries are silently stripped/ignored
   (harmless in default mode).
3. **Be careful with AND semantics.** `conjunctive := true` returns nothing if
   any query token is a stopword or out-of-vocabulary (1.5.5 behavior). Either
   stay disjunctive (default), or pre-filter query tokens against
   `fts_<schema>_<table>.dict` before using conjunctive mode.
4. **Always filter scores via a subquery alias**, never
   `WHERE match_bm25(...) IS NOT NULL` directly (correctness bug with
   `conjunctive`, §3).
5. **Override the default `ignore` regex** if numbers should be searchable
   (dates, years, versions are common in notes): e.g. drop `0-9` from the
   default class. Consider `stopwords='none'` (BM25's IDF already down-weights
   common words) since the SMART list eats words like `help`, `value`, `novel`.
6. **`fields` lists must be comma-separated with no spaces.**
7. `k`/`b` are per-query knobs — safe to expose or tune later without touching
   the index.
8. Score threshold: `match_bm25` returns NULL for non-matches, not 0; ranked
   results need `score IS NOT NULL ... ORDER BY score DESC LIMIT n`.

## Sources

- Docs: <https://duckdb.org/docs/stable/core_extensions/full_text_search>
  (fetched 2026-08-07).
- Source (pinned to the version shipped with DuckDB 1.5.5, extension version
  `6814ec9`):
  [`extension/fts/fts_extension.cpp`](https://github.com/duckdb/duckdb-fts/blob/6814ec9a7d5fd63500176507262b0dbf7cea0095/extension/fts/fts_extension.cpp),
  [`extension/fts/fts_indexing.cpp`](https://github.com/duckdb/duckdb-fts/blob/6814ec9a7d5fd63500176507262b0dbf7cea0095/extension/fts/fts_indexing.cpp).
- Upstream main (future capabilities, not in 1.5.5):
  [README.md @ `a22606a6`](https://github.com/duckdb/duckdb-fts/blob/a22606a6b6f3f3fb5b7d77cf8585a9e8fb77168e/README.md)
  — `incremental` trigger maintenance, `layered_search` (phrase, phrase-prefix,
  autocomplete, wildcard, regex, fuzzy), `opensearch_standard` tokenizer,
  BM25F/`best_fields` field weighting, JSON boolean query trees.
- Empirical verification: DuckDB 1.5.5 (Python), in-memory, macOS arm64,
  2026-08-07. Probe scripts exercised: tokenization dumps, INSERT/UPDATE
  staleness, overwrite/drop, conjunctive/OOV/stopword cases, phrase/prefix/
  boolean negatives, `k`/`b` overrides, accent/case folding both ways, fields
  whitespace, and the 1k/5k/20k scaling table above.
