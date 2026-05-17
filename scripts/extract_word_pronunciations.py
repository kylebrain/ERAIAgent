"""
Split infra/lexicons/elden_ring_generated.json into a per-word pronunciation
file at infra/lexicons/elden_ring_words.json. Each term and its IPA are
tokenized on whitespace and paired by position; duplicates are deduped (first
occurrence wins, conflicts are reported).

Terms whose word count does not match their IPA's word count are skipped.

Usage:
    python scripts/extract_word_pronunciations.py
"""
import json
from collections import defaultdict
from pathlib import Path

SRC = Path("infra/lexicons/elden_ring_generated.json")
DST = Path("infra/lexicons/elden_ring_words.json")

# Outer punctuation to strip from each whitespace-split token.
# Internal apostrophes / hyphens are preserved (e.g. "Alberich's", "Half-Wolf").
PUNCT = "(),.;:!?\"'`"


def normalize(token: str) -> str:
    return token.strip(PUNCT)


def main() -> None:
    src = json.loads(SRC.read_text(encoding="utf-8"))

    words: dict[str, str] = {}
    conflicts: dict[str, set[str]] = defaultdict(set)
    mismatched: list[str] = []

    for term, entry in src.items():
        ipa = (entry.get("ipa") if isinstance(entry, dict) else entry) or ""
        ipa = ipa.strip()
        if not ipa:
            continue

        term_tokens = [normalize(t) for t in term.split()]
        term_tokens = [t for t in term_tokens if t]
        ipa_tokens = ipa.split()

        if len(term_tokens) != len(ipa_tokens):
            mismatched.append(term)
            continue

        for word, word_ipa in zip(term_tokens, ipa_tokens):
            if word in words:
                if words[word] != word_ipa:
                    conflicts[word].add(words[word])
                    conflicts[word].add(word_ipa)
            else:
                words[word] = word_ipa

    out = {
        w: {"ipa": ipa}
        for w, ipa in sorted(words.items(), key=lambda kv: kv[0].lower())
    }
    DST.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Read   {len(src)} terms from {SRC}")
    print(f"Wrote  {len(out)} unique words to {DST}")
    print(f"Skipped {len(mismatched)} terms (term/IPA token count mismatch)")
    if mismatched[:5]:
        for t in mismatched[:5]:
            print(f"  - {t}")
        if len(mismatched) > 5:
            print(f"  ... and {len(mismatched) - 5} more")
    if conflicts:
        print(f"\n{len(conflicts)} words had conflicting pronunciations (kept first):")
        for word, ipas in sorted(conflicts.items())[:10]:
            print(f"  {word}: {' | '.join(sorted(ipas))}")
        if len(conflicts) > 10:
            print(f"  ... and {len(conflicts) - 10} more")


if __name__ == "__main__":
    main()
