# daily_brief.py — Financial News Brief (Overhauled v2)
# Model: DeepSeek V4 Pro via OpenRouter, reasoning=medium
import os, json, time, csv, re
from datetime import date, datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple, Optional
import requests
from openai import OpenAI
from urllib.parse import urlparse, urlencode

# ---------- CONFIG ----------
TARGET_ARTICLES = 99
PER_CALL_LIMIT = 3
REQUEST_DELAY_S = 0.6
MODEL = "deepseek/deepseek-v4-pro"
REASONING = {"extra_body": {"reasoning": {"effort": "medium"}}}
SITE_BASE_URL = os.getenv("SITE_BASE_URL", "https://sanky0-0.github.io/financial-news-brief")
TRANSLATE_TO_EN = os.getenv("TRANSLATE", "1") == "1"
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# ---------- KEYS ----------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
MARKETAUX_API_KEY = os.environ.get("MARKETAUX_API_KEY")
missing = []
if not OPENROUTER_API_KEY:
    missing.append("OPENROUTER_API_KEY")
if not MARKETAUX_API_KEY:
    missing.append("MARKETAUX_API_KEY")
if missing:
    print(f"[WARN] Missing: {', '.join(missing)}. Some features disabled.")

client = OpenAI(
    api_key=OPENROUTER_API_KEY or "dummy",
    base_url="https://openrouter.ai/api/v1"
)

# ---------- LLM WRAPPER ----------
def call_llm(messages, max_tokens=300, temperature=0.2, model=MODEL, extra_params=None):
    try:
        kwargs = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
        if extra_params:
            kwargs.update(extra_params)
        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[LLM] error: {e}")
        return ""

# ---------- STATE FILE ----------
STATE_PATH = "run_state.json"
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"last_run_date": None}
def save_state(today):
    with open(STATE_PATH, "w") as f:
        json.dump({"last_run_date": today}, f)

# ---------- FETCH HELPERS ----------
def fetch_marketaux_articles(api_key, per_call_limit, target, extra_params):
    base = "https://api.marketaux.com/v1/news/all"
    page = 1
    results = []
    seen = set()
    while len(results) < target:
        params = {"api_token": api_key, "limit": str(per_call_limit), "page": str(page)}
        params.update(extra_params)
        r = requests.get(base, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
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

def safe_fetch_marketaux(api_key, per_call_limit, target, params):
    if os.getenv("ENABLE_MARKETAUX", "0") != "1":
        return []
    try:
        return fetch_marketaux_articles(api_key, per_call_limit, target, params)
    except Exception as e:
        print(f"[Marketaux] error: {e}")
        return []

def _gdelt_stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")

def _normalize_gdelt_query(q):
    q = (q or "").strip()
    if not q:
        return "(finance OR market)"
    q = q.replace("central bank", '"central bank"').replace("interest rate", '"interest rate"')
    if " OR " in q and "(" not in q:
        q = f"({q})"
    return q

def fetch_gdelt(query="finance OR market OR stocks OR earnings OR inflation OR central bank",
                max_records=250, hours_back=24):
    base = "https://api.gdeltproject.org/api/v2/doc/doc"
    headers = {"User-Agent": "financial-news-ai/2.0"}
    q = _normalize_gdelt_query(query)
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=hours_back)
    params = {
        "query": q, "mode": "ArtList", "maxrecords": str(max_records),
        "sort": "DateDesc", "format": "json",
        "startdatetime": _gdelt_stamp(start_dt), "enddatetime": _gdelt_stamp(end_dt),
    }
    try:
        r = requests.get(base, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        if "application/json" not in r.headers.get("content-type", "").lower():
            return []
        data = r.json()
        arts = data.get("articles", []) or []
    except Exception as e:
        print(f"[GDELT] error: {e}")
        return []
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
        out.append({"title": title, "title_en": None, "source": domain or "GDELT",
                     "url": url_, "published_at": published_iso, "language": lang or None,
                     "source_lang": lang or None, "provider": "gdelt"})
    return out

# ---------- SECTION ARCHITECTURE (10 sections) ----------
SECTION_ORDER = [
    "Macro & Policy", "Earnings & Guidance", "AI & Technology",
    "AI Products & Launches", "Semiconductors", "Energy",
    "Rates & Central Banks", "Geopolitics", "Other"
]

TAG_KEYWORDS = {
    "Macro & Policy": [
        r"\b(inflation|cpi|ppi|gdp|employment|jobs report|tariff|fiscal|budget|stimulus|subsidy|regulation|policy|imf|world bank|pboC|ecb|boj|fed|fomc|recession|slowdown|consumer spending|retail sales|manufacturing pmi|services pmi|trade deficit|current account)\b",
    ],
    "Earnings & Guidance": [
        r"\b(earnings|revenue|guidance|profit|loss|quarter|q[1-4]\b|beat|miss|outlook|eps|ebitda|margin|buyback|dividend|forecast)\b"
    ],
    "AI & Technology": [
        r"\b(ai|artificial intelligence|llm|large language model|foundation model|machine learning|deep learning|neural network|transformer|gpt|claude|gemini|llama|mistral|open source model|fine.?tune|rag|agent|ai agent|ai safety|alignment|reasoning|inference)\b"
    ],
    "AI Products & Launches": [
        r"\b(product launch|beta|release|announce.*new|unveil|debut|introduc.*(ai|agent|model)|api release|preview|early access|ai.*feature|ai.*tool|ai.*platform|ai.*assistant|ai.*copilot)\b",
        r"\b(meta.*llama|openai.*(gpt|o1|o3)|anthropic.*claude|google.*gemini|mistral.*|xai.*grok|cohere.*|stability.*|hugging face)\b"
    ],
    "Semiconductors": [
        r"\b(semiconductor|chip|gpu|foundry|tsmc|samsung.*foundry|intel.*foundry|nvidia|amd|asic|fpga|hbm|dram|nand|wafer|node|nanometer|nm|euv|packaging|chiplet|advanced packaging|supply chain.*chip|chip.*shortage|fab|fabrication)\b"
    ],
    "Energy": [
        r"\b(oil|gas|lng|coal|uranium|nuclear|solar|wind|renewable|opec|brent|wti|natural gas|crude|refinery|offshore|drilling|exploration|pipeline|energy.*crisis|power.*grid|electricity)\b"
    ],
    "Rates & Central Banks": [
        r"\b(rate hike|rate cut|interest rate|yield|treasury|bond|spread|dovish|hawkish|dot plot|forward guidance|monetary policy|tightening|loosening|quantitative easing|quantitative tightening|term premium|curve)\b"
    ],
    "Geopolitics": [
        r"\b(conflict|war|ceasefire|election|coup|sanction|diplomatic|border|strait|taiwan|ukraine|middle east|red sea|houthi|iran|israel|gaza|russia|china.*military|nato|defense|military.*aid|arms|trade.*war|tariff.*(china|eu|us))\b"
    ],
}

def choose_section(title):
    t = (title or "").lower()
    for section, patterns in TAG_KEYWORDS.items():
        for pat in patterns:
            if re.search(pat, t):
                return section
    return "Other"

def tag_headlines(items):
    tagged = []
    for it in items:
        title = (it.get("title_en") or it.get("title") or "").strip()
        tagged.append((it, choose_section(title)))
    return tagged

def dedupe_items(items):
    seen = set()
    unique = []
    for it in items:
        title = (it.get("title_en") or it.get("title") or "").strip().lower()
        norm = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", title))
        key = norm or (it.get("url") or "")
        if key and key not in seen:
            seen.add(key)
            unique.append(it)
    return unique

# ---------- TRANSLATION ----------
def translate_non_english(items):
    if not TRANSLATE_TO_EN:
        return items
    to_xlate = [(i, it.get("title",""), it.get("language","")) 
                for i, it in enumerate(items) 
                if it.get("title") and it.get("language") and it.get("language") != "en"]
    if not to_xlate:
        return items
    batch = []
    for i, ttl, lang in to_xlate:
        batch.append(f"[{i}] ({lang}) {ttl}")
    if not batch:
        return items
    prompt = "Translate each headline to English. Keep the [N] prefix. Output one per line:\n\n" + "\n".join(batch)
    msgs = [{"role":"system","content":"You translate headlines. Keep [N] prefix. Output one per line."},
            {"role":"user","content":prompt}]
    result = call_llm(msgs, max_tokens=2000, temperature=0.1, extra_params=REASONING)
    if not result:
        return items
    for line in result.split("\n"):
        m = re.match(r"\[(\d+)\]\s*(.*)", line.strip())
        if m:
            idx = int(m.group(1))
            translated = m.group(2).strip()
            if idx < len(items) and translated:
                items[idx]["title_en"] = translated
    return items

# ---------- TODAY AT A GLANCE (Guiding Summary) ----------
def write_glance(grouped):
    items_summary = []
    for section, articles in grouped.items():
        if not articles:
            continue
        top = articles[0]
        ttl = (top.get("title_en") or top.get("title") or "").strip()
        items_summary.append(f"{section}: {ttl}")
    context = "\n".join(items_summary) if items_summary else "No headlines today."
    prompt = (
        "You are writing 'Today at a Glance' — a 3-6 sentence guide to today's financial news brief.\n"
        "Tell the reader:\n"
        "- What's most interesting today\n"
        "- What to pay attention to\n"
        "- What to watch for in the future\n"
        "- Why this day matters\n\n"
        "Be specific. Name companies, tickers, people. Do not use bullet points — flowing prose.\n"
        "Focus on what's actionable and notable.\n\n"
        f"Today's headlines by section:\n{context}"
    )
    msgs = [{"role":"system","content":"You write tight, specific financial guidance. Name names. Give context."},
            {"role":"user","content":prompt}]
    return call_llm(msgs, max_tokens=300, temperature=0.3, extra_params=REASONING)

# ---------- SECTION BRIEF (Headline bulletins + Why this matters) ----------
def write_section_brief(section, items):
    if not items:
        return "", ""
    # Headline bulletins
    bullets = []
    for it in items[:12]:
        ttl = (it.get("title_en") or it.get("title") or "").strip()
        url_ = it.get("url") or ""
        src = (it.get("source") or "").strip()
        if url_:
            b = f"**{ttl}** — [→ {src}]({url_})"
        else:
            b = f"**{ttl}** — ({src})"
        bullets.append(b)
    headlines = "\n".join(bullets)
    
    # Why this matters (per section)
    context = "\n".join([f"- {(it.get('title_en') or it.get('title') or '').strip()} ({(it.get('source') or '').strip()})" for it in items[:8]])
    prompt = (
        f"Section: {section}\n\n"
        f"Headlines:\n{context}\n\n"
        "Write 'Why this matters' for THIS section only — 2-4 bullet points.\n"
        "Be SPECIFIC: name companies, tickers, sectors, numbers, percentages.\n"
        "Connect the dots between headlines. What's the implication?\n"
        "Example: 'NVIDIA's new GPU announcement puts pressure on AMD (AMD) — watch for competitive response next week.'\n"
        "NOT vague: 'This highlights the ongoing challenges in the tech sector.'\n"
    )
    msgs = [{"role":"system","content":"You write specific, actionable financial analysis. Name names and numbers."},
            {"role":"user","content":prompt}]
    why = call_llm(msgs, max_tokens=250, temperature=0.3, extra_params=REASONING)
    return headlines, why

# ---------- OTHER SECTION (Ticker format, gated, translated) ----------
def write_other_brief(items):
    if not items:
        return "", ""
    # Take max 8 items, flag country where possible
    entries = []
    for it in items[:8]:
        ttl = (it.get("title_en") or it.get("title") or "").strip()
        src = (it.get("source") or "").strip()
        url_ = it.get("url") or ""
        # Try to extract country flag from title
        countries = {"🇺🇸":"US","🇨🇳":"China","🇪🇺":"EU","🇯🇵":"Japan","🇬🇧":"UK","🇩🇪":"Germany",
                     "🇫🇷":"France","🇮🇳":"India","🇧🇷":"Brazil","🇰🇷":"Korea","🇷🇺":"Russia"}
        flag = "🌐"
        for emoji, name in countries.items():
            if name.lower() in ttl.lower():
                flag = emoji
                break
        if url_:
            entries.append(f"- {flag} **{ttl}** [→ {src}]({url_})")
        else:
            entries.append(f"- {flag} **{ttl}** ({src})")
    headlines = "\n".join(entries)
    # Why matters for Other
    context = "\n".join([f"- {(it.get('title_en') or it.get('title') or '').strip()}" for it in items[:5]])
    prompt = (
        "From these miscellaneous headlines, write 1-2 'Why this matters' bullets.\n"
        "Only if there's a clear signal. If all noise, say 'No significant implications.'\n\n"
        f"Items:\n{context}"
    )
    msgs = [{"role":"system","content":"You are a financial analyst. Be concise. Only flag if there's a real signal."},
            {"role":"user","content":prompt}]
    why = call_llm(msgs, max_tokens=150, temperature=0.2, extra_params=REASONING)
    return headlines, why

# ---------- BUILD THE BRIEF ----------
def build_brief(items):
    tagged = tag_headlines(items)
    grouped = {k: [] for k in SECTION_ORDER}
    for it, tag in tagged:
        grouped.setdefault(tag, []).append(it)
    
    # "Today at a Glance" guiding summary
    glance = write_glance(grouped)
    
    md = ["# Daily Financial Brief", ""]
    if glance:
        md += ["## Today at a Glance", glance, ""]
    
    md += ["## Daily Brief", ""]
    for section in SECTION_ORDER:
        section_items = grouped.get(section, [])
        if not section_items:
            continue
        md.append(f"### {section}")
        if section == "Other":
            head, why = write_other_brief(section_items)
        else:
            head, why = write_section_brief(section, section_items)
        if head:
            md.append(head)
            md.append("")
        if why:
            md.append("**Why this matters:**")
            md.append(why)
            md.append("")
    
    if not any(grouped.values()):
        return "# Daily Financial Brief\n\n_No headlines available today._"
    
    return "\n".join(md)

# ---------- OUTPUT ----------
def build_static_site(md_text, today):
    import markdown, shutil
    os.makedirs("docs/days", exist_ok=True)
    nojekyll = "docs/.nojekyll"
    if not os.path.exists(nojekyll):
        open(nojekyll, "a").close()
    
    html_body = markdown.markdown(md_text, extensions=["extra", "sane_lists", "toc", "attr_list"])
    day_html = f"docs/days/{today}.html"
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
    with open(day_html, "w", encoding="utf-8") as f:
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
    shutil.copyfile(day_html, "docs/latest.html")
    print(f"🌐 Built: docs/ (index.html, latest.html, days/{today}.html)")

def email_brief(today):
    """Send the brief via Mailgun if configured. Silent no-op if not."""
    domain = os.environ.get("MAILGUN_DOMAIN")
    api_key = os.environ.get("MAILGUN_API_KEY")
    to_addr = os.environ.get("MAILGUN_TO")
    if not domain or not api_key or not to_addr:
        return
    html_path = f"docs/days/{today}.html"
    if not os.path.exists(html_path):
        return
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    try:
        r = requests.post(
            f"https://api.mailgun.net/v3/{domain}/messages",
            auth=("api", api_key),
            data={"from": f"FinancialBrief <mailgun@{domain}>", "to": [to_addr],
                  "subject": f"Daily Financial Brief — {today}",
                  "html": html + f'<p><a href="{SITE_BASE_URL}/latest.html">Read on web</a></p>'},
            timeout=30)
        print(f"📧 Email sent: {r.status_code}")
    except Exception as e:
        print(f"📧 Email error: {e}")

def save_csv(items, today):
    with open(f"out/{today}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["title", "title_en", "source", "url", "published_at", "language", "section"])
        tagged = tag_headlines(items)
        for it, tag in tagged:
            w.writerow([it.get("title",""), it.get("title_en",""), it.get("source",""),
                        it.get("url",""), it.get("published_at",""), it.get("language",""), tag])

def save_json(items, today):
    os.makedirs("data/raw", exist_ok=True)
    with open(f"data/raw/{today}.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

# ---------- LOOM / GRIMOIRE INTEGRATION ----------
def save_to_grimoire(md_text, today):
    archive_path = os.path.expanduser("~/archive")
    atoms_dir = os.path.join(archive_path, "atoms")
    log_path = os.path.join(archive_path, "log.md")
    os.makedirs(atoms_dir, exist_ok=True)
    
    # Save as atom
    hour = datetime.now().strftime("%H%M")
    atom_path = os.path.join(atoms_dir, f"{today.replace('-','')}{hour}-financial-brief.md")
    with open(atom_path, "w", encoding="utf-8") as f:
        f.write(f"---\ntitle: \"Daily Financial Brief — {today}\"\ncreated: {today}\nupdated: {today}\ntype: atom\ntags: [financial, daily-brief, news]\n---\n\n{md_text}")
    
    # Append to log
    with open(log_path, "a") as f:
        f.write(f"## [{today}] ingest | Daily Financial Brief — {today}\n")
        f.write(f"- Created: atoms/{os.path.basename(atom_path)}\n")
        f.write(f"- Site: {SITE_BASE_URL}/days/{today}.html\n\n")
    print(f"📚 Saved to Grimoire: {atom_path}")

# ---------- MAIN ----------
def main():
    today = date.today().isoformat()
    print(f"=== Daily Financial Brief — {today} ===")
    
    # State check
    state = load_state()
    if state.get("last_run_date") == today and not DRY_RUN:
        print(f"[SKIP] Already ran for {today}")
        return
    
    if DRY_RUN:
        print("[DRY RUN] — limited fetch, one section test")
    
    # Fetch
    all_items = []
    if os.getenv("ENABLE_GDELT", "1") == "1":
        print("[Fetch] GDELT...")
        all_items.extend(fetch_gdelt(max_records=250 if not DRY_RUN else 20))
    if os.getenv("ENABLE_MARKETAUX", "0") == "1":
        print("[Fetch] Marketaux...")
        all_items.extend(safe_fetch_marketaux(MARKETAUX_API_KEY, PER_CALL_LIMIT, 
                                               TARGET_ARTICLES if not DRY_RUN else 5, 
                                               {"filter_entities": "true"}))
    
    print(f"  Fetched: {len(all_items)} articles")
    all_items = dedupe_items(all_items)
    print(f"  After dedup: {len(all_items)}")
    
    if not all_items:
        print("[WARN] No articles. Skipping.")
        return
    
    # Translate
    if TRANSLATE_TO_EN:
        print("[Translate]...")
        all_items = translate_non_english(all_items)
    
    # Build brief
    print("[Build]...")
    md_text = build_brief(all_items)
    
    if DRY_RUN:
        print("\n" + "="*60)
        print("DRY RUN OUTPUT:")
        print("="*60)
        print(md_text[:2000])
        print("... (truncated)")
        print("="*60)
        print("[DRY RUN] — no files written, no state saved")
        return
    
    # Save outputs
    print("[Save]...")
    os.makedirs("out", exist_ok=True)
    with open(f"out/{today}.md", "w", encoding="utf-8") as f:
        f.write(md_text)
    save_json(all_items, today)
    save_csv(all_items, today)
    build_static_site(md_text, today)
    email_brief(today)  # silent no-op if Mailgun not configured
    
    # Grimoire
    save_to_grimoire(md_text, today)
    
    # State
    save_state(today)
    print(f"[DONE] Brief for {today} complete.")

if __name__ == "__main__":
    main()