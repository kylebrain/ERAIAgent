"""
Compile infra/lexicons/elden_ring_words_filtered.json + overrides.json into one or
more PLS XML files. Splits into shards (eldenring.pls, eldenringb.pls...)
when the per-lexicon limit is hit. Prints the comma-separated LEXICON_NAMES
list to set in template.yaml.

Polly limits per lexicon: 4000 lexemes, 40000 bytes content. Max 5 lexicons
referenced per SynthesizeSpeech call. Lexicon names must match
[0-9A-Za-z]{1,20} (no underscores or dashes), so the on-disk shard filenames
intentionally have no separator.

Usage:
    python scripts/build_lexicon.py
"""
import json
import string
from pathlib import Path
from xml.sax.saxutils import escape

LEXICON_DIR = Path("infra/lexicons")
GENERATED = LEXICON_DIR / "elden_ring_words_filtered.json"
OVERRIDES = LEXICON_DIR / "overrides.json"
LEXICON_BASE_NAME = "eldenring"
MAX_LEXEMES = 3900  # leave headroom under Polly's 4000 cap
MAX_BYTES = 39000   # headroom under 40000 cap


PLS_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<lexicon version="1.0"
    xmlns="http://www.w3.org/2005/01/pronunciation-lexicon"
    alphabet="ipa"
    xml:lang="en-US">
"""
PLS_FOOTER = "</lexicon>\n"


def load_pronunciations() -> dict[str, str]:
    if not GENERATED.exists():
        raise FileNotFoundError(f"{GENERATED} not found. Run generate_pronunciations.py first.")
    gen = json.loads(GENERATED.read_text(encoding="utf-8"))
    overrides = {}
    if OVERRIDES.exists():
        overrides = json.loads(OVERRIDES.read_text(encoding="utf-8"))

    merged = {}
    for term, entry in gen.items():
        ipa = (entry.get("ipa") if isinstance(entry, dict) else entry) or ""
        ipa = ipa.strip()
        if ipa:
            merged[term] = ipa
    for term, ipa in overrides.items():
        ipa = (ipa or "").strip()
        if ipa:
            merged[term] = ipa
    return merged


def lexeme_xml(term: str, ipa: str) -> str:
    return f"  <lexeme><grapheme>{escape(term)}</grapheme><phoneme>{escape(ipa)}</phoneme></lexeme>\n"


def shard_terms(items: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    shards = []
    current = []
    current_bytes = len(PLS_HEADER.encode("utf-8")) + len(PLS_FOOTER.encode("utf-8"))
    for term, ipa in items:
        line = lexeme_xml(term, ipa)
        line_bytes = len(line.encode("utf-8"))
        if len(current) >= MAX_LEXEMES or current_bytes + line_bytes > MAX_BYTES:
            shards.append(current)
            current = []
            current_bytes = len(PLS_HEADER.encode("utf-8")) + len(PLS_FOOTER.encode("utf-8"))
        current.append((term, ipa))
        current_bytes += line_bytes
    if current:
        shards.append(current)
    return shards


def write_shard(name: str, items: list[tuple[str, str]]) -> Path:
    path = LEXICON_DIR / f"{name}.pls"
    body = "".join(lexeme_xml(t, ipa) for t, ipa in items)
    path.write_text(PLS_HEADER + body + PLS_FOOTER, encoding="utf-8")
    return path


def main():
    merged = load_pronunciations()
    items = sorted(merged.items(), key=lambda kv: kv[0].lower())
    print(f"Loaded {len(items)} terms with IPA")

    shards = shard_terms(items)
    print(f"Sharded into {len(shards)} lexicon file(s)")

    # Clean up any old shard files (current naming + legacy underscored naming).
    for pattern in (f"{LEXICON_BASE_NAME}*.pls", "elden_ring*.pls"):
        for old in LEXICON_DIR.glob(pattern):
            old.unlink()

    names = []
    suffixes = string.ascii_lowercase
    for i, shard in enumerate(shards):
        suffix = suffixes[i] if len(shards) > 1 else ""
        name = f"{LEXICON_BASE_NAME}{suffix}"
        path = write_shard(name, shard)
        names.append(name)
        size = path.stat().st_size
        print(f"  {path.name}: {len(shard)} lexemes, {size} bytes")

    if len(names) > 5:
        print(f"\nWARNING: {len(names)} lexicon shards exceed Polly's 5-per-request cap.")
        print("Trim infra/lexicons/elden_ring_terms.txt or overrides.json before uploading.")

    print(f"\nSet template.yaml LEXICON_NAMES to: {','.join(names)}")


if __name__ == "__main__":
    main()
