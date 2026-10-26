# nv-watch

Daily digest of new NV-centre / diamond quantum-sensing papers. A Claude scheduled task reads it.

`scripts/nv_watch.py` (standard library only) collects candidates from five sources and screens them with a keyword filter:

| source | what it covers | why |
|---|---|---|
| **arXiv RSS** (`rss.arxiv.org/rss/<cat>`) | that day's announcement for the categories in `ARXIV_CATS`, new papers and cross-lists | complete for the current day |
| **arXiv OAI-PMH** (`oaipmh.arxiv.org/oai`, set `physics`) | every physics record created in the look-back window | catches days the job missed |
| **arXiv API** search | full-abstract search, all categories | third net; its search index lags, so on its own it returned only 1–5 hits per 5-day window |
| **Crossref** | journal articles created in the window | |
| **OpenAlex** | one boolean search, filtered by publication date over the last ~35 days | `from_created_date` is a premium filter, and the old 5-query search returned 429 every day |

- Journal items with no abstract (Elsevier withholds them from Crossref) are back-filled from OpenAlex DOI lookups, then from the Semantic Scholar batch API.
- Relevance: `CORE` must match. A bare "nitrogen vacancy" also needs spin or sensing context (`CONTEXT`). Catalysis, battery and nitride papers are dropped (`OFFTOPIC`) unless they mention diamond or NV centres explicitly.
- Items already reported are skipped (`state/seen.json`). Same-day re-runs merge into a single dated digest.
- Output: `digests/latest.{md,json}` and `digests/YYYY-MM-DD.{md,json}`. The header shows ok/FAILED and hit count for each source, the arXiv announcement date covered, how many abstracts were back-filled, and a link to the Actions run.

`.github/workflows/nv-watch.yml` (at the repo root; GitHub ignores workflow files anywhere else) runs at 01:17, 02:47 and 04:23 UTC. GitHub has been starting scheduled jobs 4–6 h late, so with three runs one usually lands before the morning routine. You can also run it by hand from the **Actions** tab ("Run workflow").

Setup: public repo; Settings → Actions → General → Workflow permissions: "Read and write".

Optional repo secrets: `CONTACT_EMAIL` (puts Crossref/OpenAlex requests in their faster "polite" pool), `OPENALEX_API_KEY`.

Tuning: `ARXIV_CATS`, `ARXIV_TERMS`, `CROSSREF_QUERIES`, `OPENALEX_SEARCH`, `CORE`/`CONTEXT`/`OFFTOPIC`, and the ranking weights in `BOOST`.
