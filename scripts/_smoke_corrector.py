"""Local smoke test for lambda/speech_handler/ipa_corrector.correct()."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lambda" / "speech_handler"))

import ipa_corrector

CASES = [
    # (input, expected_substring_or_unchanged_text)
    ("I fought Raydon at Limgrave", "Radahn"),
    ("How do I enter Mog win Palace", "Mohgwyn"),
    ("path to Farum Azula", "Farum"),
    ("the Scadu Tree fragment locations", "Scadu"),
    ("uchigatana build for samurai", "Uchigatana"),
    ("Best ooh chee gah tana build", "Uchigatana"),
    ("in the rotten woods", "rotten"),  # should NOT substitute
    ("fight against Radan", "Radahn"),
    ("Caelid is dangerous", "Caelid"),
    ("Kayleed is dangerous", "Caelid"),
    ("Where do I find Mohgwyn Palace", "Mohgwyn"),
    ("Naga keep ah katana", "Nagakiba"),
    ("Bayle the dragon fight", "Bayle"),
    ("Bail the dragon fight", "Bayle"),
    # extra false-positive guards
    ("This is the best path to take", "path"),  # don't turn into Pata
    ("Help me build a new character", "build"),  # don't turn into Blaidd
    ("Where to find a katana for samurai", "katana"),  # generic word, not a proper noun
    ("How do I beat the demi human chief", "demi"),  # generic
]

for text, expected in CASES:
    out = ipa_corrector.correct(text)
    marker = "OK " if expected.lower() in out.lower() else "?? "
    print(f"  {marker}  in:  {text!r}")
    print(f"        out: {out!r}  (looking for {expected!r})")
