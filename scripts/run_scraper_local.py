"""
Run the wiki scraper locally against the live S3 bucket.

Usage:
    python scripts/run_scraper_local.py --bucket elden-ring-kb-docs-<account-id> \\
        [--limit N] [--batch-size N] [--start-offset N] [--preserve] [--print-every N]

Loops through batches sequentially until the entire sitemap is processed
(or --limit pages reached). No Lambda chaining is used locally.
"""
import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lambda" / "scraper"))


def _fmt_duration(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # negative or NaN
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True, help="S3 bucket name (DOCS_BUCKET)")
    parser.add_argument("--limit", type=int, default=0, help="Max total pages (0 = all)")
    parser.add_argument("--batch-size", type=int, default=200, help="Pages per batch (default 200)")
    parser.add_argument("--start-offset", type=int, default=0, help="Resume from this URL index")
    parser.add_argument("--preserve", action="store_true", help="Skip URLs whose S3 key already exists")
    parser.add_argument("--print-every", type=int, default=10,
                        help="Per-page progress line every N pages (default 10; 0 = silent)")
    args = parser.parse_args()

    os.environ["DOCS_BUCKET"] = args.bucket

    import scraper

    offset = args.start_offset
    totals = {"saved": 0, "preserved": 0, "skipped": 0, "processed": 0}
    start = time.time()

    while True:
        event = {
            "offset": offset,
            "batch_size": args.batch_size,
            "print_every": args.print_every,
        }
        if args.limit:
            event["limit"] = args.limit
        if args.preserve:
            event["preserve"] = True

        result = scraper.handler(event, None)  # context=None -> no self-chain
        totals["saved"] += result["saved"]
        totals["preserved"] += result.get("preserved", 0)
        totals["skipped"] += result["skipped"]
        totals["processed"] += result["processed"]

        next_offset = result["offset"] + result["processed"]
        remaining = max(result["total"] - next_offset, 0)
        elapsed = time.time() - start
        pct = 100 * next_offset / result["total"] if result["total"] else 100
        rate = totals["processed"] / elapsed if elapsed > 0 else 0
        eta = remaining / rate if rate > 0 else float("nan")

        print(
            f"--- progress: {next_offset}/{result['total']} ({pct:.1f}%) | "
            f"saved={totals['saved']} preserved={totals['preserved']} skipped={totals['skipped']} | "
            f"elapsed={_fmt_duration(elapsed)} | "
            f"rate={rate * 60:.1f} pg/min | "
            f"eta={_fmt_duration(eta)} ---"
        )

        if next_offset >= result["total"] or result["processed"] == 0:
            break
        offset = next_offset

    print(f"\nFinished in {_fmt_duration(time.time() - start)}: {totals}")


if __name__ == "__main__":
    main()
