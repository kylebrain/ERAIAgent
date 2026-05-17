"""
Phonetic post-correction for STT transcripts against the Elden Ring lexicon.

After STT returns a transcript, encode each word with Double Metaphone and
look up matching lexicon terms (also encoded with Double Metaphone at module
load). If a candidate's spelling is also close by Levenshtein, substitute the
canonical spelling.

This catches mistranscriptions the STT model misses despite keyterm biasing
(e.g. "raydon" -> "Radahn", "moe gwen" -> "Mohgwyn"). Words below MIN_WORD_LEN,
common English stopwords, and exact spellings already in the lexicon are
left alone. The MAX_SPELLING_RATIO guard prevents Double Metaphone collisions
between fantasy names and real English words from causing wrong substitutions
(e.g. "rotten" and "Radahn" both encode to RTN, but their spellings differ
too much for substitution).

Note: the bundled lexicon_data.json includes IPA pronunciations (generated for
the Polly TTS lexicon), but this module uses canonical spellings rather than
IPA. Comparing IPA would require a heavy G2P (gruut adds ~50MB of deps).
Double Metaphone gives effectively the same accuracy for fantasy names at a
fraction of the package size.
"""
import json
import re
from pathlib import Path

from metaphone import doublemetaphone

_LEXICON_PATH = Path(__file__).parent / "lexicon_data.json"

_MIN_WORD_LEN = 4
# Tight enough to reject "path"->"Pata" (ratio 0.5) and "build"->"Blaidd"
# (ratio 0.5) while still accepting "Radan"->"Radahn" (ratio 0.17) and
# "Bail"->"Bayle" (ratio 0.4). Loosening past 0.4 risks 4-vs-5-char false
# positives between common English words and short fantasy names.
_MAX_SPELLING_RATIO = 0.17
# Pair lookup uses the same character-ratio guard but with a lower bound on
# absolute edit distance (multi-word concatenations naturally have larger
# total length so the ratio alone is too forgiving).
_MAX_PAIR_DISTANCE = 3
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")

# Common English words that share Double Metaphone codes with fantasy names
# and would otherwise trigger false substitutions. The 0.34 spelling-ratio
# guard handles most of the long tail; this list catches the high-frequency
# cases we've seen actually collide with lexicon entries.
_STOPWORDS = frozenset({
    # function words
    "the", "and", "for", "with", "from", "that", "this", "what", "where",
    "when", "have", "will", "your", "they", "them", "their", "there", "here",
    "into", "over", "after", "before", "about", "are", "was", "were", "been",
    "but", "not", "all", "any", "some", "than", "then", "also", "just",
    "like", "much", "many", "more", "most", "very", "well", "out", "off",
    "now", "new", "old", "one", "two", "way", "still", "while", "during",
    "between", "how", "why", "who", "which", "can", "could", "would", "should",
    "must", "may", "might",
    # common content verbs
    "make", "made", "take", "took", "give", "gave", "got", "get", "want",
    "find", "look", "tell", "know", "think", "need", "say", "said", "use",
    "used", "come", "came", "go", "going", "went", "see", "saw", "seen",
    "try", "show", "let", "put", "ask", "feel", "felt", "leave", "left",
    "call", "called", "work", "play", "help", "kill", "killed", "beat",
    "fight", "fought", "build", "built", "run", "ran", "hit", "drop",
    # common nouns that have collided in tests
    "path", "way", "boss", "fire", "magic", "weapon", "armor", "shield",
    "ring", "sword", "spear", "bow", "spell", "level", "item", "quest",
    "area", "place", "thing", "stuff", "game", "tip", "guide", "wood",
    "woods", "tree", "cave", "lake", "river", "land", "field", "tower",
    "city", "village", "castle", "fort", "gate", "door", "key", "map",
    "north", "south", "east", "west", "first", "last", "next", "back",
    "good", "bad", "real", "true", "high", "low", "long", "short",
    "right", "wrong", "best", "worst",
})

_lexicon_terms: list[str] | None = None
_metaphone_index: dict[str, list[str]] | None = None


def _load() -> tuple[list[str], dict[str, list[str]]]:
    global _lexicon_terms, _metaphone_index
    if _lexicon_terms is not None and _metaphone_index is not None:
        return _lexicon_terms, _metaphone_index
    if not _LEXICON_PATH.exists():
        _lexicon_terms = []
        _metaphone_index = {}
        return _lexicon_terms, _metaphone_index
    raw = json.loads(_LEXICON_PATH.read_text(encoding="utf-8"))
    terms = list(raw.keys())
    index: dict[str, list[str]] = {}
    for term in terms:
        # Encode the term with spaces/hyphens stripped — Whisper/AssemblyAI
        # outputs "mohgwyn" as one token even though some lexicon entries
        # are multi-word ("Farum Azula" etc).
        key = re.sub(r"[^A-Za-z]", "", term)
        if not key:
            continue
        a, b = doublemetaphone(key)
        for code in (a, b):
            if code:
                index.setdefault(code, []).append(term)
    _lexicon_terms = terms
    _metaphone_index = index
    return _lexicon_terms, _metaphone_index


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + (0 if ca == cb else 1),
            ))
        prev = curr
    return prev[-1]


def _best_match(word: str, candidates: list[str]) -> tuple[str | None, float]:
    word_lower = word.lower()
    best_term: str | None = None
    best_ratio = 2.0
    for term in candidates:
        term_compact = re.sub(r"[^A-Za-z]", "", term).lower()
        dist = _levenshtein(word_lower, term_compact)
        denom = max(len(word_lower), len(term_compact)) or 1
        ratio = dist / denom
        if ratio < best_ratio:
            best_ratio = ratio
            best_term = term
    return best_term, best_ratio


def _try_substitute(word: str, index: dict[str, list[str]], max_ratio: float) -> str | None:
    """Return canonical lexicon spelling if `word` should be corrected, else None."""
    a, b = doublemetaphone(word)
    candidates: list[str] = []
    seen: set[str] = set()
    for code in (a, b):
        if code and code in index:
            for term in index[code]:
                if term not in seen:
                    seen.add(term)
                    candidates.append(term)
    if not candidates:
        return None
    best, ratio = _best_match(word, candidates)
    if best is None or ratio > max_ratio:
        return None
    # Already correct (case-insensitive, ignoring non-letters in the term).
    if re.sub(r"[^A-Za-z]", "", best).lower() == word.lower():
        return None
    return best


def correct(transcript: str) -> str:
    if not transcript:
        return transcript
    _, index = _load()
    if not index:
        return transcript

    tokens = list(_WORD_RE.finditer(transcript))
    if not tokens:
        return transcript

    # (start, end, replacement) tuples to apply right-to-left.
    edits: list[tuple[int, int, str]] = []
    i = 0
    n = len(tokens)
    while i < n:
        w1 = tokens[i].group(0)
        # Try adjacent-pair lookup first (handles "Mog win" -> "Mohgwyn").
        # Looser ratio because joining drops phoneme boundaries.
        if i + 1 < n:
            w2 = tokens[i + 1].group(0)
            if (len(w1) >= 3 and len(w2) >= 3
                    and w1.lower() not in _STOPWORDS
                    and w2.lower() not in _STOPWORDS):
                pair_sub = _try_substitute(w1 + w2, index, max_ratio=0.5)
                if pair_sub is not None:
                    edits.append((tokens[i].start(), tokens[i + 1].end(), pair_sub))
                    i += 2
                    continue
        # Single-word lookup with tight ratio.
        if len(w1) >= _MIN_WORD_LEN and w1.lower() not in _STOPWORDS:
            sub = _try_substitute(w1, index, max_ratio=_MAX_SPELLING_RATIO)
            if sub is not None:
                edits.append((tokens[i].start(), tokens[i].end(), sub))
        i += 1

    if not edits:
        return transcript
    result = transcript
    for start, end, sub in reversed(edits):
        result = result[:start] + sub + result[end:]
    return result
