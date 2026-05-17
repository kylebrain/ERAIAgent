"""
Build the Whisper STT prompt that biases transcription toward Elden Ring
proper nouns. Reads infra/lexicons/elden_ring_words_filtered.json (merged with
overrides.json), drops possessives, sorts by length descending so the
weirdest/longest names land in the prompt first, and packs as many as fit under
the Whisper 224-token cap (~750 chars budget to leave headroom).

Writes lambda/speech_handler/stt_prompt.txt, which the Lambda reads on cold
start.

Usage:
    python scripts/build_stt_prompt.py
"""
import json
from pathlib import Path

LEXICON_DIR = Path("infra/lexicons")
FILTERED = LEXICON_DIR / "elden_ring_words_filtered.json"
OVERRIDES = LEXICON_DIR / "overrides.json"
OUT_PATH = Path("lambda/speech_handler/stt_prompt.txt")

PROMPT_PREFIX = "Elden Ring proper nouns: "
CHAR_BUDGET = 750  # ~224 tokens with headroom for fantasy-name tokenization


def load_terms() -> list[str]:
    if not FILTERED.exists():
        raise FileNotFoundError(f"{FILTERED} not found. Run generate_pronunciations.py first.")
    data = json.loads(FILTERED.read_text(encoding="utf-8"))
    terms = set(data.keys())
    if OVERRIDES.exists():
        terms.update(json.loads(OVERRIDES.read_text(encoding="utf-8")).keys())
    # Drop possessives — Whisper learns the base form and handles 's on its own.
    return [t for t in terms if not (t.endswith("'s") or t.endswith("’s"))]


def score(term: str) -> tuple:
    # Lower is better. Prefer 4-12 char single-word fantasy proper nouns;
    # those are what Whisper actually mis-hears. Long English compounds
    # ("Crimsonwhorl", "Greatshield") tokenize into common pieces and
    # rarely benefit from biasing, so push them to the back.
    n = len(term)
    multiword = " " in term or "-" in term
    if multiword:
        sweet_spot = 99
    elif 4 <= n <= 12:
        sweet_spot = 0
    elif n < 4:
        sweet_spot = 2  # too short — risk of false positives
    else:
        sweet_spot = 1 + (n - 12)  # gently penalize long compounds
    return (sweet_spot, term.lower())


def pack(terms: list[str]) -> tuple[str, int]:
    # Bucket by first letter, sort each bucket by score, then round-robin so
    # the prompt covers the whole alphabet instead of dying at "D".
    buckets: dict[str, list[str]] = {}
    for t in sorted(terms, key=score):
        buckets.setdefault(t[0].lower(), []).append(t)

    chosen = []
    used = len(PROMPT_PREFIX)
    while buckets:
        for letter in sorted(buckets.keys()):
            bucket = buckets[letter]
            if not bucket:
                del buckets[letter]
                continue
            t = bucket.pop(0)
            cost = len(t) + 2  # +2 for ", " or final "."
            if used + cost > CHAR_BUDGET:
                buckets.clear()
                break
            chosen.append(t)
            used += cost
            if not bucket:
                del buckets[letter]
    chosen.sort(key=str.lower)
    return PROMPT_PREFIX + ", ".join(chosen) + ".", len(chosen)


def main():
    terms = load_terms()
    prompt, kept = pack(terms)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(prompt + "\n", encoding="utf-8")
    print(f"Loaded {len(terms)} non-possessive terms")
    print(f"Kept {kept} in prompt ({len(prompt)} chars)")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
