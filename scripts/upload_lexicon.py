"""
Upload all infra/lexicons/eldenring*.pls files to Polly via put_lexicon.

Polly lexicon names must match [0-9A-Za-z]{1,20}, so the filenames have no
separator (eldenring.pls, eldenringb.pls, ...).

Usage:
    python scripts/upload_lexicon.py [--profile <aws-profile>]
"""
import argparse
from pathlib import Path
import boto3

LEXICON_DIR = Path("infra/lexicons")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    parser.add_argument("--region")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    polly = session.client("polly", region_name=args.region) if args.region else session.client("polly")

    pls_files = sorted(LEXICON_DIR.glob("eldenring*.pls"))
    if not pls_files:
        print(f"No PLS files in {LEXICON_DIR}. Run build_lexicon.py first.")
        return

    for path in pls_files:
        name = path.stem  # e.g. "eldenring", "eldenringb"
        content = path.read_text(encoding="utf-8")
        print(f"Uploading {path.name} as '{name}' ({len(content.encode('utf-8'))} bytes)...")
        polly.put_lexicon(Name=name, Content=content)

    print("\nLexicons currently registered in Polly:")
    listed = polly.list_lexicons().get("Lexicons", [])
    for lex in listed:
        attrs = lex.get("Attributes", {})
        print(f"  {lex['Name']}: {attrs.get('LexemesCount')} lexemes, "
              f"{attrs.get('Size')} bytes, lang={attrs.get('LanguageCode')}")


if __name__ == "__main__":
    main()
