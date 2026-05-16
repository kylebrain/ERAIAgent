"""
Fextralife Elden Ring wiki crawler.
Fetches all entity pages via sitemap and saves plain-text documents to S3.
"""
import os
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import boto3
from botocore.exceptions import ClientError
import re

WIKI_BASE = "https://eldenring.wiki.fextralife.com"
ROBOTS_URL = f"{WIKI_BASE}/robots.txt"
DOCS_BUCKET = os.environ["DOCS_BUCKET"]
S3_PREFIX = "wiki/"
DEFAULT_BATCH_SIZE = 200

# Pages that are navigation/meta, not entity content
SKIP_PATTERNS = re.compile(
    r"/(Special:|User:|Talk:|File:|Template:|Category:|Help:|Elden.Ring.Wiki$|Interactive.Map)",
    re.IGNORECASE,
)

s3 = boto3.client("s3")


def _fetch(url: str, delay: float = 1.0) -> str:
    time.sleep(delay)
    # Percent-encode any non-ASCII chars in the path (Fextralife sitemap includes
    # URLs with raw unicode like Korean/Japanese characters)
    parts = urllib.parse.urlsplit(url)
    safe_path = urllib.parse.quote(parts.path, safe="/%+")
    safe_url = urllib.parse.urlunsplit(parts._replace(path=safe_path))
    req = urllib.request.Request(safe_url, headers={"User-Agent": "EldenRingAIAgent/1.0 (personal research)"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _sitemap_urls() -> list[str]:
    """Return all page URLs from the wiki sitemap."""
    # Discover sitemap from robots.txt
    sitemap_url = f"{WIKI_BASE}/sitemap.xml"
    try:
        robots = _fetch(ROBOTS_URL, delay=0)
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemap_url = line.split(":", 1)[1].strip()
                break
    except Exception:
        pass

    xml_text = _fetch(sitemap_url, delay=1)
    root = ET.fromstring(xml_text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    # Handle sitemap index (list of sitemaps) or plain sitemap
    urls = []
    for loc in root.findall(".//sm:loc", ns):
        url = loc.text.strip()
        if url.endswith(".xml"):
            # Nested sitemap — fetch and recurse one level
            try:
                sub_xml = _fetch(url, delay=0.5)
                sub_root = ET.fromstring(sub_xml)
                for sub_loc in sub_root.findall(".//sm:loc", ns):
                    urls.append(sub_loc.text.strip())
            except Exception:
                pass
        else:
            urls.append(url)

    return [u for u in urls if not SKIP_PATTERNS.search(u)]


def _extract_text(html: str, url: str) -> str | None:
    """Extract article title + body text from a Fextralife wiki page."""
    # Lazy import — BeautifulSoup4 is bundled in the Lambda package
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    # Remove scripts, styles, nav, ads, edit buttons
    for tag in soup(["script", "style", "nav", "footer", "aside", "iframe",
                      "noscript", ".edit-link", ".toc", "#toc"]):
        tag.decompose()

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else url.split("/")[-1]

    # Fextralife wraps article body in #wiki-content-block
    content_el = (
        soup.find(id="wiki-content-block")
        or soup.find(id="wiki-content")
        or soup.find(class_="wiki-content")
        or soup.find("article")
        or soup.find("main")
    )
    if not content_el:
        return None

    body = content_el.get_text(separator="\n", strip=True)
    if len(body) < 100:
        return None

    return f"# {title}\nSource: {url}\n\n{body}"


def _slug(url: str) -> str:
    path = url.replace(WIKI_BASE, "").strip("/").replace("/", "_")
    return re.sub(r"[^\w\-]", "_", path)[:200]


def _s3_key_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=DOCS_BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def handler(event, context):
    event = event or {}
    offset = int(event.get("offset", 0))
    batch_size = int(event.get("batch_size", DEFAULT_BATCH_SIZE))
    limit = event.get("limit")  # hard cap on total URLs (testing)
    preserve = bool(event.get("preserve", False))
    print_every = int(event.get("print_every", 0))  # 0 = no per-page output

    urls = _sitemap_urls()
    print(f"Found {len(urls)} pages in sitemap")
    if limit:
        urls = urls[: int(limit)]
        print(f"Limiting to first {limit} pages")

    batch = urls[offset : offset + batch_size]
    print(f"Processing batch [{offset}:{offset + len(batch)}] of {len(urls)} (preserve={preserve})")

    saved = 0
    skipped = 0
    preserved = 0
    batch_start = time.time()
    for i, url in enumerate(batch, start=1):
        key = f"{S3_PREFIX}{_slug(url)}.txt"
        if preserve and _s3_key_exists(key):
            preserved += 1
            outcome = "preserved"
        else:
            try:
                html = _fetch(url, delay=1.0)
                text = _extract_text(html, url)
                if not text:
                    skipped += 1
                    outcome = "skipped (no content)"
                else:
                    s3.put_object(Bucket=DOCS_BUCKET, Key=key, Body=text.encode("utf-8"),
                                  ContentType="text/plain")
                    saved += 1
                    outcome = "saved"
            except urllib.error.HTTPError as e:
                print(f"HTTP {e.code} for {url}")
                skipped += 1
                outcome = f"HTTP {e.code}"
            except Exception as e:
                print(f"Error on {url}: {e}")
                skipped += 1
                outcome = f"error: {e.__class__.__name__}"

        if print_every and (i % print_every == 0 or i == len(batch)):
            elapsed = time.time() - batch_start
            rate = i / elapsed if elapsed > 0 else 0
            print(
                f"  [{offset + i}/{len(urls)}] {outcome} | "
                f"batch {i}/{len(batch)} @ {rate:.2f} pg/s"
            )

    print(f"Done: {saved} saved, {preserved} preserved, {skipped} skipped")
    return {
        "offset": offset,
        "processed": len(batch),
        "saved": saved,
        "preserved": preserved,
        "skipped": skipped,
        "total": len(urls),
    }
