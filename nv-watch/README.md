# nv-watch

Daily digest of new NV-centre / diamond quantum-sensing papers. A Claude scheduled task reads it.

- `scripts/nv_watch.py` queries the **arXiv API**, **Crossref** and **OpenAlex**. The arXiv search covers full abstracts across all categories, including cross-lists.
  - The search window is the last 5 days by submission or creation date.
  - Items already reported are skipped, using `state/seen.json`.
  - The output is `digests/latest.md` and `digests/latest.json`, plus a dated copy of each.
- `.github/workflows/nv-watch.yml` runs it every day at 03:40 UTC and commits the result. You can also run it by hand from the **Actions** tab ("Run workflow").

Setup:
1. Create a **public** repo named `nv-watch`.
2. Upload these files, keeping the folders.
3. Go to Settings → Actions → General → Workflow permissions, and choose "Read and write".

Optional repo secrets:
- `CONTACT_EMAIL`: Crossref/OpenAlex give priority access to requests that include a contact email.
- `OPENALEX_API_KEY`

Tuning: the search terms are in `ARXIV_TERMS`, `CROSSREF_QUERIES` and `OPENALEX_QUERIES`. The relevance filter is `CORE`, and the ranking weights for widefield, thermometry, relaxometry and bio work are in `BOOST`.
