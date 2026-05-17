"""
Classify single words in infra/lexicons/elden_ring_words.json as "hard" (needs
custom TTS pronunciation) or "easy" (handled correctly by Polly out of the
box). Writes the hard subset (with IPA preserved) to
infra/lexicons/elden_ring_words_filtered.json.

Classifications are cached per-word in
infra/lexicons/elden_ring_words_classified.json so re-runs only classify new
words.

Env:
    ANTHROPIC_API_KEY  required
    ANTHROPIC_MODEL    optional, default claude-haiku-4-5-20251001

Usage:
    python scripts/filter_hard_words.py [--batch-size 100] [--limit N]
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

WORDS_PATH = Path("infra/lexicons/elden_ring_words.json")
CLASSIFIED_PATH = Path("infra/lexicons/elden_ring_words_classified.json")
FILTERED_PATH = Path("infra/lexicons/elden_ring_words_filtered.json")
API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_BATCH = 100

SYSTEM_PROMPT = """You decide whether individual words from the video game Elden Ring need custom TTS pronunciation guidance.

Mark a word as "hard" if a standard English text-to-speech engine is likely to mispronounce it. Reasons include:
- Invented or game-specific words (Glintstone, Furlcalling, Erdtree, Stonesword, Smithscript, Scadutree)
- Proper nouns of non-English etymology (Malenia, Miquella, Marika, Mohg, Radahn, Rykard, Rennala, Morgott, Caelid, Liurnia, Nokron, Siofra, Ainsel)
- Possessive forms of those names (Malenia's, Rykard's)
- Archaic words, words with silent letters, non-obvious stress, or non-English vowel patterns

Mark a word as "easy" only if it is a standard, commonly-pronounced English word that a TTS engine handles correctly without help. Examples of "easy": "the", "of", "about", "arrow", "spear", "armor", "key", "river", "ashes", "bow", "altered".

When in doubt, prefer "hard" — false positives waste a lexicon slot but false negatives cause audible mispronunciation.

Respond with a JSON object: {"results": [{"word": "...", "hard": true|false}, ...]}.
Include every input word in the same order. No commentary."""


def load_classified() -> dict:
    if not CLASSIFIED_PATH.exists():
        return {}
    try:
        return json.loads(CLASSIFIED_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  [warn] {CLASSIFIED_PATH} is corrupted, starting fresh")
        return {}


def save_classified(data: dict) -> None:
    CLASSIFIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLASSIFIED_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def call_anthropic(api_key: str, model: str, words: list[str]) -> list[dict]:
    user_msg = "Words:\n" + "\n".join(f"- {w}" for w in words)
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--limit", type=int, default=0, help="Max new words to classify (0 = all)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY env var not set", file=sys.stderr)
        sys.exit(1)
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    if not WORDS_PATH.exists():
        print(
            f"ERROR: {WORDS_PATH} does not exist. Run extract_word_pronunciations.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    words_data = json.loads(WORDS_PATH.read_text(encoding="utf-8"))
    all_words = list(words_data.keys())

    classified = load_classified()
    todo = [w for w in all_words if w not in classified]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(all_words)} total words, {len(classified)} already classified, {len(todo)} to classify")

    if todo:
        for i in range(0, len(todo), args.batch_size):
            batch = todo[i : i + args.batch_size]
            print(f"  Batch {i // args.batch_size + 1}: {len(batch)} words ({i + len(batch)}/{len(todo)})")
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

            by_word = {r.get("word"): bool(r.get("hard", False)) for r in results if r.get("word")}
            for word in batch:
                classified[word] = by_word.get(word, False)
            save_classified(classified)

    hard = {
        w: words_data[w]
        for w in sorted(all_words, key=str.lower)
        if classified.get(w)
    }
    FILTERED_PATH.parent.mkdir(parents=True, exist_ok=True)
    FILTERED_PATH.write_text(
        json.dumps(hard, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"\nWrote {len(hard)} hard words to {FILTERED_PATH} "
        f"(skipped {len(all_words) - len(hard)} easy)"
    )


if __name__ == "__main__":
    main()
