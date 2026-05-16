"""
Generate enemy_lookup.xml, item_lookup.xml, and map_lookup.xml from Elden Ring HTML data files.

Sources:
  - lookups/Elden Ring Entity ID List.html  -> enemy_lookup.xml
  - lookups/Elden Ring Item Analysis.html   -> item_lookup.xml
  - lookups/map_overview.html              -> map_lookup.xml

enemy_lookup: maps cXXXX_XXXX -> location + english name
item_lookup:  maps AEG asset ID -> english item name
map_lookup:   maps mXX_XX_XX_XX -> human-readable region name
"""

import re
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path

BASE_DIR    = Path(__file__).parent.parent
ENTITY_FILE = BASE_DIR / "lookups" / "Elden Ring Entity ID List.html"
ITEMS_FILE  = BASE_DIR / "lookups" / "Elden Ring Item Analysis.html"
MAP_FILE    = BASE_DIR / "lookups" / "map_overview.html"
OUT_ENEMY   = BASE_DIR / "lookups" / "enemy_lookup.xml"
OUT_ITEM    = BASE_DIR / "lookups" / "item_lookup.xml"
OUT_MAP     = BASE_DIR / "lookups" / "map_lookup.xml"

ENEMY_PAT   = re.compile(r'(m\d{2}_\d{2}_\d{2}_\d{2}) (c\d{4}_\d{4}) \(([^)]+)\) Enemy \(([^)]+)\)')
AEG_MAP_PAT = re.compile(r'asset (AEG[A-Z0-9]+_\d+_\d+) \([^)]+\) in (m\d{2}_\d{2}_\d{2}_\d{2})')
FLAG_PAT    = re.compile(r'\s*-\s*flag\s+\d+\s*$')
GOODS_PAT   = re.compile(r'\s*\(Goods \d+\)')


def parse_enemies():
    """Return dict: (map_id, c_id) -> (map_id, location, name).

    Filtering rules:
    - "Talk Dummy" entries are renamed to "Site of Grace"
    - All other entries containing "Dummy" are excluded
    """
    seen = {}
    with open(ENTITY_FILE, encoding="utf-8") as f:
        for line in f:
            for m in ENEMY_PAT.finditer(line):
                map_id, c_id, location, name = m.group(1), m.group(2), m.group(3), m.group(4)
                if name == "Talk Dummy":
                    name = "Site of Grace"
                elif "Dummy" in name:
                    continue
                if not name or not location:
                    continue
                key = (map_id, c_id)
                if key not in seen:
                    seen[key] = (map_id, location, name)
    return seen


def parse_items():
    """Return dict: (map_id, aeg_id) -> item_name."""
    seen = {}
    with open(ITEMS_FILE, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            # Skip continuation lines ([^] means same-lot continuation)
            if re.match(r'^\d+ \[\^\]', stripped):
                continue
            if "asset AEG" not in stripped:
                continue

            aeg_map_pairs = AEG_MAP_PAT.findall(stripped)
            if not aeg_map_pairs:
                continue

            # Find the closing ']' of the context section using depth tracking.
            # rfind would break for item names containing brackets like "Stone [7]".
            # The context itself also has nested [...] (e.g. "[Margit, the Fell Omen]"),
            # so we must track depth to find the outermost closing bracket.
            bracket_start = stripped.find("[")
            if bracket_start == -1:
                continue
            depth = 0
            bracket_end = -1
            for i, ch in enumerate(stripped[bracket_start:], bracket_start):
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        bracket_end = i
                        break
            if bracket_end == -1:
                continue
            name = stripped[bracket_end + 1:].strip()
            # Remove trailing "- flag XXXXX" and "(Goods XXXX)" annotations
            name = FLAG_PAT.sub("", name).strip()
            name = GOODS_PAT.sub("", name).strip()
            if not name:
                continue

            for aeg_id, map_id in aeg_map_pairs:
                key = (map_id, aeg_id)
                if key not in seen:
                    seen[key] = name

    return seen


MAP_PAT = re.compile(r'(m\d{2}_\d{2}_\d{2}_\d{2}):\s*([^<]+)')


def parse_maps():
    """Return dict: map_id -> name from map_overview.html."""
    seen = {}
    with open(MAP_FILE, encoding="utf-8") as f:
        for line in f:
            if 'class="li"' not in line:
                continue
            m = MAP_PAT.search(line)
            if m:
                map_id, name = m.group(1), m.group(2).strip()
                if name and map_id not in seen:
                    seen[map_id] = name
    return seen


def prettify(root, indent="  "):
    xml_str = ET.tostring(root, encoding="unicode")
    dom = minidom.parseString(xml_str)
    pretty = dom.toprettyxml(indent=indent, encoding=None)
    # minidom adds its own XML declaration; replace with a clean one
    lines = pretty.split("\n")
    # Remove the minidom declaration (first line) – we'll write our own
    if lines[0].startswith("<?xml"):
        lines = lines[1:]
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + "\n".join(lines)


def write_enemy_xml(enemies):
    root = ET.Element("enemies")
    for (map_id, c_id) in sorted(enemies):
        _, location, name = enemies[(map_id, c_id)]
        ET.SubElement(root, "enemy", id=c_id, map=map_id, location=location, name=name)
    OUT_ENEMY.write_text(prettify(root), encoding="utf-8")
    print(f"Wrote {len(enemies):,} enemies -> {OUT_ENEMY}")


def write_item_xml(items):
    root = ET.Element("items")
    for (map_id, aeg_id) in sorted(items):
        ET.SubElement(root, "item", id=aeg_id, map=map_id, name=items[(map_id, aeg_id)])
    OUT_ITEM.write_text(prettify(root), encoding="utf-8")
    print(f"Wrote {len(items):,} items -> {OUT_ITEM}")


def write_map_xml(maps):
    root = ET.Element("maps")
    for map_id in sorted(maps):
        ET.SubElement(root, "map", id=map_id, name=maps[map_id])
    OUT_MAP.write_text(prettify(root), encoding="utf-8")
    print(f"Wrote {len(maps):,} maps -> {OUT_MAP}")


if __name__ == "__main__":
    print("Parsing enemies…")
    enemies = parse_enemies()
    print(f"  Found {len(enemies):,} unique enemy IDs")

    print("Parsing items…")
    items = parse_items()
    print(f"  Found {len(items):,} unique AEG item IDs")

    print("Parsing maps…")
    maps = parse_maps()
    print(f"  Found {len(maps):,} unique map IDs")

    write_enemy_xml(enemies)
    write_item_xml(items)
    write_map_xml(maps)
    print("Done.")
