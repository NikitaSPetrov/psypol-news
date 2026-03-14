#!/usr/bin/env python3
"""
news-scan.py — Fetch RSS feeds in parallel, deduplicate, strip already-seen
stories, and output scan.tsv + scan.json for editorial triage.

Outputs:
  scan.tsv  — tab-separated for quick grep/review
  scan.json — structured data with URLs from RSS feeds (source of truth)

IMPORTANT: When building candidates.json, URLs MUST come from scan.json.
Never reconstruct or guess URLs — copy them verbatim from scan.json items.

Stdlib only. No pip installs.
"""

import csv
import hashlib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from urllib.request import urlopen, Request

# ---------------------------------------------------------------------------
# Feed registry
# ---------------------------------------------------------------------------

FEEDS = [
    ("Google News — Top Stories",
     "https://news.google.com/rss"),
    ("Google News — World",
     "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtVnVHZ0pWVXlnQVAB"),
    ("Google News — Science",
     "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp0Y1RjU0FtVnVHZ0pWVXlnQVAB"),
    ("Google News — Technology",
     "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGRqTVhZU0FtVnVHZ0pWVXlnQVAB"),
    ("Meduza",
     "https://meduza.io/rss/all"),
    ("Ars Technica",
     "https://feeds.arstechnica.com/arstechnica/index"),
    ("Rest of World",
     "https://restofworld.org/feed/"),
    ("Wired",
     "https://www.wired.com/feed/rss"),
    ("404 Media",
     "https://www.404media.co/rss/"),
    ("Nature",
     "https://www.nature.com/nature.rss"),
    ("New Scientist",
     "https://www.newscientist.com/feed/home/"),
    ("Futurism",
     "https://futurism.com/feed"),
    ("ScienceAlert",
     "https://www.sciencealert.com/feed"),
    ("Phys.org",
     "https://phys.org/rss-feed/"),
    ("Al Jazeera",
     "https://www.aljazeera.com/xml/rss/all.xml"),
    ("South China Morning Post",
     "https://www.scmp.com/rss/91/feed"),
    ("RFE/RL",
     "https://www.rferl.org/api/z-pqpiev-qpp"),
    ("The Intercept",
     "https://theintercept.com/feed/?rss"),
    ("Science (AAAS)",
     "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science"),
]

TIMEOUT = 10  # seconds per feed
SCRIPT_DIR = Path(__file__).resolve().parent
REALITY_MD = SCRIPT_DIR / "content" / "reality.md"
OUTPUT_TSV = SCRIPT_DIR / "scan.tsv"
OUTPUT_JSON = SCRIPT_DIR / "scan.json"

# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Strip utm_ params, trailing slashes, fragments for dedup."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=False)
    cleaned = {k: v for k, v in params.items() if not k.startswith("utm_")}
    query = urlencode(cleaned, doseq=True)
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, ""))


# ---------------------------------------------------------------------------
# Google News URL resolution
# ---------------------------------------------------------------------------

def resolve_google_news_url(item_link: str, item_xml: ET.Element) -> str:
    """
    Google News wraps real URLs in redirects. Try to extract the actual
    source URL from the XML first; fall back to following the redirect.
    """
    # Strategy 1: <source url="..."> element
    source_el = item_xml.find("source")
    if source_el is not None:
        source_url = source_el.get("url", "")
        if source_url and "google.com" not in source_url:
            return source_url

    # Strategy 2: look for a non-Google URL in <link> text
    link_el = item_xml.find("link")
    if link_el is not None and link_el.text:
        link_text = link_el.text.strip()
        if "google.com" not in link_text:
            return link_text

    # Strategy 3: follow the redirect
    try:
        req = Request(item_link, method="HEAD")
        req.add_header("User-Agent", "Mozilla/5.0")
        with urlopen(req, timeout=TIMEOUT) as resp:
            return resp.url
    except Exception:
        pass

    return item_link


# ---------------------------------------------------------------------------
# Feed fetching & parsing
# ---------------------------------------------------------------------------

def fetch_feed(source_name: str, feed_url: str) -> list[dict]:
    """Fetch one RSS feed and return a list of item dicts."""
    req = Request(feed_url)
    req.add_header("User-Agent", "Mozilla/5.0 (compatible; PsyPolNewsScan/1.0)")

    with urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read()

    root = ET.fromstring(data)
    items = []

    # Handle both RSS 2.0 (<channel><item>) and Atom (<entry>)
    # RSS 2.0
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pub_el = item.find("pubDate")

        title = html.unescape(title_el.text.strip()) if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

        # Google News: resolve wrapped URLs
        if "news.google.com" in feed_url and link:
            link = resolve_google_news_url(link, item)

        title_ru = ""
        if source_name == "Meduza":
            title_ru = title

        items.append({
            "source": source_name,
            "title": title,
            "url": link,
            "pub_date": pub_date,
            "title_ru": title_ru,
        })

    # Atom feeds (<feed><entry>)
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            pub_el = entry.find("atom:published", ns) or entry.find("atom:updated", ns)

            title = html.unescape(title_el.text.strip()) if title_el is not None and title_el.text else ""
            link = link_el.get("href", "") if link_el is not None else ""
            pub_date = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

            items.append({
                "source": source_name,
                "title": title,
                "url": link,
                "pub_date": pub_date,
                "title_ru": "",
            })

    return items


# ---------------------------------------------------------------------------
# Already-seen check
# ---------------------------------------------------------------------------

def load_seen_urls() -> set[str]:
    """Read reality.md and extract all URLs from markdown links."""
    seen = set()
    if not REALITY_MD.exists():
        return seen
    text = REALITY_MD.read_text(encoding="utf-8")
    # Match markdown link pattern: [text](url)
    for match in re.finditer(r"\]\((https?://[^\)]+)\)", text):
        seen.add(normalize_url(match.group(1)))
    return seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # 1. Fetch all feeds in parallel
    all_items: list[dict] = []
    feeds_ok = 0

    with ThreadPoolExecutor(max_workers=len(FEEDS)) as pool:
        futures = {
            pool.submit(fetch_feed, name, url): name
            for name, url in FEEDS
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                items = future.result()
                all_items.extend(items)
                feeds_ok += 1
            except Exception as exc:
                print(f"  WARNING: {name} — {exc}", file=sys.stderr)

    total_fetched = len(all_items)

    # 2. Deduplicate by normalized URL (keep first seen)
    seen_norm: dict[str, dict] = {}
    duplicates = 0
    for item in all_items:
        norm = normalize_url(item["url"])
        if norm in seen_norm:
            duplicates += 1
        else:
            seen_norm[norm] = item

    deduped = list(seen_norm.values())

    # 3. Check against already-published URLs
    published_urls = load_seen_urls()
    new_items = []
    seen_items = []

    for item in deduped:
        norm = normalize_url(item["url"])
        if norm in published_urls:
            item["status"] = "seen"
            seen_items.append(item)
        else:
            item["status"] = "new"
            new_items.append(item)

    # 4. Sort: new first (grouped by source), then seen (grouped by source)
    source_order = {name: i for i, (name, _) in enumerate(FEEDS)}
    new_items.sort(key=lambda x: source_order.get(x["source"], 999))
    seen_items.sort(key=lambda x: source_order.get(x["source"], 999))
    output = new_items + seen_items

    # 5. Write scan.tsv
    with open(OUTPUT_TSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["status", "source", "title", "url", "pub_date", "title_ru"])
        for item in output:
            writer.writerow([
                item["status"],
                item["source"],
                item["title"],
                item["url"],
                item["pub_date"],
                item["title_ru"],
            ])

    # 6. Write scan.json (structured data for candidates.json generation)
    today = datetime.now().strftime("%Y-%m-%d")
    scan_json = {
        "date": today,
        "feeds_fetched": feeds_ok,
        "feeds_total": len(FEEDS),
        "total_items": total_fetched,
        "new_count": len(new_items),
        "seen_count": len(seen_items),
        "duplicates_removed": duplicates,
        "items": [
            {
                "status": item["status"],
                "source_name": item["source"],
                "headline": item["title"],
                "source_url": item["url"],
                "pub_date": item["pub_date"],
                "headline_ru": item["title_ru"],
            }
            for item in output
        ],
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(scan_json, f, indent=2, ensure_ascii=False)

    # 7. Summary
    print(f"Fetched {total_fetched} items from {feeds_ok} feeds. "
          f"{len(new_items)} new, {len(seen_items)} seen, "
          f"{duplicates} duplicates removed.")
    print(f"Output: {OUTPUT_TSV}")
    print(f"        {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
