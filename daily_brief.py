# daily_brief.py  — clean, spaces-only version
import os, json, time, csv
from datetime import date
from typing import List, Dict, Any
import requests
from openai import OpenAI

# ---------- CONFIG ----------
TARGET_ARTICLES = 24
PER_CALL_LIMIT = 3
REQUEST_DELAY_S = 0.6
MODEL_FOR_SUMMARY = "gpt-4o-mini"
SITE_BASE_URL = "https://sanky0-0.github.io/financial-news-brief"  # change this

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


# ---------- SUMMARIZE ----------
def make_brief(items: List[Dict[str, Any]]) -> str:
    headlines = []
    for it in items:
        title = it.get("title") or ""
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
    html_body = markdown.markdown(md_text, extensions=["extra", "sane_lists"])

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
    import requests
    html_path = f"docs/days/{today}.html"
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    resp = requests.post(
        f"https://api.mailgun.net/v3/{os.environ['MAILGUN_DOMAIN']}/messages",
        auth=("api", os.environ['MAILGUN_API_KEY']),
        data={
            "from": f"FinancialNewsAI <mailgun@{os.environ['MAILGUN_DOMAIN']}>",
            "to": ["sankalpogale@gmail.com"],  # change this to your email
            "subject": f"Daily Financial Brief — {today}",
            "text": f"Read on the web: {SITE_BASE_URL}/latest.html",
            "html": html + f'<p><a href="{SITE_BASE_URL}/latest.html">Read on the web</a></p>',
        },
        timeout=30,
    )
    print("📧 Mailgun status:", resp.status_code)



# ---------- MAIN ----------
def main() -> None:
    os.makedirs("data/raw", exist_ok=True)
    os.makedirs("out", exist_ok=True)

    items = fetch_marketaux_articles(MARKETAUX_API_KEY, PER_CALL_LIMIT, TARGET_ARTICLES, MARKETAUX_PARAMS)
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

    # Build brief (Markdown)
    brief = make_brief(items)
    out_path = f"out/{today}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(brief)

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
