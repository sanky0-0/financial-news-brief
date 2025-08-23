import os, time, json
from datetime import date
from typing import List, Dict, Any
import requests
from openai import OpenAI

# ========= 1) CONFIG (edit these) =========
TARGET_ARTICLES = 24     # change to 15/18/21/24 as you like
PER_CALL_LIMIT  = 3      # free plan cap per request
REQUEST_DELAY_S = 0.6    # gentle delay to be nice to the API
MARKETAUX_PARAMS = {
    # optional filters: uncomment/edit as you prefer
    # "countries": "us,gb,eu",       # example
    # "language": "en",              # or leave broad to get more
    "filter_entities": "true"
}
MODEL_FOR_SUMMARY = "gpt-4o-mini"

# ========= 2) KEYS =========
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY", "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")
MARKETAUX_API_KEY = os.getenv("MARKETAUX_API_KEY", "YHSWx3f3vLBXKygJcRliXcHE6D6gbz7YEnMwdVQ2")

client = OpenAI(api_key=OPENAI_API_KEY)

# ========= 3) FETCH (paged) =========
def fetch_marketaux_articles(
    api_key: str,
    per_call_limit: int,
    target: int,
    extra_params: Dict[str, str]
) -> List[Dict[str, Any]]:
    """Fetch up to `target` articles in batches of `per_call_limit` using page=1..N."""
    base = "https://api.marketaux.com/v1/news/all"
    page = 1
    results: List[Dict[str, Any]] = []
    seen = set()

    while len(results) < target:
        params = {
            "api_token": api_key,
            "limit": str(per_call_limit),
            "page": str(page),
            **extra_params,
        }
        r = requests.get(base, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()

        # Handle warnings from free tier gracefully
        warnings = payload.get("warnings") or []
        if warnings:
            print("⚠️ Marketaux warning:", "; ".join(warnings))

        items = payload.get("data") or []
        if not items:
            # nothing more to fetch
            break

        # Deduplicate by uuid/url/title
        added_this_page = 0
        for it in items:
            uid = it.get("uuid") or it.get("url") or it.get("title", "")
            if not uid:
                continue
            if uid in seen:
                continue
            seen.add(uid)
            results.append(it)
            added_this_page += 1
            if len(results) >= target:
                break

        # If the API returned 0 new uniques, bail to avoid infinite loops
        if added_this_page == 0:
            break

        page += 1
        time.sleep(REQUEST_DELAY_S)

    return results[:target]

# ========= 4) SUMMARIZE =========
def make_brief(items: List[Dict[str, Any]]) -> str:
    headlines = []
    for it in items:
        title = it.get("title") or ""
        src = it.get("source") or ""
        published = it.get("published_at") or ""
        # compact source/published to help the model with provenance
        if title:
            headlines.append(f"- {title} (source: {src}; published: {published})")
    if not headlines:
        return "No headlines available today."

    joined = "\n".join(headlines)

    prompt = f"""
You are a financial news analyst. Here are today's headlines (with sources and timestamps):

{joined}

Tasks:
1) Remove duplicates already covered by others.
2) Group items into 3–6 clear themes (e.g., Macro policy, Earnings/Guidance, Geopolitics, Commodities, Tech/AI, Energy).
3) Produce a concise daily brief (~180–220 words) with bullet points.
4) Add a short "Why this matters" section (2–5 bullets) that connects dots across items (e.g., policy → sector → company impact).

Output format:
# Daily Brief
- ...
- ...

## Why this matters
- ...
- ...
"""
    resp = client.chat.completions.create(
        model=MODEL_FOR_SUMMARY,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )
    return resp.choices[0].message.content

# ========= 5) MAIN RUN =========
def main():
    os.makedirs("data/raw", exist_ok=True)
    os.makedirs("out", exist_ok=True)

    items = fetch_marketaux_articles(
        MARKETAUX_API_KEY, PER_CALL_LIMIT, TARGET_ARTICLES, MARKETAUX_PARAMS
    )
    count = len(items)
    print(f"Fetched {count} article(s).")

    today = str(date.today())

    # Save raw combined JSON
    raw_path = f"data/raw/{today}.json"
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump({"count": count, "data": items}, f, ensure_ascii=False, indent=2)

    # Build the brief
    brief = make_brief(items)

    # Save brief
    out_path = f"out/{today}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(brief)



# --- begin: static site export ---
import markdown, shutil, os

os.makedirs("docs/days", exist_ok=True)

# read today's markdown
with open(out_path, "r", encoding="utf-8") as f:
    md_text = f.read()

# convert to HTML
html_body = markdown.markdown(md_text, extensions=["extra", "sane_lists"])

# simple HTML shell
PAGE_TEMPLATE = f"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<title>Daily Financial Brief — {today}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/mvp.css">  <!-- lightweight default styling -->
<body>
<main class="container">
  <header><h1>Daily Financial Brief — {today}</h1></header>
  {html_body}
  <hr>
  <p><a href="../index.html">← Back to archive</a></p>
</main>
</body></html>"""

# write the per‑day page
day_html_path = f"docs/days/{today}.html"
with open(day_html_path, "w", encoding="utf-8") as f:
    f.write(PAGE_TEMPLATE)

# rebuild archive index (links newest → oldest)
pages = sorted([p for p in os.listdir("docs/days") if p.endswith(".html")], reverse=True)
links = "\n".join([f'<li><a href="./days/{p}">{p[:-5]}</a></li>' for p in pages])

INDEX = f"""<!doctype html>
<html lang="en"><meta charset="utf-8">
<title>Financial News Brief — Archive</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/mvp.css">
<body>
<main class="container">
  <header><h1>Financial News Brief</h1><p>Auto‑published daily.</p></header>
  <p><strong>Latest:</strong> <a href="./days/{today}.html">{today}</a></p>
  <h2>Archive</h2>
  <ul>{links}</ul>
</main>
</body></html>"""

with open("docs/index.html", "w", encoding="utf-8") as f:
    f.write(INDEX)
print("🌐 Built static site in docs/")
# --- end: static site export ---



    
        
    print(f"✅ Saved raw → {raw_path}")
    print(f"✅ Saved brief → {out_path}")

if __name__ == "__main__":
    main()
