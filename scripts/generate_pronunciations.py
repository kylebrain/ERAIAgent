"""
Generate IPA pronunciations for each term in
infra/lexicons/elden_ring_terms_filtered.txt (the hard-to-pronounce subset
produced by filter_hard_terms.py) via batched calls to the Anthropic API.

Reads existing infra/lexicons/elden_ring_generated.json and skips terms already
present so re-runs after a wiki re-scrape only fetch new ones.

Env:
    ANTHROPIC_API_KEY  required
    ANTHROPIC_MODEL    optional, default claude-haiku-4-5-20251001

Usage:
    python scripts/generate_pronunciations.py [--batch-size 50] [--limit N] [--terms PATH]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

TERMS_PATH = Path("infra/lexicons/elden_ring_terms_filtered.txt")
OUT_PATH = Path("infra/lexicons/elden_ring_generated.json")
API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_BATCH = 50

SYSTEM_PROMPT = """You generate IPA (International Phonetic Alphabet) pronunciations for proper nouns from the video game Elden Ring (Old/Middle English fantasy style).

For each term you are given, return its most natural English-speaker pronunciation as a single IPA string suitable for use inside a Polly Lexicon <phoneme alphabet="ipa"> element.

Guidelines:
- Use standard IPA with primary stress mark ˈ. Do NOT include slashes or brackets.
- Prefer the pronunciation a native English speaker would naturally produce.
- For multi-word terms, separate words with a single space.
- Examples: "Marika" -> "məˈriːkə", "Mohg" -> "moʊɡ", "Erdtree" -> "ˈɜːrdtriː", "Liurnia" -> "liˈɜːrniə"
- If a term contains a common English word, give the standard pronunciation.

Respond with a JSON object of the form {"results": [{"term": "...", "ipa": "..."}, ...]}.
Include every input term in the same order. Do not add commentary."""


def load_existing() -> dict:
    if not OUT_PATH.exists():
        return {}
    try:
        return json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  [warn] {OUT_PATH} is corrupted, starting fresh")
        return {}


def save(data: dict):
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def call_anthropic(api_key: str, model: str, terms: list[str]) -> list[dict]:
    user_msg = "Terms:\n" + "\n".join(f"- {t}" for t in terms)
    payload = {
        "model": model,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    text = body["content"][0]["text"]
    # The model may wrap JSON in prose or a code fence; extract the first {...} block.
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON object in response: {text[:200]}")
    parsed = json.loads(m.group(0))
    return parsed.get("results", [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--limit", type=int, default=0, help="Max new terms to process (0 = all)")
    parser.add_argument("--terms", type=Path, default=TERMS_PATH, help="Terms file to read")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY env var not set", file=sys.stderr)
        sys.exit(1)
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    if not args.terms.exists():
        print(
            f"ERROR: {args.terms} does not exist. "
            f"Run extract_wiki_terms.py and filter_hard_terms.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    all_terms = [t.strip() for t in args.terms.read_text(encoding="utf-8").splitlines() if t.strip()]
    existing = load_existing()
    todo = [t for t in all_terms if t not in existing]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(all_terms)} total terms, {len(existing)} already generated, {len(todo)} to fetch")
    if not todo:
        print("Nothing to do.")
        return

    saved = 0
    for i in range(0, len(todo), args.batch_size):
        batch = todo[i : i + args.batch_size]
        print(f"  Batch {i // args.batch_size + 1}: {len(batch)} terms ({i + len(batch)}/{len(todo)})")
        try:
            results = call_anthropic(api_key, model, batch)
        except Exception as e:
            print(f"    [error] batch failed: {e}; sleeping 5s and retrying")
            time.sleep(5)
            try:
                results = call_anthropic(api_key, model, batch)
            except Exception as e2:
                print(f"    [error] retry also failed: {e2}; skipping this batch")
                continue

        by_term = {r.get("term"): r.get("ipa", "").strip() for r in results if r.get("term")}
        for term in batch:
            ipa = by_term.get(term, "").strip()
            existing[term] = {"ipa": ipa}
        save(existing)
        saved += len(batch)
        print(f"    saved (cumulative: {saved})")

    print(f"\nDone. {OUT_PATH} now has {len(existing)} entries.")


if __name__ == "__main__":
    main()
