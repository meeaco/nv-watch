#!/usr/bin/env python3
"""NV-diamond literature watch.

Collects new NV-centre / diamond quantum-sensing items from
  * arXiv RSS     - today's announcement (new + cross-lists) for the physics categories below
  * arXiv OAI-PMH - every physics record created in the look-back window (catches missed days)
  * arXiv API     - full-text search (kept as a third net; its index lags, so it is incomplete)
  * Crossref, OpenAlex - journals; missing abstracts are back-filled from OpenAlex / Semantic Scholar
and de-duplicates against previously seen items
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
import urllib.error
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
S2_KEY = os.environ.get("S2_API_KEY", "").strip()              # optional, Semantic Scholar

# ---------------------------------------------------------------- relevance
CORE = re.compile(
    r"nitrogen[\s\-‐–]*vacanc|\bNV[\s\-‐−–]*(?:\^?\{?-\}?|⁻)?[\s\-]*(?:cent(?:er|re)s?|ensembles?|spins?|"
    r"magnetomet|diamonds?|defects?|colou?r|layer|sensor|quantum|ODMR|electron)|"
    r"\bNV\$?\^?\{?[-−]\}?\$?\s|\bNVs?\b[^.]{0,80}\bdiamond|diamond[^.]{0,80}\bNVs?\b|"
    r"optically[\s\-]detected magnetic resonance|\bODMR\b|"
    r"diamond[\s\-](?:magnetomet|quantum sens|sensor|membrane|thermomet)|quantum diamond|nanodiamond(?!-like)",
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


# "nitrogen vacancy" is also a defect in carbon nitride, GaN/AlN, TiN, perovskites ... -> need sensing/spin context
CONTEXT = re.compile(
    r"diamond|алмаз|\bNVs?\b|ODMR|optically[\s\-]detected|colou?r[\s\-]cent|spin[\s\-]defect|qubit|"
    r"magnetomet|quantum[\s\-]sens|relaxometr|spin|hyperfine|photoluminescen|zero[\s\-]phonon",
    re.I,
)
OFFTOPIC = re.compile(
    r"photocatal|electrocatal|catalys|water[\s\-]splitting|hydrogen evolution|C3N4|carbon nitride|"
    r"batter(?:y|ies)|anode|cathode|supercapacit|wurtzite|ferroelectric|\bGaN\b|\bAlN\b|\bTiN\b|perovskite|"
    r"MXene|ammonia synthesis|nitrogen reduction",
    re.I,
)
STRONG = re.compile(r"diamond|\bNV[\s\-]?(?:\$?\^?\{?[-−]\}?\$?)?\s*(?:cent|ensembl|spin|magnetomet)|nanodiamond(?!-like)|ODMR", re.I)


def is_relevant(title: str, abstract: str) -> bool:
    text = f"{title} \n {abstract}"
    if not CORE.search(text):
        return False
    if OFFTOPIC.search(text) and not STRONG.search(text):
        return False
    return bool(CONTEXT.search(text))


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
def _redact(url: str) -> str:
    """Strip credentials from a URL before it is logged or written to the digest."""
    return re.sub(r"(api_key|mailto)=[^&]*", r"\1=***", url)


def get(url: str, retries: int = 4, pause: float = 5.0, accept: str | None = None,
        data: bytes | None = None, max_wait: float = 120.0, extra_headers: dict | None = None) -> bytes:
    """GET (or POST when data is given). Honours Retry-After; error text includes HTTP status and body."""
    last = ""
    hdr = {"User-Agent": UA, **({"Accept": accept} if accept else {})}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    hdr.update(extra_headers or {})
    for i in range(retries):
        wait = pause * (2 ** i)
        try:
            req = urllib.request.Request(url, headers=hdr, data=data)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = clean(e.read().decode("utf-8", "replace"))[:200]
            except Exception:  # noqa: BLE001
                pass
            last = f"HTTP {e.code} {e.reason}" + (f" | {body}" if body else "")
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra and ra.strip().isdigit():
                wait = float(ra)
            if e.code in (400, 401, 403, 404) or wait > max_wait:
                break  # not worth retrying
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        if i < retries - 1:
            time.sleep(wait)
    raise RuntimeError(f"{'POST' if data else 'GET'} failed: {_redact(url)} :: {last}")


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


# ---------------------------------------------------------------- arXiv RSS (today's announcement)
# Categories where NV / diamond sensing papers appear as primary or cross-list.
ARXIV_CATS = ["quant-ph", "cond-mat.mes-hall", "cond-mat.mtrl-sci", "cond-mat.str-el", "cond-mat.supr-con",
              "physics.app-ph", "physics.ins-det", "physics.optics", "physics.bio-ph", "physics.atom-ph",
              "physics.chem-ph", "physics.med-ph", "q-bio.QM"]
RSS_NS = {"arxiv": "http://arxiv.org/schemas/atom", "dc": "http://purl.org/dc/elements/1.1/"}
ANNOUNCED: dict[str, str] = {}   # category -> announcement date seen in its feed (for the digest header)
RSS_SCANNED = {"items": 0, "new_or_cross": 0}


def arxiv_rss() -> list[dict]:
    out: dict[str, dict] = {}
    errors = []
    for cat in ARXIV_CATS:
        try:
            root = ET.fromstring(get(f"https://rss.arxiv.org/rss/{cat}", retries=3))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{cat}: {e}")
            continue
        ch = root.find("channel")
        if ch is None:
            continue
        pub = clean(ch.findtext("pubDate") or ch.findtext("lastBuildDate") or "")
        try:
            ANNOUNCED[cat] = dt.datetime.strptime(pub[:16], "%a, %d %b %Y").date().isoformat()
        except ValueError:
            ANNOUNCED[cat] = pub
        for it in ch.findall("item"):
            RSS_SCANNED["items"] += 1
            atype = clean(it.findtext("arxiv:announce_type", "", RSS_NS))
            if atype.startswith("replace"):
                continue  # replacements of old papers are not news
            RSS_SCANNED["new_or_cross"] += 1
            link = clean(it.findtext("link"))
            m = re.search(r"abs/([^v\s]+)", link)
            if not m:
                continue
            aid = m.group(1)
            desc = it.findtext("description") or ""
            abstract = clean(desc.split("Abstract:", 1)[1] if "Abstract:" in desc else desc)
            cats = [clean(c.text) for c in it.findall("category") if c.text]
            rec = out.setdefault(aid, {
                "source": "arXiv", "id": aid, "url": f"https://arxiv.org/abs/{aid}",
                "title": clean(it.findtext("title")), "authors":
                    [clean(a) for a in re.split(r",\s*|\s+and\s+", it.findtext("dc:creator", "", RSS_NS) or "") if a.strip()],
                "abstract": abstract, "primary_category": "", "categories": cats,
                "published": ANNOUNCED[cat], "updated": "", "doi": "", "journal_ref": "",
            })
            if atype == "new":
                rec["primary_category"] = cat
            rec["categories"] = sorted(set(rec["categories"]) | set(cats))
        time.sleep(1)
    if errors and not out and len(errors) == len(ARXIV_CATS):
        raise RuntimeError("; ".join(errors)[:300])
    return list(out.values())


# ---------------------------------------------------------------- arXiv OAI-PMH (whole window, all physics)
OAI_BASES = ["https://oaipmh.arxiv.org/oai", "https://export.arxiv.org/oai2"]
OAI_NS = {"o": "http://www.openarchives.org/OAI/2.0/", "a": "http://arxiv.org/OAI/arXiv/"}


def arxiv_oai(since: dt.datetime) -> list[dict]:
    """ListRecords from the window start; keep records whose v1 was created in the window."""
    created_min = (since - dt.timedelta(days=1)).date().isoformat()
    last_err = None
    for base in OAI_BASES:
        for oai_set in ("physics", None):
            out: dict[str, dict] = {}
            params = {"verb": "ListRecords", "metadataPrefix": "arXiv", "from": since.date().isoformat()}
            if oai_set:
                params["set"] = oai_set
            url = f"{base}?{urllib.parse.urlencode(params)}"
            try:
                for _page in range(60):
                    root = ET.fromstring(get(url, retries=5, pause=10, max_wait=300))
                    err = root.find("o:error", OAI_NS)
                    if err is not None:
                        if err.get("code") == "noRecordsMatch":
                            return []
                        raise RuntimeError(f"OAI error {err.get('code')}: {clean(err.text)}")
                    for rec in root.iterfind(".//o:record", OAI_NS):
                        md = rec.find("o:metadata/a:arXiv", OAI_NS)
                        if md is None:
                            continue
                        created = md.findtext("a:created", "", OAI_NS)
                        if created < created_min:
                            continue  # metadata update / replacement of an older paper
                        idm = re.match(r"(\d{2})(\d{2})\.\d{4,5}$", md.findtext("a:id", "", OAI_NS) or "")
                        if not idm or f"20{idm.group(1)}-{idm.group(2)}" < created_min[:7]:
                            continue  # <created> can be the latest version's date: v1 is older than the window
                        title = clean(md.findtext("a:title", "", OAI_NS))
                        abstract = clean(md.findtext("a:abstract", "", OAI_NS))
                        if not is_relevant(title, abstract):
                            continue
                        aid = md.findtext("a:id", "", OAI_NS)
                        cats = (md.findtext("a:categories", "", OAI_NS) or "").split()
                        authors = [clean(f"{a.findtext('a:forenames', '', OAI_NS)} {a.findtext('a:keyname', '', OAI_NS)}")
                                   for a in md.findall("a:authors/a:author", OAI_NS)]
                        out[aid] = {
                            "source": "arXiv", "id": aid, "url": f"https://arxiv.org/abs/{aid}", "title": title,
                            "authors": authors, "abstract": abstract,
                            "primary_category": cats[0] if cats else "", "categories": cats,
                            "published": created, "updated": md.findtext("a:updated", "", OAI_NS) or "",
                            "doi": md.findtext("a:doi", "", OAI_NS) or "",
                            "journal_ref": clean(md.findtext("a:journal-ref", "", OAI_NS)),
                        }
                    tok = root.find(".//o:resumptionToken", OAI_NS)
                    if tok is None or not (tok.text or "").strip():
                        return list(out.values())
                    url = f"{base}?{urllib.parse.urlencode({'verb': 'ListRecords', 'resumptionToken': tok.text.strip()})}"
                    time.sleep(5)  # OAI flow control
                raise RuntimeError("OAI: too many pages")
            except Exception as e:  # noqa: BLE001
                last_err = e
                if "badArgument" not in str(e) and "noSetHierarchy" not in str(e):
                    break  # set was fine, endpoint failed -> try next base
    raise RuntimeError(str(last_err))


def merge_arxiv(lists: list[list[dict]], names: list[str]) -> list[dict]:
    """Merge arXiv records from several harvesters; richer fields win, provenance kept in found_by."""
    out: dict[str, dict] = {}
    for recs, name in zip(lists, names):
        for r in recs:
            cur = out.get(r["id"])
            if cur is None:
                out[r["id"]] = {**r, "found_by": [name]}
                continue
            cur["found_by"].append(name)
            for k in ("primary_category", "doi", "journal_ref", "updated"):
                if not cur.get(k) and r.get(k):
                    cur[k] = r[k]
            if len(r.get("authors", [])) > len(cur.get("authors", [])):
                cur["authors"] = r["authors"]
            if len(r.get("abstract", "")) > len(cur.get("abstract", "")):
                cur["abstract"] = r["abstract"]
            if re.match(r"\d{4}-\d{2}-\d{2}", r.get("published", "")) and r["published"] < cur.get("published", "9"):
                cur["published"] = r["published"]
            cur["categories"] = sorted(set(cur.get("categories", [])) | set(r.get("categories", [])))
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
OPENALEX_SEARCH = ('"nitrogen-vacancy" OR "nitrogen vacancy" OR "NV center" OR "NV centers" OR "NV centre" '
                   'OR "NV ensemble" OR nanodiamond OR "diamond magnetometry" OR "diamond quantum" OR ODMR')
OPENALEX_PUB_BACK_DAYS = 30   # publication_date can trail/lead indexing; seen.json removes repeats


def _inv_to_text(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = sorted((p, w) for w, ps in inv.items() for p in ps)
    return " ".join(w for _, w in pos)


def _openalex_params(extra: dict) -> dict:
    p = dict(extra)
    if MAILTO:
        p["mailto"] = MAILTO
    if OPENALEX_KEY:
        p["api_key"] = OPENALEX_KEY
    return p


def openalex_search(since: dt.date) -> list[dict]:
    out: dict[str, dict] = {}
    pub_from = (since - dt.timedelta(days=OPENALEX_PUB_BACK_DAYS)).isoformat()
    params = _openalex_params({
        "search": OPENALEX_SEARCH, "filter": f"from_publication_date:{pub_from},type:article,primary_location.source.type:journal|conference",
        "sort": "publication_date:desc", "per-page": 200,
        "select": "id,doi,title,publication_date,created_date,primary_location,authorships,"
                  "abstract_inverted_index,type"})
    data = json.loads(get("https://api.openalex.org/works?" + urllib.parse.urlencode(params),
                          retries=3, pause=20, max_wait=90))
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
    return list(out.values())


# ---------------------------------------------------------------- abstract back-fill for journal items
def enrich_abstracts(items: list[dict]) -> dict:
    """Fill missing abstracts (Elsevier etc. withhold them from Crossref). Best effort, never fatal."""
    need = {i["doi"]: i for i in items if i.get("doi") and not i.get("abstract")}
    report = {"missing": len(need), "filled": 0, "errors": []}
    if not need:
        return report
    dois = list(need)
    # OpenAlex: DOI lookups in batches of 50 (cheap list calls, no search)
    for k in range(0, len(dois), 50):
        batch = dois[k:k + 50]
        try:
            params = _openalex_params({"filter": "doi:" + "|".join(batch), "per-page": 50,
                                       "select": "doi,abstract_inverted_index"})
            data = json.loads(get("https://api.openalex.org/works?" + urllib.parse.urlencode(params),
                                  retries=2, pause=15, max_wait=60))
            for w in data.get("results", []):
                d = (w.get("doi") or "").replace("https://doi.org/", "").lower()
                txt = clean(_inv_to_text(w.get("abstract_inverted_index")))
                if d in need and txt and not need[d].get("abstract"):
                    need[d]["abstract"], need[d]["abstract_from"] = txt, "OpenAlex"
                    report["filled"] += 1
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"OpenAlex: {str(e)[:120]}")
    # Semantic Scholar batch endpoint for whatever is still missing
    left = [d for d in dois if not need[d].get("abstract")]
    if left:
        try:
            body = json.dumps({"ids": [f"DOI:{d}" for d in left[:400]]}).encode()
            url = "https://api.semanticscholar.org/graph/v1/paper/batch?fields=abstract,externalIds,tldr"
            data = json.loads(get(url, data=body, retries=4, pause=20, max_wait=120, extra_headers=(
                {"x-api-key": S2_KEY} if S2_KEY else None)))
            for d, p in zip(left, data):
                if not p:
                    continue
                txt = clean(p.get("abstract") or "")
                src = "Semantic Scholar"
                if not txt and (p.get("tldr") or {}).get("text"):
                    txt, src = "[TLDR only] " + clean(p["tldr"]["text"]), "Semantic Scholar TLDR"
                if txt:
                    need[d]["abstract"], need[d]["abstract_from"] = txt, src
                    report["filled"] += 1
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"Semantic Scholar: {str(e)[:120]}")
    return report


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

    arx_lists, arx_names = [], []
    for name, fn in [("arXiv-RSS", arxiv_rss),
                     ("arXiv-OAI", lambda: arxiv_oai(since)),
                     ("arXiv-API", lambda: arxiv_search(since, now)),
                     ("Crossref", lambda: crossref_search(since.date())),
                     ("OpenAlex", lambda: openalex_search(since.date()))]:
        t0 = time.time()
        try:
            got = fn()
            if name.startswith("arXiv"):
                got = [g for g in got if is_relevant(g["title"], g["abstract"])]
                arx_lists.append(got)
                arx_names.append(name)
            else:
                items.extend(got)
            status[name] = {"ok": True, "hits": len(got), "secs": round(time.time() - t0)}
            if name == "arXiv-RSS":
                status[name]["scanned"] = RSS_SCANNED["new_or_cross"]
        except Exception as e:  # noqa: BLE001
            status[name] = {"ok": False, "error": str(e)[:300], "secs": round(time.time() - t0)}
        print(f"{name}: {status[name]}", file=sys.stderr)
    # the arXiv block counts as working if any harvester worked
    status["arXiv"] = {"ok": any(status[n]["ok"] for n in ("arXiv-RSS", "arXiv-OAI", "arXiv-API")),
                       "hits": 0}
    if not status["arXiv"]["ok"]:
        status["arXiv"]["error"] = "all arXiv harvesters failed"
    merged = merge_arxiv(arx_lists, arx_names)
    status["arXiv"]["hits"] = len(merged)
    items.extend(merged)

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
    enrich = enrich_abstracts(new_jnl)

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

    run_url = (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/"
               f"{os.environ['GITHUB_RUN_ID']}") if os.environ.get("GITHUB_RUN_ID") else ""
    announced = sorted({v for v in ANNOUNCED.values() if re.match(r"\d{4}-", v)})
    digest = {
        "generated_utc": now.isoformat(timespec="seconds"),
        "date": today,
        "run_url": run_url,
        "arxiv_announcement": announced[-1] if announced else "",
        "abstract_backfill": enrich,
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
          "Sources: " + "; ".join(f"{k}: {'ok, ' + str(v['hits']) + ' hits' + (' of ' + str(v['scanned']) + ' scanned' if 'scanned' in v else '') if v['ok'] else 'FAILED – ' + v['error']}"
                                  for k, v in status.items()), "",
          f"arXiv announcement covered by RSS: {digest['arxiv_announcement'] or 'none (weekend/holiday or RSS failed)'}. "
          f"Journal abstracts back-filled: {enrich['filled']}/{enrich['missing']}"
          + (f" ({'; '.join(enrich['errors'])})" if enrich["errors"] else "") + ".",
          *( [f"Run: {run_url}"] if run_url else [] ), "",
          f"## arXiv ({len(new_arx)} new)", ""]
    for i in new_arx:
        md += [f"### [{i['title']}]({i['url']})",
               f"- {fmt_authors(i['authors'])} · arXiv:{i['id']} · {i['primary_category']} "
               f"(cats: {', '.join(i['categories'])}) · submitted {i['published'][:10]} · relevance score {i['score']}"
               + f" · found by {'+'.join(i.get('found_by', []))}"
               + (f" · journal ref: {i['journal_ref']}" if i.get("journal_ref") else ""),
               f"- Abstract: {i['abstract']}", ""]
    md += [f"## Journals ({len(new_jnl)} new)", ""]
    for i in new_jnl:
        md += [f"### [{i['title']}]({i['url']})",
               f"- {fmt_authors(i['authors'])} · {i.get('venue','')} · created {i.get('created','')[:10]} · "
               f"published {i.get('published','')} · via {i['source']} · relevance score {i['score']}",
               f"- Abstract{' (via ' + i['abstract_from'] + ')' if i.get('abstract_from') else ''}: "
               f"{i.get('abstract') or '(not provided by any index)'}", ""]
    mdt = "\n".join(md)
    (DIGESTS / f"{today}.md").write_text(mdt)
    (DIGESTS / "latest.md").write_text(mdt)
    STATE.write_text(json.dumps(seen, indent=0, sort_keys=True))

    print(mdt[:3000])
    # fail the job only if every source failed, so GitHub emails you
    return 0 if any(v["ok"] for v in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
