# daily_brief.py  — clean, spaces-only version
import os, json, time, csv
from datetime import date, datetime, timezone
from typing import List, Dict, Any
import requests
from openai import OpenAI
import re
from typing import List, Dict, Any, Tuple

# ---------- CONFIG ----------
TARGET_ARTICLES = 6
PER_CALL_LIMIT = 3
REQUEST_DELAY_S = 0.6
MODEL_FOR_SUMMARY = "gpt-4o-mini"
SITE_BASE_URL = "https://sanky0-0.github.io/financial-news-brief"  # change this
TRANSLATE_TO_EN = True   # set False to disable later

MARKETAUX_PARAMS = {
    "filter_entities": "true",
    # "language": "en",        # optional
    # "countries": "us,gb,eu", # optional
}

# ---------- KEYS ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
MARKETAUX_API_KEY = os.environ.get("MARKETAUX_API_KEY")
missing = []
if not OPENAI_API_KEY:
    missing.append("OPENAI_API_KEY")
if not MARKETAUX_API_KEY:
    missing.append("MARKETAUX_API_KEY")
if missing:
    raise SystemExit(f"Missing env var(s): {', '.join(missing)}")

client = OpenAI(api_key=OPENAI_API_KEY)


#--------GDELT--------
ENABLE_GDELT   = os.getenv("ENABLE_GDELT", "1")  # "1" to enable, "0" to disable
GDELT_QUERY    = os.getenv("GDELT_QUERY", "finance OR market OR stocks OR earnings OR inflation OR central bank")
GDELT_MAXREC   = int(os.getenv("GDELT_MAXREC", "40"))
GDELT_TIMESPAN = os.getenv("GDELT_TIMESPAN", "24h")


#---------llm_chat def---------
def call_llm(messages, max_tokens=200, temperature=0.3, model="gpt-4o-mini"):
    """
    Simple wrapper around OpenAI Chat Completions.
    Returns the assistant's message content or '' on failure.
    """
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print("LLM error:", e)
        return ""


# ---------- FETCH ----------
def fetch_marketaux_articles(
    api_key: str, per_call_limit: int, target: int, extra_params: Dict[str, str]
) -> List[Dict[str, Any]]:
    base = "https://api.marketaux.com/v1/news/all"
    page = 1
    results: List[Dict[str, Any]] = []
    seen = set()

    while len(results) < target:
        params = {"api_token": api_key, "limit": str(per_call_limit), "page": str(page)}
        params.update(extra_params)

        r = requests.get(base, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()

        warnings = payload.get("warnings") or []
        if warnings:
            print("⚠️ Marketaux:", "; ".join(warnings))

        items = payload.get("data") or []
        if not items:
            break

        added = 0
        for it in items:
            uid = it.get("uuid") or it.get("url") or it.get("title", "")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            results.append(it)
            added += 1
            if len(results) >= target:
                break

        if added == 0:
            break

        page += 1
        time.sleep(REQUEST_DELAY_S)

    return results[:target]

#-------MarketAux wrapper------
ENABLE_MARKETAUX = os.getenv("ENABLE_MARKETAUX", "0")  # "1"=use, "0"=skip

def safe_fetch_marketaux_articles(api_key, per_call_limit, target_articles, params):
    """Call Marketaux, but never crash the run. Returns [] on any error."""
    if ENABLE_MARKETAUX != "1":
        print("[Marketaux] skipped by config")
        return []
    try:
        return fetch_marketaux_articles(api_key, per_call_limit, target_articles, params)
    except Exception as e:
        print(f"[Marketaux] fetch error: {e}")
        return []


# ---- GDELT (free) fetcher — robust + auto-fix query ----
import os
import requests
from datetime import datetime, timedelta, timezone

def _gdelt_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")

def _normalize_gdelt_query(q: str) -> str:
    """
    GDELT requires: any OR'ed terms must be inside parentheses.
    Also quote multi-word terms so the OR applies to the phrase.
    e.g., finance OR market OR central bank  ->  (finance OR market OR "central bank")
    """
    q = (q or "").strip()
    if not q:
        return "(finance OR market)"
    # quote a few common multi-word phrases if present
    q = q.replace("central bank", '"central bank"').replace("interest rate", '"interest rate"')
    # if it contains OR but no parentheses, wrap the whole thing
    if " OR " in q and "(" not in q and ")" not in q:
        q = f"({q})"
    return q

def fetch_gdelt(query: str = "finance OR market OR stocks OR earnings OR inflation OR central bank",
                max_records: int = 40,
                hours_back: int = 24) -> list:
    """
    Uses explicit UTC start/end window (more reliable than timespan).
    Handles non-JSON responses and prints minimal debug when GDELT_DEBUG=1.
    """
    base = "https://api.gdeltproject.org/api/v2/doc/doc"
    headers = {"User-Agent": "financial-news-ai/1.0"}
    debug = os.getenv("GDELT_DEBUG", "0") == "1"

    # Normalize the query for GDELT's boolean rules
    q = _normalize_gdelt_query(query)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=hours_back)

    params = {
        "query": q,
        "mode": "ArtList",
        "maxrecords": str(max_records),
        "sort": "DateDesc",
        "format": "json",
        "startdatetime": _gdelt_stamp(start_dt),
        "enddatetime": _gdelt_stamp(end_dt),
    }

    try:
        r = requests.get(base, params=params, headers=headers, timeout=30)
        if debug:
            print("[GDELT] GET", r.url)
        r.raise_for_status()
        # Guard: GDELT returns text/html on errors. Don't .json() that.
        ctype = r.headers.get("content-type", "").lower()
        if "application/json" not in ctype:
            if debug:
                print("[GDELT] Non-JSON response (first 300 chars):", r.text[:300])
            return []
        data = r.json()
        arts = data.get("articles", []) or []
    except Exception as e:
        print("[GDELT] fetch error:", e)
        return []

    # Normalize to your item schema
    out = []
    for a in arts:
        title = (a.get("title") or "").strip()
        url_ = a.get("url") or ""
        domain = (a.get("domain") or "").strip()
        lang = (a.get("language") or "").strip()
        seen = (a.get("seendate") or "").strip()

        published_iso = ""
        if len(seen) == 14 and seen.isdigit():
            try:
                dt = datetime.strptime(seen, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                published_iso = dt.isoformat()
            except Exception:
                pass

        out.append({
            "title": title,
            "title_en": None,
            "source": domain or "GDELT",
            "url": url_,
            "published_at": published_iso,
            "language": lang or None,
            "source_lang": lang or None,
            "provider": "gdelt",
        })

    if debug:
        print(f"[GDELT] returning {len(out)} articles")
    return out


# ---- TAGGING HELPERS ----
SECTION_ORDER = [
    "Macro policy", "Earnings", "Tech/AI", "Energy", "Rates", "Geopolitics", "Other"
]

# Keyword rules for tagging headlines
TAG_KEYWORDS = {
    "Macro policy": [
        r"\b(inflation|cpi|ppi|gdp|employment|jobs report|tariff|sanction|fiscal|budget|stimulus|subsidy|regulation|policy|central bank|ecb|boj|pboC|imf|world bank)\b",
        r"\b(china|us|europe|eu|japan|india|emerging markets)\b.*\b(policy|ban|restriction|export control|trade)\b",
    ],
    "Earnings": [
        r"\b(earnings|revenue|guidance|profit|loss|quarter|q[1-4]\b|beat|miss|outlook)\b"
    ],
    "Tech/AI": [
        r"\b(ai|artificial intelligence|chip|semiconductor|gpu|cloud|data center|software|saas|foundry|hbm)\b"
    ],
    "Energy": [
        r"\b(oil|gas|lng|coal|uranium|nuclear|solar|wind|renewable|opec|brent|wti)\b"
    ],
    "Rates": [
        r"\b(rate hike|rate cut|interest rate|yield|treasury|bond|spread|dovish|hawkish|dot plot)\b"
    ],
    "Geopolitics": [
        r"\b(conflict|war|ceasefire|election|coup|sanction|diplomatic|border|strait|taiwan|ukraine|middle east|red sea)\b"
    ],
}

def make_anchor_id(text: str) -> str:
    """Convert section title into a safe HTML anchor ID."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return slug.strip("-") or "section"

def choose_section_for_headline(title: str, src: str = "", lang: str = "en") -> str:
    """Pick the most likely section tag for a given headline."""
    t = (title or "").lower()
    for section, patterns in TAG_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, t):
                return section
    return "Other"

def tag_headlines(items: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], str]]:
    """Return [(headline_dict, section_tag), ...]."""
    tagged = []
    for it in items:
        title = (it.get("title_en") or it.get("title") or "").strip()
        tag = choose_section_for_headline(title, it.get("source") or "", it.get("language") or "en")
        tagged.append((it, tag))
    return tagged


# ---- C3 HELPERS: summaries + implications ----
def summarize_overall_brief(grouped: Dict[str, List[Dict[str, Any]]]) -> str:
    """80–120 word digest that summarizes the Daily Brief without repeating exact headlines."""
    lines = []
    for section, items in grouped.items():
        if not items:
            continue
        # feed only 2 items per section to keep it high-level
        previews = []
        for it in items[:2]:
            ttl = (it.get("title_en") or it.get("title") or "").strip()
            src = (it.get("source") or "").strip()
            previews.append(f"- {ttl} ({src})")
        lines.append(f"{section}:\n" + "\n".join(previews))
    context = "\n\n".join(lines) if lines else "No headlines today."
    msgs = [
        {"role": "system", "content": "You are a concise financial analyst."},
        {"role": "user", "content":
            "Write an 80–120 word 'Today at a glance' that SYNTHESIZES themes from the evidence below. "
            "Do NOT repeat exact headlines or phrases. No bullet points; 2–3 tight sentences; neutral tone.\n\n"
            f"{context}"
        },
    ]
    return call_llm(msgs, max_tokens=140, temperature=0.2) or ""


def summarize_section(section: str, items: List[Dict[str, Any]]) -> str:
    bullets = []
    for it in items[:8]:
        ttl = (it.get("title_en") or it.get("title") or "").strip()
        src = it.get("source") or ""
        bullets.append(f"- {ttl} ({src})")
    context = "\n".join(bullets) if bullets else "No items."
    msg = [
        {"role": "system", "content": "You are a financial analyst summarizing headlines."},
        {"role": "user", "content": (
            f"Section: {section}\n"
            "Write 2–3 sentences summarizing the common thread and implications. "
            "Synthesize; do not list.\n\n"
            f"Headlines:\n{context}"
        )},
    ]
    return call_llm(msg, max_tokens=220, temperature=0.2) or ""


def gather_evidence_snippets(grouped, max_sections: int = 5, max_items_per_section: int = 3) -> str:
    lines = []
    count_sections = 0
    for section, items in grouped.items():
        if not items:
            continue
        lines.append(f"{section}:")
        for it in items[:max_items_per_section]:
            title = (it.get("title_en") or it.get("title") or "").strip()
            src   = (it.get("source") or "").strip()
            lines.append(f"- {title} ({src})")
        lines.append("")
        count_sections += 1
        if count_sections >= max_sections:
            break
    return "\n".join(lines).strip() or "No evidence."


def write_implications_bullets(evidence_text: str) -> str:
    """
    Ask the LLM to produce 3–6 implication bullets that connect dots across sources.
    Bullets must cite >=2 distinct sources in square brackets, e.g., [Reuters; Nikkei].
    Gracefully returns '' if API is unavailable to preserve the rest of the page.
    """
    prompt = (
        "You are a concise financial analyst. Using the evidence below (sectioned headlines), "
        "write 3–6 bullets under the heading 'Why this matters'. Each bullet should:\n"
        "• connect at least two distinct items or themes (policy → sectors → tickers)\n"
        "• be specific and neutral (avoid hype)\n"
        "• end with square‑bracket source tags combining at least two short names, e.g., [Reuters; Nikkei]\n"
        "• avoid duplicate points\n\n"
        "Evidence:\n"
        f"{evidence_text}\n"
    )
    msgs = [
        {"role": "system", "content": "You write tight, factual finance bullets with clear implications."},
        {"role": "user", "content": prompt},
    ]
    # FIX: use `msgs` (plural), not `msg`
    return call_llm(msgs, max_tokens=260, temperature=0.2) or ""

#---------Dedupe Helper--------
def dedupe_items(items):
    """Remove near-duplicate headlines by normalized title (fallback to URL)."""
    seen = set()
    unique = []
    for it in items:
        title = (it.get("title_en") or it.get("title") or "").strip().lower()
        # normalize: alnum only + single spaces
        norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", title))
        key = norm or (it.get("url") or "")
        if key and key not in seen:
            seen.add(key)
            unique.append(it)
    return unique



#----------Sectioned Brief ----------
def build_sectioned_brief(items: List[Dict[str, Any]]) -> str:
    """
    Page layout:
    # Daily Financial Brief
    ## Today at a Glance        (LLM, concise synthesis)
    ## Daily Brief              (sections + mini-summaries + bullets)
    ## Why this matters         (LLM, 3–6 implication bullets)
    """
    # Group by tag
    tagged = tag_headlines(items)
    grouped: Dict[str, List[Dict[str, Any]]] = {k: [] for k in SECTION_ORDER}
    for it, tag in tagged:
        grouped.setdefault(tag, [])
        grouped[tag].append(it)

    # Top synthesis
    top_summary = summarize_overall_brief(grouped)

    # Evidence → implications
    evidence = gather_evidence_snippets(grouped, max_sections=5, max_items_per_section=3)
    why_matters = write_implications_bullets(evidence)

    md: List[str] = ["# Daily Financial Brief", ""]

    # 1) Today at a Glance
    if top_summary:
        md += ["## Today at a Glance", top_summary, ""]

    # 2) Daily Brief (sections)
    md += ["## Daily Brief", ""]
    for section in SECTION_ORDER:
        section_items = grouped.get(section, [])
        if not section_items:
            continue

        md.append(f"### {section}")
        # per-section mini-summary
        sec_sum = summarize_section(section, section_items)
        if sec_sum:
            md += ["", sec_sum, ""]

        # bullets
        for it in section_items:
            title = (it.get("title_en") or it.get("title") or "").strip()
            lang = (it.get("source_lang") or it.get("language") or "en")
            if lang != "en" and "translated from" not in title.lower():
                title = f"{title} [translated from {lang}]"
            src = it.get("source") or ""
            published = it.get("published_at") or ""
            md.append(f"- {title}  _(source: {src}; published: {published})_")
        md.append("")

    # 3) Why this matters (single block, at the end)
    if why_matters:
        md += ["## Why this matters", why_matters, ""]

    if not any(grouped.values()):
        return "# Daily Financial Brief\n\n_No headlines available today._"

    return "\n".join(md)


# ---------- SUMMARIZE ----------
def make_brief(items: List[Dict[str, Any]]) -> str:
    headlines = []
    for it in items:
        title = (it.get("title_en") or it.get("title") or "").strip()
        lang  = (it.get("source_lang") or it.get("language") or "en")
        if lang != "en":
           # Optional: mark that this line was translated
           title = f"{title} [translated from {lang}]"
            
        src = it.get("source") or ""
        published = it.get("published_at") or ""
        if title:
            headlines.append(f"- {title} (source: {src}; published: {published})")

    if not headlines:
        return "# Daily Brief\n\n_No headlines available today._"

    joined = "\n".join(headlines)
    prompt = f"""
You are a financial news analyst. Here are today's headlines:

{joined}

Tasks:
1) Remove duplicates already covered by others.
2) Group into 3–6 themes (Macro policy, Earnings/Guidance, Geopolitics, Commodities, Tech/AI, Energy).
3) Write a concise daily brief (~180–220 words) in bullet points.
4) Add a short "Why this matters" section (2–5 bullets) that connects dots across items.

Output:

# Daily Brief
- ...

## Why this matters
- ...
"""
    resp = client.chat.completions.create(
        model=MODEL_FOR_SUMMARY,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    text = resp.choices[0].message.content
    try:
        u = resp.usage
        print(f"🧮 tokens — prompt:{u.prompt_tokens} output:{u.completion_tokens} total:{u.total_tokens}")
    except Exception:
        pass
    return text


# ---------- STATIC SITE ----------
def build_static_site(out_path: str, today: str) -> None:
    import markdown, shutil

    os.makedirs("docs/days", exist_ok=True)
    nojekyll = "docs/.nojekyll"
    if not os.path.exists(nojekyll):
        open(nojekyll, "a").close()

    with open(out_path, "r", encoding="utf-8") as f:
        md_text = f.read()
    html_body = markdown.markdown(
    md_text,
    extensions=["extra", "sane_lists", "toc", "attr_list"]
)


    day_html_path = f"docs/days/{today}.html"
    page = f"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<title>Daily Financial Brief — {today}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/mvp.css">
<body><main class="container">
<header><h1>Daily Financial Brief — {today}</h1></header>
{html_body}
<hr><p><a href="../index.html">← Back to archive</a></p>
</main></body></html>"""
    with open(day_html_path, "w", encoding="utf-8") as f:
        f.write(page)

    pages = sorted([p for p in os.listdir("docs/days") if p.endswith(".html")], reverse=True)
    links = "\n".join([f'<li><a href="./days/{p}">{p[:-5]}</a></li>' for p in pages])
    index = f"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<title>Financial News Brief — Archive</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/mvp.css">
<body><main class="container">
<header><h1>Financial News Brief</h1><p>Auto-published daily.</p></header>
<p><strong>Latest:</strong> <a href="./days/{today}.html">{today}</a></p>
<h2>Archive</h2><ul>{links}</ul>
<p><a href="{SITE_BASE_URL}/latest.html">Stable link for today</a></p>
</main></body></html>"""
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(index)

    shutil.copyfile(day_html_path, "docs/latest.html")
    print("🌐 Built static site in docs/ (index.html, latest.html, and days/)")

#------Mailgun Email ------
def email_brief(today: str):
    import os, requests
    domain  = os.environ.get("MAILGUN_DOMAIN")
    api_key = os.environ.get("MAILGUN_API_KEY")
    to_addr = os.environ.get("MAILGUN_TO")  # optional recipient secret

    if not domain or not api_key or not to_addr:
        print("📧 Mailgun not configured (missing MAILGUN_DOMAIN/API_KEY/TO) — skipping email.")
        return

    html_path = f"docs/days/{today}.html"
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    r = requests.post(
        f"https://api.mailgun.net/v3/{domain}/messages",
        auth=("api", api_key),
        data={
            "from": f"FinancialNewsAI <mailgun@{domain}>",
            "to": [to_addr],
            "subject": f"Daily Financial Brief — {today}",
            "text": f"Read on the web: {SITE_BASE_URL}/latest.html",
            "html": html + f'<p><a href="{SITE_BASE_URL}/latest.html">Read on the web</a></p>',
        },
        timeout=30,
    )
    print("📧 Mailgun status:", r.status_code, r.text[:200])

import json, re


#------Translate------
def translate_non_english_titles(items):
    """Translate non-English item['title'] → item['title_en'] using OpenAI."""
    if not TRANSLATE_TO_EN:
        return items

    # Pick items that look non-English using Marketaux 'language' field
    to_xlate = [(i, it.get("title",""), it.get("language","")) 
                for i, it in enumerate(items) 
                if it.get("title") and it.get("language") and it.get("language") != "en"]
    if not to_xlate:
        return items

    # Build a compact list to translate in one shot
    lines = "\n".join([f"{i}\t{lang}\t{title}" for (i, title, lang) in to_xlate])

    prompt = f"""
You are a professional financial translator.
Translate each headline into natural English while preserving company names, tickers, and finance terms.
Return STRICT JSON: a list of objects like {{"i": <index>, "lang": "<src>", "en_title": "<english>"}}
No commentary, no markdown. Here are the items (tab-separated index,lang,title):

{lines}
"""

    resp = client.chat.completions.create(
        model=MODEL_FOR_SUMMARY,  # gpt-4o-mini is fine
        messages=[{"role":"user","content":prompt}],
        temperature=0.2,
    )
    content = resp.choices[0].message.content

    # Try to extract JSON safely
    try:
        data = json.loads(content)
    except Exception:
        # Best-effort: find the first JSON block
        m = re.search(r"\[.*\]", content, re.DOTALL)
        data = json.loads(m.group(0)) if m else []

    mapping = { int(d["i"]): d.get("en_title","") for d in data if "i" in d }
    langs   = { int(d["i"]): d.get("lang","")     for d in data if "i" in d }

    for idx, it in enumerate(items):
        if idx in mapping and mapping[idx]:
            it["title_en"] = mapping[idx]
            it["source_lang"] = langs.get(idx, it.get("language",""))
        else:
            # leave as-is if translation missing
            it["title_en"] = it.get("title")

    return items



# ---------- MAIN ----------
def main() -> None:
    os.makedirs("data/raw", exist_ok=True)
    os.makedirs("out", exist_ok=True)

    # --- collect items from all enabled sources (never crash if one fails) ---
    items = []

    # Marketaux (safe)
    items.extend(
        safe_fetch_marketaux_articles(
            MARKETAUX_API_KEY, PER_CALL_LIMIT, TARGET_ARTICLES, MARKETAUX_PARAMS
        )
    )

    # GDELT (optional; only if you added the fetcher previously)
    if os.getenv("ENABLE_GDELT", "1") == "1":
        gdelt_items = fetch_gdelt(
            os.getenv("GDELT_QUERY", "finance OR market OR stocks OR earnings OR inflation OR central bank"),
            max_records=int(os.getenv("GDELT_MAXREC", "40")),
            hours_back=int(os.getenv("GDELT_HOURS_BACK", "24")),
        )

        print(f"[GDELT] fetched {len(gdelt_items)}")
        items.extend(gdelt_items)

    # quality pass
    items = dedupe_items(items)                 # remove repeats across sources
    items = translate_non_english_titles(items) # C1: fill title_en

    print(f"Fetched {len(items)} article(s).")

    today = str(date.today())

    # CSV index (handy for Excel)
    with open(f"out/{today}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["published_at", "source", "title", "url"])
        for it in items:
            w.writerow([it.get("published_at", ""), it.get("source", ""), it.get("title", ""), it.get("url", "")])

    # Save raw JSON
    raw_path = f"data/raw/{today}.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump({"count": len(items), "data": items}, f, ensure_ascii=False, indent=2)

    # --- (optional) add GDELT items if enabled ---
    #if ENABLE_GDELT == "1":
    #   gdelt_items = fetch_gdelt(GDELT_QUERY, max_records=GDELT_MAXREC, timespan=GDELT_TIMESPAN)
    #   print(f"[GDELT] fetched {len(gdelt_items)}")
    #   items.extend(gdelt_items)

    # now dedupe across sources
    items = dedupe_items(items)


    # Build brief (Markdown)
    flat_brief_md = make_brief(items)
    sectioned_brief_md = build_sectioned_brief(items)
    
    # Choose what to publish: "flat" | "sectioned"
    brief_mode = os.getenv("BRIEF_MODE", "sectioned").lower()

    if brief_mode == "flat":
       out_text = flat_brief_md
    else:
       out_text = sectioned_brief_md  # no "combined" to avoid repetition

    out_path = f"out/{today}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out_text)

    # Static site
    build_static_site(out_path, today)
    email_brief(today)

    print(f"✅ Saved raw  → {raw_path}")
    print(f"✅ Saved brief → {out_path}")
    print(f"🌍 Live site:   {SITE_BASE_URL}/")
    print(f"⭐ Today link:  {SITE_BASE_URL}/latest.html")
    print(f"📅 Permalink:   {SITE_BASE_URL}/days/{today}.html")


if __name__ == "__main__":
    main()
