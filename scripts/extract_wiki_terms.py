"""
Extract titles from all wiki pages in s3://<bucket>/wiki/ into a sorted, deduped
manifest at infra/lexicons/elden_ring_terms.txt.

Each wiki file begins with `# {title}` on line 1 (see scripts/scraper.py). We
range-read only the first ~200 bytes per object to keep this fast.

Usage:
    python scripts/extract_wiki_terms.py --bucket elden-ring-kb-docs-<account-id>
"""
import argparse
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import boto3
from botocore.config import Config

PREFIX = "wiki/"
TITLE_LINE = re.compile(r"^#\s*(.+?)\s*$", re.MULTILINE)
WIKI_SUFFIX = re.compile(r"\s*\|\s*Elden Ring Wiki\s*$", re.IGNORECASE)
DISAMBIGUATOR = re.compile(r"\s*\((Item|Boss|NPC|Location|Spirit Ash|Sorcery|Incantation|Talisman|Weapon|Armor|Shield|Spell|Ash of War|Enemy)\)\s*$", re.IGNORECASE)
MAX_WORKERS = 32
OUT_PATH = Path("infra/lexicons/elden_ring_terms.txt")


def _read_title(s3, bucket: str, key: str) -> str | None:
    try:
        obj = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-300")
    except Exception as e:
        print(f"  [error] {key}: {e}")
        return None
    head = obj["Body"].read().decode("utf-8", errors="replace")
    m = TITLE_LINE.search(head)
    if not m:
        return None
    title = WIKI_SUFFIX.sub("", m.group(1))
    title = DISAMBIGUATOR.sub("", title).strip()
    if not title or len(title) < 2:
        return None
    return title


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    s3 = session.client("s3", config=Config(max_pool_connections=MAX_WORKERS * 2))

    print(f"Listing s3://{args.bucket}/{PREFIX} ...")
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=args.bucket, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".txt"):
                keys.append(obj["Key"])
    print(f"  {len(keys)} wiki objects")

    titles = set()
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_read_title, s3, args.bucket, k) for k in keys]
        for fut in as_completed(futures):
            title = fut.result()
            if title:
                titles.add(title)
            done += 1
            if done % 200 == 0 or done == len(keys):
                print(f"  {done}/{len(keys)} read, {len(titles)} unique titles")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    sorted_titles = sorted(titles, key=lambda s: s.lower())
    OUT_PATH.write_text("\n".join(sorted_titles) + "\n", encoding="utf-8")
    print(f"\nWrote {len(sorted_titles)} terms to {OUT_PATH}")


if __name__ == "__main__":
    main()
