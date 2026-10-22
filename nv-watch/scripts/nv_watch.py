#!/usr/bin/env python3
"""NV-diamond literature watch.

Queries the arXiv API, Crossref and OpenAlex for new NV-centre / diamond
quantum-sensing items, de-duplicates against previously seen items
(state/seen.json) and writes:

  digests/latest.json, digests/latest.md, digests/YYYY-MM-DD.{json,md}

Standard library only. Designed to run daily in GitHub Actions.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGESTS = ROOT / "digests"
STATE = ROOT / "state" / "seen.json"

LOOKBACK_DAYS = int(os.environ.get("NV_LOOKBACK_DAYS", "5"))   # search window; seen.json prevents repeats
SEEN_KEEP_DAYS = 90
UA = "nv-watch/1.0 (daily literature digest; GitHub Actions)"
MAILTO = os.environ.get("CONTACT_EMAIL", "").strip()          # optional, for Crossref/OpenAlex polite pools
OPENALEX_KEY = os.environ.get("OPENALEX_API_KEY", "").strip()  # optional

# ---------------------------------------------------------------- relevance
CORE = re.compile(
    r"nitrogen[\s\-‐–]*vacanc|\bNV[\s\-‐−–]*(?:\^?\{?-\}?|⁻)?[\s\-]*(?:cent(?:er|re)s?|ensembles?|spins?|"
    r"magnetomet|diamonds?|defects?|colou?r|layer|sensor|quantum|ODMR|electron)|"
    r"\bNV\$?\^?\{?[-−]\}?\$?\s|\bNVs?\b[^.]{0,80}\bdiamond|diamond[^.]{0,80}\bNVs?\b|"
    r"optically[\s\-]detected magnetic resonance|\bODMR\b|"
    r"diamond[\s\-](?:magnetomet|quantum sens|sensor|membrane|thermomet)|quantum diamond|nanodiamond",
    re.I,
)
# terms that move an item up for widefield-ensemble / thermometry / relaxometry / bio work
BOOST = {
    r"wide[\s\-]?field|imaging|camera|microscop": 3,
    r"ensemble": 2,
    r"membrane|thin[\s\-]film|heteroepitax|overgrowth|delta[\s\-]dop": 2,
    r"thermomet|zero[\s\-]field splitting|\bZFS\b|temperature": 3,
    r"relaxometr|\bT_?1\b|\bT_?2\b|spin[\s\-]lattice|decoherence|dephasing|coherence time": 2,
    r"photothermal|nanoparticle|plasmon|heating": 3,
    r"\bcells?\b|biolog|neuron|tissue|in vitro|in vivo|cellular": 3,
    r"magnetomet|magnetic imag": 1,
    r"sensitivity|nT/|pT/|mK/": 1,
}
NOISE_DOI = re.compile(r"/(?:v\d+/)?(?:review|decision|response)\d*$|\.s\d{3}$", re.I)


def is_relevant(title: str, abstract: str) -> bool:
    return bool(CORE.search(f"{title} \n {abstract}"))


def score(title: str, abstract: str) -> int:
    text = f"{title} {abstract}"
    s = 0
    for pat, w in BOOST.items():
        if re.search(pat, text, re.I):
            s += w
    if re.search(r"wide[\s\-]?field|thermomet|relaxometr|photothermal", title, re.I):
        s += 2
    return s


# ---------------------------------------------------------------- http
def get(url: str, retries: int = 4, pause: float = 5.0, accept: str | None = None) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **({"Accept": accept} if accept else {})})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(pause * (i + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} :: {last}")


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ---------------------------------------------------------------- arXiv
ARXIV_TERMS = [
    'abs:"nitrogen-vacancy"', 'ti:"nitrogen-vacancy"', 'abs:"nitrogen vacancy"',
    'abs:"NV center"', 'abs:"NV centers"', 'abs:"NV centre"', 'abs:"NV centres"',
    'abs:"NV ensemble"', 'abs:"NV ensembles"', 'abs:"NV spins"', 'abs:"NV magnetometry"',
    'abs:ODMR', 'abs:"optically detected magnetic resonance"',
    'abs:"diamond magnetometry"', 'abs:"diamond magnetometer"', 'abs:"quantum diamond"',
    'abs:nanodiamond', 'abs:nanodiamonds', 'abs:"diamond quantum sensing"',
    'abs:"diamond membrane"', 'abs:"NV thermometry"', 'abs:"NV relaxometry"',
]
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


def arxiv_search(since: dt.datetime, until: dt.datetime) -> list[dict]:
    rng = f"submittedDate:[{since:%Y%m%d%H%M} TO {until:%Y%m%d%H%M}]"
    out: dict[str, dict] = {}
    # split terms into chunks to keep URLs short and robust
    chunks = [ARXIV_TERMS[i:i + 8] for i in range(0, len(ARXIV_TERMS), 8)]
    for chunk in chunks:
        q = f"({' OR '.join(chunk)}) AND {rng}"
        start = 0
        while True:
            url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
                "search_query": q, "start": start, "max_results": 200,
                "sortBy": "submittedDate", "sortOrder": "descending"})
            root = ET.fromstring(get(url))
            entries = root.findall("a:entry", ATOM)
            for e in entries:
                abs_url = e.findtext("a:id", "", ATOM)
                m = re.search(r"abs/(.+?)(v\d+)?$", abs_url)
                if not m:
                    continue
                aid = m.group(1)
                title = clean(e.findtext("a:title", "", ATOM))
                summary = clean(e.findtext("a:summary", "", ATOM))
                authors = [clean(a.findtext("a:name", "", ATOM)) for a in e.findall("a:author", ATOM)]
                prim = e.find("arxiv:primary_category", ATOM)
                cats = [c.get("term") for c in e.findall("a:category", ATOM)]
                doi = e.findtext("arxiv:doi", "", ATOM)
                jref = e.findtext("arxiv:journal_ref", "", ATOM)
                out[aid] = {
                    "source": "arXiv", "id": aid, "url": f"https://arxiv.org/abs/{aid}",
                    "title": title, "authors": authors, "abstract": summary,
                    "primary_category": prim.get("term") if prim is not None else (cats[0] if cats else ""),
                    "categories": cats, "published": e.findtext("a:published", "", ATOM),
                    "updated": e.findtext("a:updated", "", ATOM), "doi": doi, "journal_ref": jref,
                }
            total = int(root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults") or 0)
            start += 200
            time.sleep(3.5)  # arXiv API etiquette
            if start >= total or not entries:
                break
    return list(out.values())


# ---------------------------------------------------------------- Crossref
CROSSREF_QUERIES = ["nitrogen-vacancy diamond", "NV center diamond sensing", "NV centers magnetometry",
                    "nanodiamond quantum sensing", "diamond quantum sensor ODMR"]


def crossref_search(since: dt.date) -> list[dict]:
    out: dict[str, dict] = {}
    for qtext in CROSSREF_QUERIES:
        params = {"query.bibliographic": qtext, "rows": 100,
                  "filter": f"from-created-date:{since.isoformat()},type:journal-article",
                  "select": "DOI,title,container-title,created,published,author,abstract,URL"}
        if MAILTO:
            params["mailto"] = MAILTO
        data = json.loads(get("https://api.crossref.org/works?" + urllib.parse.urlencode(params)))
        for it in data.get("message", {}).get("items", []):
            doi = (it.get("DOI") or "").lower()
            if not doi or NOISE_DOI.search(doi):
                continue
            title = clean(" ".join(it.get("title") or []))
            abstract = clean(re.sub(r"<[^>]+>", " ", it.get("abstract") or ""))
            if not is_relevant(title, abstract):
                continue
            authors = [clean(f"{a.get('given','')} {a.get('family','')}") for a in it.get("author") or []]
            out[doi] = {
                "source": "Crossref", "id": doi, "url": f"https://doi.org/{doi}", "title": title,
                "authors": authors, "abstract": abstract,
                "venue": clean(" ".join(it.get("container-title") or [])),
                "created": (it.get("created") or {}).get("date-time", ""),
                "published": "-".join(str(x) for x in ((it.get("published") or {}).get("date-parts") or [[""]])[0]),
                "doi": doi,
            }
        time.sleep(2)
    return list(out.values())


# ---------------------------------------------------------------- OpenAlex
OPENALEX_QUERIES = ["nitrogen-vacancy", "NV center diamond", "NV centers diamond", "nanodiamond sensing",
                    "diamond magnetometry"]


def _inv_to_text(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = sorted((p, w) for w, ps in inv.items() for p in ps)
    return " ".join(w for _, w in pos)


def openalex_search(since: dt.date) -> list[dict]:
    out: dict[str, dict] = {}
    for qtext in OPENALEX_QUERIES:
        params = {"search": qtext, "filter": f"from_created_date:{since.isoformat()}", "per-page": 100,
                  "select": "id,doi,title,publication_date,created_date,primary_location,authorships,"
                            "abstract_inverted_index,ids,type"}
        if MAILTO:
            params["mailto"] = MAILTO
        if OPENALEX_KEY:
            params["api_key"] = OPENALEX_KEY
        data = json.loads(get("https://api.openalex.org/works?" + urllib.parse.urlencode(params)))
        for w in data.get("results", []):
            title = clean(w.get("title"))
            abstract = clean(_inv_to_text(w.get("abstract_inverted_index")))
            if not is_relevant(title, abstract):
                continue
            doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
            if doi and NOISE_DOI.search(doi):
                continue
            src = ((w.get("primary_location") or {}).get("source") or {})
            key = doi or w.get("id")
            out[key] = {
                "source": "OpenAlex", "id": key, "url": f"https://doi.org/{doi}" if doi else w.get("id"),
                "title": title,
                "authors": [clean((a.get("author") or {}).get("display_name")) for a in w.get("authorships") or []],
                "abstract": abstract, "venue": clean(src.get("display_name")),
                "created": w.get("created_date", ""), "published": w.get("publication_date", ""), "doi": doi,
            }
        time.sleep(1.5)
    return list(out.values())


# ---------------------------------------------------------------- state / output
def load_seen() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def arxiv_doi(aid: str) -> str:
    return f"10.48550/arxiv.{aid}".lower()


def fmt_authors(a: list[str]) -> str:
    a = [x for x in a if x]
    return ", ".join(a) if len(a) <= 4 else f"{a[0]} et al. ({len(a)} authors)"


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=LOOKBACK_DAYS)
    seen = load_seen()
    status: dict[str, dict] = {}
    items: list[dict] = []

    for name, fn in [("arXiv", lambda: arxiv_search(since, now)),
                     ("Crossref", lambda: crossref_search(since.date())),
                     ("OpenAlex", lambda: openalex_search(since.date()))]:
        try:
            got = fn()
            status[name] = {"ok": True, "hits": len(got)}
            items.extend(got)
        except Exception as e:  # noqa: BLE001
            status[name] = {"ok": False, "error": str(e)[:300]}

    # de-duplicate: arXiv first; journal items that match an arXiv DOI / title are attached, not repeated
    def norm_title(t: str) -> str:
        return re.sub(r"[^a-z0-9]", "", t.lower())[:120]

    arx = {i["id"]: i for i in items if i["source"] == "arXiv" and is_relevant(i["title"], i["abstract"])}
    titles = {norm_title(i["title"]): i for i in arx.values()}
    journals: dict[str, dict] = {}
    for i in items:
        if i["source"] == "arXiv":
            continue
        doi = i.get("doi", "")
        if doi.startswith("10.48550/arxiv."):
            continue  # arXiv DOI mirrored in Crossref/OpenAlex; covered by arXiv API
        nt = norm_title(i["title"])
        if nt in titles:
            titles[nt].setdefault("journal_versions", []).append({"venue": i.get("venue"), "doi": doi})
            continue
        k = doi or nt
        if k in journals:
            if not journals[k].get("abstract") and i.get("abstract"):
                journals[k]["abstract"] = i["abstract"]
            continue
        journals[k] = i

    new_arx = [i for i in arx.values() if i["id"] not in seen]
    for k, i in journals.items():
        i["_key"] = k
    new_jnl = [i for k, i in journals.items() if k not in seen]
    for i in new_arx + new_jnl:
        i["score"] = score(i["title"], i.get("abstract", ""))
    new_arx.sort(key=lambda i: (-i["score"], i["published"]))
    new_jnl.sort(key=lambda i: (-i["score"], i.get("created", "")))

    today = now.date().isoformat()
    for i in new_arx:
        seen[i["id"]] = today
    for i in new_jnl:
        seen[i["_key"]] = today
    cutoff = (now.date() - dt.timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}

    # same-day re-runs: keep what an earlier run today already reported
    prev_path = DIGESTS / f"{today}.json"
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text())
            have = {i["id"] for i in new_arx}
            new_arx = [i for i in prev.get("arxiv_new", []) if i["id"] not in have] + new_arx
            havej = {i["id"] for i in new_jnl}
            new_jnl = [i for i in prev.get("journal_new", []) if i["id"] not in havej] + new_jnl
            new_arx.sort(key=lambda i: (-i["score"], i["published"]))
            new_jnl.sort(key=lambda i: (-i["score"], i.get("created", "")))
        except Exception:  # noqa: BLE001
            pass

    digest = {
        "generated_utc": now.isoformat(timespec="seconds"),
        "date": today,
        "search_window_utc": [since.isoformat(timespec="minutes"), now.isoformat(timespec="minutes")],
        "sources": status,
        "arxiv_new": new_arx,
        "journal_new": new_jnl,
    }

    DIGESTS.mkdir(exist_ok=True)
    STATE.parent.mkdir(exist_ok=True)
    js = json.dumps(digest, ensure_ascii=False, indent=1)
    (DIGESTS / f"{today}.json").write_text(js)
    (DIGESTS / "latest.json").write_text(js)

    md = [f"# NV watch digest {today}", "",
          f"Generated {digest['generated_utc']} UTC. Search window {digest['search_window_utc'][0]} → "
          f"{digest['search_window_utc'][1]} (items already reported in earlier digests are excluded).", "",
          "Sources: " + "; ".join(f"{k}: {'ok, ' + str(v['hits']) + ' hits' if v['ok'] else 'FAILED – ' + v['error']}"
                                  for k, v in status.items()), "",
          f"## arXiv ({len(new_arx)} new)", ""]
    for i in new_arx:
        md += [f"### [{i['title']}]({i['url']})",
               f"- {fmt_authors(i['authors'])} · arXiv:{i['id']} · {i['primary_category']} "
               f"(cats: {', '.join(i['categories'])}) · submitted {i['published'][:10]} · relevance score {i['score']}"
               + (f" · journal ref: {i['journal_ref']}" if i.get("journal_ref") else ""),
               f"- Abstract: {i['abstract']}", ""]
    md += [f"## Journals ({len(new_jnl)} new)", ""]
    for i in new_jnl:
        md += [f"### [{i['title']}]({i['url']})",
               f"- {fmt_authors(i['authors'])} · {i.get('venue','')} · created {i.get('created','')[:10]} · "
               f"published {i.get('published','')} · via {i['source']} · relevance score {i['score']}",
               f"- Abstract: {i.get('abstract') or '(not provided by index)'}", ""]
    mdt = "\n".join(md)
    (DIGESTS / f"{today}.md").write_text(mdt)
    (DIGESTS / "latest.md").write_text(mdt)
    STATE.write_text(json.dumps(seen, indent=0, sort_keys=True))

    print(mdt[:3000])
    # fail the job only if every source failed, so GitHub emails you
    return 0 if any(v["ok"] for v in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
