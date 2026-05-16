"""
Parses WitchyBND-serialized MSB XML files to extract entity spawn positions
and generates natural-language proximity documents for KB ingestion.

Prerequisites (run these manually first):
  1. UXM Selective Unpack → unpack 'map' category from game archives
     Download: https://github.com/Nordgaren/UXM-Selective-Unpack
     Game dir:  C:\\Program Files (x86)\\Steam\\steamapps\\common\\ELDEN RING\\Game
     Result:    <game_dir>\\map\\mapstudio\\*.msb.dcx

  2. WitchyBND CLI → serialize .msb.dcx files to XML
     Download: https://github.com/ividyon/WitchyBND/releases  (WitchyBND.zip)
     Run from the mapstudio folder:
       witchybnd.exe --recursive <game_dir>\\map\\mapstudio\\
     Result:    <game_dir>\\map\\mapstudio\\*.msb.dcx-witchy\\*.xml

Usage:
  python scripts/extract_msb_coordinates.py \
    --msb-dir "C:\\...\\ELDEN RING\\Game\\map\\mapstudio" \
    --output-dir ./spatial_docs \
    --radius 200
"""
import argparse
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


LOOKUPS_DIR       = Path(__file__).parent.parent / "lookups"
ENEMY_LOOKUP_XML  = LOOKUPS_DIR / "enemy_lookup.xml"
ITEM_LOOKUP_XML   = LOOKUPS_DIR / "item_lookup.xml"
MAP_LOOKUP_XML    = LOOKUPS_DIR / "map_lookup.xml"
GRACE_LOOKUP_HTML = LOOKUPS_DIR / "map_overview.html"


def _load_enemy_lookup() -> dict[str, tuple[str, str]]:
    """Return {map_id:c_id -> (location, name)} from enemy_lookup.xml."""
    lookup: dict[str, tuple[str, str]] = {}
    if not ENEMY_LOOKUP_XML.exists():
        print(f"Warning: {ENEMY_LOOKUP_XML} not found")
        return lookup
    for el in ET.parse(ENEMY_LOOKUP_XML).getroot():
        c_id   = el.get("id", "")
        map_id = el.get("map", "")
        if map_id and c_id:
            lookup[f"{map_id}:{c_id}"] = (el.get("location", ""), el.get("name", ""))
    return lookup


def _load_item_lookup() -> dict[str, str]:
    """Return {map_id:aeg_id -> name} from item_lookup.xml."""
    lookup: dict[str, str] = {}
    if not ITEM_LOOKUP_XML.exists():
        print(f"Warning: {ITEM_LOOKUP_XML} not found")
        return lookup
    for el in ET.parse(ITEM_LOOKUP_XML).getroot():
        aeg_id = el.get("id", "")
        map_id = el.get("map", "")
        if map_id and aeg_id:
            lookup[f"{map_id}:{aeg_id}"] = el.get("name", "")
    return lookup


def _load_map_lookup() -> dict[str, str]:
    """Return {map_id: name} from map_lookup.xml."""
    lookup: dict[str, str] = {}
    if not MAP_LOOKUP_XML.exists():
        print(f"Warning: {MAP_LOOKUP_XML} not found")
        return lookup
    for el in ET.parse(MAP_LOOKUP_XML).getroot():
        lookup[el.get("id", "")] = el.get("name", "")
    return lookup


def _load_grace_lookup() -> dict[str, list[str]]:
    """Return {tile_id: [grace_name, ...]} parsed from map_overview.html."""
    if not GRACE_LOOKUP_HTML.exists():
        print(f"Warning: {GRACE_LOOKUP_HTML} not found")
        return {}

    text = GRACE_LOOKUP_HTML.read_text(encoding="utf-8")
    # Each list item is: <li class="levelN [node]"><div class="li"> TEXT </div>
    item_re = re.compile(r'<li\s+class="level(\d)[^"]*"[^>]*><div\s+class="li">\s*([^<]+?)\s*</div>')

    result: dict[str, list[str]] = defaultdict(list)
    current_tile: str | None = None
    in_grace = False

    for m in item_re.finditer(text):
        level, content = int(m.group(1)), m.group(2).strip()
        if level == 1:
            tile_m = re.match(r"(m\d+_\d+_\d+_\d+)", content)
            current_tile = tile_m.group(1) if tile_m else None
            in_grace = False
        elif level == 2:
            in_grace = content.rstrip(":") == "Sites of Grace"
        elif level == 3 and in_grace and current_tile:
            result[current_tile].append(content)

    return dict(result)


# WitchyBND Part/ subdirectory name → friendly entity type (omitted types are skipped)
PART_TYPE_MAP = {
    "Enemy": "enemy",
    "Asset": "asset",
}

# Types excluded from region summaries (covered by dedicated sections or not player-relevant)
_SUMMARY_EXCLUDED = {"site_of_grace", "player_spawn"}

_PLURAL = {
    "enemy":         "Enemies",
    "asset":         "Assets",
    "site_of_grace": "Sites of Grace",
    "player_spawn":  "Player Spawns",
}


def _parse_entity_file(
    xml_path: Path,
    tile_id: str,
    entity_type: str,
    enemy_lookup: dict[str, tuple[str, str]],
    item_lookup: dict[str, str],
    map_lookup: dict[str, str],
) -> dict | None:
    """Parse a single per-entity XML file produced by WitchyBND (e.g. Part/Enemy/c0000_9026.xml).

    Entities not found in enemy_lookup or item_lookup are skipped.
    """
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as e:
        print(f"  XML parse error in {xml_path}: {e}")
        return None

    entity_key = (root.findtext("Name") or xml_path.stem).strip()
    lookup_key = f"{tile_id}:{entity_key}"

    if lookup_key in enemy_lookup:
        _, name = enemy_lookup[lookup_key]
    elif lookup_key in item_lookup:
        name = item_lookup[lookup_key]
    else:
        return None  # not in either lookup — exclude from proximity docs

    pos_el = root.find("Position")
    if pos_el is None:
        return None

    try:
        x = float(pos_el.findtext("X") or 0)
        y = float(pos_el.findtext("Y") or 0)
        z = float(pos_el.findtext("Z") or 0)
    except (TypeError, ValueError):
        return None

    if x == 0.0 and z == 0.0:
        return None  # uninitialized entry

    if name == "Site of Grace":
        entity_type = "site_of_grace"

    return {
        "name": name,
        "type": entity_type,
        "x": x, "y": y, "z": z,
        "tile": tile_id,
        "region": map_lookup.get(tile_id, tile_id),
    }


def _dist2d(a: dict, b: dict) -> float:
    return math.sqrt((a["x"] - b["x"]) ** 2 + (a["z"] - b["z"]) ** 2)


def _compass(origin: dict, target: dict) -> str:
    dx = target["x"] - origin["x"]
    dz = target["z"] - origin["z"]
    angle = math.degrees(math.atan2(dz, dx))
    dirs = ["E", "NE", "N", "NW", "W", "SW", "S", "SE"]
    return dirs[round(angle / 45) % 8]


def _cardinal_location(cx_real: float, cz_real: float, center_x: float, center_z: float) -> str:
    dx = cx_real - center_x
    dz = cz_real - center_z
    if dx == 0 and dz == 0:
        return "central"
    angle = math.degrees(math.atan2(dz, dx))
    dirs = ["east", "northeast", "north", "northwest", "west", "southwest", "south", "southeast"]
    return dirs[round(angle / 45) % 8]


def _build_proximity_docs(entities: list[dict], radius: float) -> list[tuple[dict, str]]:
    results = []
    for i, e in enumerate(entities):
        if e["type"] == "site_of_grace":
            continue
        nearby = sorted(
            [(d, o) for j, o in enumerate(entities) if i != j and (d := _dist2d(e, o)) <= radius],
            key=lambda t: t[0],
        )
        if not nearby:
            continue
        lines = [
            f"{e['type'].title()}: {e['name']}",
            f"Region: {e['region']}",
            f"Nearby within {radius:.0f} meters:",
        ]
        for dist, other in nearby:
            lines.append(f"  - {other['name']} ({other['type']}, ~{dist:.0f} meters {_compass(e, other)})")
        results.append((e, "\n".join(lines)))
    return results


def _build_region_summary_docs(
    entities: list[dict],
    grace_by_region: dict[str, list[str]],
) -> dict[str, str]:
    """Return {region_name: doc_text} with one summary document per region."""
    by_region: dict[str, list] = defaultdict(list)
    for e in entities:
        by_region[e["region"]].append(e)

    summaries: dict[str, str] = {}
    for region, members in sorted(by_region.items()):
        by_type: dict[str, list] = defaultdict(list)
        for m in members:
            by_type[m["type"]].append(m["name"])

        included = [m for m in members if m["type"] not in _SUMMARY_EXCLUDED]
        lines = [
            f"Region Summary: {region}",
            f"\nTotal entities: {len(included)}",
        ]
        for entity_type, names in sorted((t, n) for t, n in by_type.items() if t not in _SUMMARY_EXCLUDED):
            counts: dict[str, int] = defaultdict(int)
            for n in names:
                counts[n] += 1
            unique = sorted(counts)
            label = _PLURAL.get(entity_type, entity_type.replace("_", " ").title() + "s")
            lines.append(f"\n{label} ({len(names)} total, {len(unique)} unique):")
            for name in unique:
                suffix = f" (x{counts[name]})" if counts[name] > 1 else ""
                lines.append(f"  - {name}{suffix}")

        graces = grace_by_region.get(region, [])
        if graces:
            lines.append(f"\nNamed Sites of Grace ({len(graces)}):")
            for g in sorted(graces):
                lines.append(f"  - {g}")

        if not included and not graces:
            continue  # skip empty regions (e.g. colosseum arenas with no tracked entities)

        summaries[region] = "\n".join(lines)
    return summaries


def _build_cluster_docs(entities: list[dict], grid_size: float = 500) -> list[tuple[str, str]]:
    region_xs: dict[str, list] = defaultdict(list)
    region_zs: dict[str, list] = defaultdict(list)
    for e in entities:
        region_xs[e["region"]].append(e["x"])
        region_zs[e["region"]].append(e["z"])
    region_center = {
        r: ((min(region_xs[r]) + max(region_xs[r])) / 2,
            (min(region_zs[r]) + max(region_zs[r])) / 2)
        for r in region_xs
    }

    cells: dict[tuple, list] = defaultdict(list)
    for e in entities:
        cell = (round(e["x"] / grid_size), round(e["z"] / grid_size))
        cells[cell].append(e)

    results = []
    for (cx, cz), members in cells.items():
        if len(members) < 3:
            continue
        region = members[0]["region"]
        cx_real = cx * grid_size
        cz_real = cz * grid_size
        center_x, center_z = region_center[region]
        location = _cardinal_location(cx_real, cz_real, center_x, center_z)

        type_names: dict[str, list] = defaultdict(list)
        for m in members:
            type_names[m["type"]].append(m["name"])

        lines = [f"Area cluster in {region} ({location}):"]
        for t, names in sorted(type_names.items()):
            name_counts: dict[str, int] = defaultdict(int)
            for n in names:
                name_counts[n] += 1
            parts = [f"{n} (x{name_counts[n]})" if name_counts[n] > 1 else n for n in sorted(name_counts)]
            label = _PLURAL.get(t, t.replace("_", " ").title() + "s")
            lines.append(f"  - {label}: {', '.join(parts)}")

        filename = f"{_slugify(region)}_{_slugify(location)}_{cx_real:.0f}_{cz_real:.0f}.txt"
        results.append((filename, "\n".join(lines)))
    return results


def _gather_tile(
    msb_dir: Path,
    enemy_lookup: dict,
    item_lookup: dict,
    map_lookup: dict,
    cache_dir: Path,
) -> tuple[str, list[dict], bool]:
    """Returns (tile_id, entities, cache_hit)."""
    tile_id = msb_dir.name.removesuffix("-msb-dcx")
    cache_file = cache_dir / f"{tile_id}.json"

    if cache_file.exists():
        return tile_id, json.loads(cache_file.read_text(encoding="utf-8")), True

    # Pre-build the set of entity IDs that exist in lookups for this tile so we
    # can reject XML files by filename before paying the cost of ET.parse().
    prefix = f"{tile_id}:"
    plen = len(prefix)
    valid_ids = (
        {k[plen:] for k in enemy_lookup if k.startswith(prefix)} |
        {k[plen:] for k in item_lookup  if k.startswith(prefix)}
    )

    part_root = msb_dir / "Part"
    if not part_root.is_dir():
        cache_file.write_text("[]", encoding="utf-8")
        return tile_id, [], False

    tile_entities: list[dict] = []
    for type_dir in sorted(part_root.iterdir()):
        if not type_dir.is_dir() or type_dir.name not in PART_TYPE_MAP:
            continue
        entity_type = PART_TYPE_MAP[type_dir.name]
        for xml_path in sorted(type_dir.glob("*.xml")):
            if xml_path.stem not in valid_ids:
                continue  # not in any lookup — skip XML parse entirely
            entity = _parse_entity_file(xml_path, tile_id, entity_type, enemy_lookup, item_lookup, map_lookup)
            if entity:
                tile_entities.append(entity)

    cache_file.write_text(json.dumps(tile_entities), encoding="utf-8")
    return tile_id, tile_entities, False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--msb-dir", required=True,
                        help="Directory containing WitchyBND-serialized MSB XML files "
                             "(e.g. .../map/mapstudio or a folder of *.xml)")
    parser.add_argument("--output-dir", default="./spatial_docs")
    parser.add_argument("--radius", type=float, default=30,
                        help="Proximity radius in game units (default 30)")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 4,
                        help="Number of parallel workers (default: CPU count)")
    parser.add_argument("--test", nargs="?", const=5, default=None, metavar="N",
                        type=lambda v: None if v.lower() == "exclude" else int(v),
                        help="Limit to N subfolders for quick testing (default N=5); "
                             "pass 'exclude' to force a full run")
    args = parser.parse_args()

    msb_root = Path(args.msb_dir)
    out_root = Path(args.output_dir)

    # WitchyBND produces one <tile>-msb-dcx/ folder per map file, each containing Part/<Type>/*.xml
    msb_dirs = sorted(d for d in msb_root.iterdir() if d.is_dir() and d.name.endswith("-msb-dcx"))
    if not msb_dirs:
        print(f"No *-msb-dcx folders found under {msb_root}")
        print("Make sure WitchyBND has been run on the mapstudio folder.")
        return

    enemy_lookup  = _load_enemy_lookup()
    item_lookup   = _load_item_lookup()
    map_lookup    = _load_map_lookup()
    grace_lookup  = _load_grace_lookup()
    print(f"Loaded {len(enemy_lookup)} enemy, {len(item_lookup)} item, {len(map_lookup)} map, "
          f"{sum(len(v) for v in grace_lookup.values())} grace entries from lookups")

    if args.test is not None:
        msb_dirs = msb_dirs[:args.test]
        print(f"[--test] Limiting to {args.test} subfolders")
    else:
        print(f"Found {len(msb_dirs)} map folders")

    cache_dir = Path(".cache")
    cache_dir.mkdir(exist_ok=True)

    print(f"Gathering entities ({len(msb_dirs)} tiles, {args.workers} workers)...")
    tile_results: list[tuple[str, list[dict]]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_gather_tile, d, enemy_lookup, item_lookup, map_lookup, cache_dir): d
            for d in msb_dirs
        }
        for future in as_completed(futures):
            tile_id, tile_entities, cached = future.result()
            tile_results.append((tile_id, tile_entities))
            if tile_entities:
                tag = " (cached)" if cached else ""
                print(f"  {tile_id}: {len(tile_entities)} entities{tag}")

    tile_results.sort(key=lambda r: r[0])
    all_entities = [e for _, entities in tile_results for e in entities]

    if not all_entities:
        print("No entities with positions found. Check the XML structure matches expectations.")
        return

    print(f"\nTotal entities: {len(all_entities)}")

    # Group by tile so proximity is computed within each tile (O(Σ tile_n²) vs O(N²) globally)
    by_tile: dict[str, list[dict]] = defaultdict(list)
    for e in all_entities:
        by_tile[e["tile"]].append(e)

    print(f"Building proximity docs (radius={args.radius}, per-tile, {args.workers} workers)...")
    prox_dir = out_root / "proximity"
    prox_dir.mkdir(parents=True, exist_ok=True)
    prox_results: list[tuple[str, list[tuple[dict, str]]]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_build_proximity_docs, tile_entities, args.radius): tile_id
            for tile_id, tile_entities in sorted(by_tile.items())
        }
        for future in as_completed(futures):
            tile_id = futures[future]
            pairs = future.result()
            prox_results.append((tile_id, pairs))

    prox_results.sort(key=lambda r: r[0])
    all_prox_pairs = [pair for _, pairs in prox_results for pair in pairs]
    for tile_id, pairs in prox_results:
        if pairs:
            print(f"  {tile_id}: {len(pairs)} proximity docs")

    base_counts: dict[str, int] = defaultdict(int)
    for entity, _ in all_prox_pairs:
        base_counts[f"{_slugify(entity['name'])}_{_slugify(entity['region'])}"] += 1

    base_seen: dict[str, int] = defaultdict(int)
    for entity, doc in all_prox_pairs:
        base = f"{_slugify(entity['name'])}_{_slugify(entity['region'])}"
        if base_counts[base] > 1:
            filename = f"{base}_{base_seen[base]:02d}.txt"
        else:
            filename = f"{base}.txt"
        base_seen[base] += 1
        (prox_dir / filename).write_text(doc, encoding="utf-8")

    total_prox = len(all_prox_pairs)
    print(f"  {total_prox} proximity documents total")

    print("Building region summary docs...")
    grace_by_region: dict[str, list[str]] = defaultdict(list)
    for tile_id, graces in grace_lookup.items():
        region = map_lookup.get(tile_id, tile_id)
        grace_by_region[region].extend(graces)

    region_summaries = _build_region_summary_docs(all_entities, dict(grace_by_region))
    summary_dir = out_root / "region_summaries"
    summary_dir.mkdir(parents=True, exist_ok=True)
    for region, doc in region_summaries.items():
        filename = _slugify(region) + ".txt"
        (summary_dir / filename).write_text(doc, encoding="utf-8")
    print(f"  {len(region_summaries)} region summary documents")

    print("Building cluster docs...")
    cluster_docs = _build_cluster_docs(all_entities, grid_size=100)
    print(f"  {len(cluster_docs)} cluster documents")

    cluster_dir = out_root / "clusters"
    cluster_dir.mkdir(parents=True, exist_ok=True)
    for filename, doc in cluster_docs:
        (cluster_dir / filename).write_text(doc, encoding="utf-8")

    # Also dump a compact JSON coordinate index for reference
    coord_index = [{"name": e["name"], "type": e["type"], "region": e["region"],
                    "x": e["x"], "y": e["y"], "z": e["z"]} for e in all_entities]
    (out_root / "entity_coordinates.json").write_text(
        json.dumps(coord_index, indent=2), encoding="utf-8"
    )

    print(f"\nDone. Written to {out_root}/")
    print(f"  proximity/        — {total_prox} files")
    print(f"  region_summaries/ — {len(region_summaries)} files")
    print(f"  clusters/         — {len(cluster_docs)} files")
    print(f"  entity_coordinates.json — full coordinate index")
    print(f"\nUpload: aws s3 sync {out_root}/ s3://<KbDocsBucketName>/spatial/")


if __name__ == "__main__":
    main()
