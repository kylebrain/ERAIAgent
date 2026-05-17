"""
Classify terms in infra/lexicons/elden_ring_terms.txt as "hard" (needs custom
TTS pronunciation) or "easy" (standard English handled correctly by Polly).
Writes the hard subset to infra/lexicons/elden_ring_terms_filtered.txt, which
generate_pronunciations.py then reads to build IPA entries for the lexicon.

Classifications are cached per-term in infra/lexicons/elden_ring_classified.json
so re-runs after a wiki re-scrape only classify new terms.

Env:
    ANTHROPIC_API_KEY  required
    ANTHROPIC_MODEL    optional, default claude-haiku-4-5-20251001

Usage:
    python scripts/filter_hard_terms.py [--batch-size 80] [--limit N]
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

TERMS_PATH = Path("infra/lexicons/elden_ring_terms.txt")
CLASSIFIED_PATH = Path("infra/lexicons/elden_ring_classified.json")
FILTERED_PATH = Path("infra/lexicons/elden_ring_terms_filtered.txt")
API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_BATCH = 80

SYSTEM_PROMPT = """You decide whether terms from the video game Elden Ring need custom TTS pronunciation guidance.

Mark a term as "hard" if any word in it is likely to be mispronounced by a standard English text-to-speech engine. Reasons include:
- Invented or game-specific words (Glintstone, Furlcalling, Erdtree, Stonesword, Smithscript)
- Proper nouns of non-English etymology (Malenia, Miquella, Marika, Mohg, Radahn, Rykard, Rennala, Morgott, Caelid, Liurnia, Farum Azula, Nokron, Siofra, Ainsel)
- Archaic or unusual compounds (Furlcalling Finger Remedy, Stonesword Key, Bewitching Branch)
- Names with silent letters, non-obvious stress, or non-English vowel patterns

Mark a term as "easy" only if every word is a standard, commonly-pronounced English word that a TTS engine handles correctly without help. Examples of "easy":
- Tutorial / meta pages: "About Bows", "About Death", "About Multiplayer"
- Plain gear and stats: "Arrow", "Iron Spear", "Armor", "Intelligence", "Items"

When in doubt, prefer "hard" — false positives waste a lexicon slot but false negatives cause audible mispronunciation.

Respond with a JSON object: {"results": [{"term": "...", "hard": true|false}, ...]}.
Include every input term in the same order. No commentary."""


def load_classified() -> dict:
    if not CLASSIFIED_PATH.exists():
        return {}
    try:
        return json.loads(CLASSIFIED_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  [warn] {CLASSIFIED_PATH} is corrupted, starting fresh")
        return {}


def save_classified(data: dict):
    CLASSIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLASSIFIED_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


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
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError(f"No JSON object in response: {text[:200]}")
    parsed = json.loads(m.group(0))
    return parsed.get("results", [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--limit", type=int, default=0, help="Max new terms to classify (0 = all)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY env var not set", file=sys.stderr)
        sys.exit(1)
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    if not TERMS_PATH.exists():
        print(f"ERROR: {TERMS_PATH} does not exist. Run extract_wiki_terms.py first.", file=sys.stderr)
        sys.exit(1)

    all_terms = [t.strip() for t in TERMS_PATH.read_text(encoding="utf-8").splitlines() if t.strip()]
    classified = load_classified()
    todo = [t for t in all_terms if t not in classified]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(all_terms)} total terms, {len(classified)} already classified, {len(todo)} to classify")

    if todo:
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

            by_term = {r.get("term"): bool(r.get("hard", False)) for r in results if r.get("term")}
            for term in batch:
                classified[term] = by_term.get(term, False)
            save_classified(classified)

    hard_terms = [t for t in all_terms if classified.get(t)]
    FILTERED_PATH.parent.mkdir(parents=True, exist_ok=True)
    FILTERED_PATH.write_text("\n".join(hard_terms) + "\n", encoding="utf-8")
    print(
        f"\nWrote {len(hard_terms)} hard terms to {FILTERED_PATH} "
        f"(skipped {len(all_terms) - len(hard_terms)} easy)"
    )


if __name__ == "__main__":
    main()
