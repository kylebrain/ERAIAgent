"""
Build STT artifacts from the Elden Ring lexicon:

1. lambda/speech_handler/stt_keyterms.json
   JSON array of up to KEYTERMS_CAP prioritized proper nouns, passed as
   AssemblyAI's keyterms_prompt to bias transcription toward fantasy names.

2. lambda/speech_handler/lexicon_data.json
   JSON object { term: ipa } for ALL known terms (including possessives).
   Used by ipa_corrector.py for post-transcription phonetic correction.

Reads infra/lexicons/elden_ring_words_filtered.json merged with overrides.json.

Usage:
    python scripts/build_stt_prompt.py
"""
import json
from pathlib import Path

LEXICON_DIR = Path("infra/lexicons")
FILTERED = LEXICON_DIR / "elden_ring_words_filtered.json"
OVERRIDES = LEXICON_DIR / "overrides.json"
OUT_DIR = Path("lambda/speech_handler")
KEYTERMS_PATH = OUT_DIR / "stt_keyterms.json"
LEXICON_PATH = OUT_DIR / "lexicon_data.json"

KEYTERMS_CAP = 1000  # AssemblyAI keyterms_prompt accepts up to 1000 entries.


def load_term_ipa() -> dict[str, str]:
    """Return { term: ipa } merged from filtered.json + overrides.json."""
    if not FILTERED.exists():
        raise FileNotFoundError(f"{FILTERED} not found. Run generate_pronunciations.py first.")
    out: dict[str, str] = {}
    filtered = json.loads(FILTERED.read_text(encoding="utf-8"))
    for term, entry in filtered.items():
        ipa = entry.get("ipa", "") if isinstance(entry, dict) else str(entry or "")
        if ipa:
            out[term] = ipa
    if OVERRIDES.exists():
        overrides = json.loads(OVERRIDES.read_text(encoding="utf-8"))
        for term, value in overrides.items():
            ipa = value.get("ipa", "") if isinstance(value, dict) else str(value or "")
            if ipa:
                out[term] = ipa
    return out


def score(term: str) -> tuple:
    # Lower is better. Prefer 4-12 char single-word fantasy proper nouns;
    # those are what STT models actually mis-hear. Long English compounds
    # ("Crimsonwhorl", "Greatshield") tokenize into common pieces and rarely
    # benefit from biasing, so push them to the back.
    n = len(term)
    multiword = " " in term or "-" in term
    if multiword:
        sweet_spot = 99
    elif 4 <= n <= 12:
        sweet_spot = 0
    elif n < 4:
        sweet_spot = 2  # too short — risk of false positives
    else:
        sweet_spot = 1 + (n - 12)
    return (sweet_spot, term.lower())


def pick_keyterms(term_ipa: dict[str, str], cap: int) -> list[str]:
    # Drop possessives — the model learns the base form and handles 's on its own.
    candidates = [t for t in term_ipa if not (t.endswith("'s") or t.endswith("’s"))]
    candidates.sort(key=score)
    chosen = candidates[:cap]
    chosen.sort(key=str.lower)
    return chosen


def main():
    term_ipa = load_term_ipa()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    keyterms = pick_keyterms(term_ipa, KEYTERMS_CAP)
    KEYTERMS_PATH.write_text(
        json.dumps(keyterms, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    LEXICON_PATH.write_text(
        json.dumps(term_ipa, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"Loaded {len(term_ipa)} term->IPA pairs")
    print(f"Wrote {KEYTERMS_PATH} ({len(keyterms)} keyterms)")
    print(f"Wrote {LEXICON_PATH} ({len(term_ipa)} entries)")


if __name__ == "__main__":
    main()
