#!/usr/bin/env python3
"""Is the offline map stack fit to ship? One command, one measured answer per rule.

    docker exec face_recognition_api python3 /app/scripts/map_data/production_gate.py
    docker exec face_recognition_api python3 /app/scripts/map_data/production_gate.py --json

Every line prints what was MEASURED, not what was assumed. That distinction is
the whole reason this file exists: the previous acceptance checks all passed
while the Light basemap was 145,718 copies of OpenStreetMap's "Access blocked"
image, because each of them measured structure — tile present, count matches,
checksum recorded, coverage complete — and none of them looked at what a tile
depicts or at whether anything had ever checked.

Exit code is 0 only if every rule passes. `--allow-unavailable satellite`
declares a dataset deliberately absent, which is a valid production state: an
honest UNAVAILABLE with a reason code is shippable, a silent substitution is
not.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, "/app")

from backend.core import map_content_ledger as ledger        # noqa: E402
from config import settings                                  # noqa: E402

ORPHAN_SUFFIXES = (".staged", ".previous", ".part", ".installing")


class Gate:
    def __init__(self):
        self.rows = []

    def check(self, name, ok, measured, detail=""):
        self.rows.append({"rule": name, "pass": bool(ok),
                          "measured": measured, "detail": detail})
        return ok

    @property
    def failed(self):
        return [r for r in self.rows if not r["pass"]]

    def report(self, as_json=False):
        if as_json:
            print(json.dumps({"pass": not self.failed, "rules": self.rows}, indent=1))
            return 0 if not self.failed else 1
        width = max(len(r["rule"]) for r in self.rows)
        print()
        for row in self.rows:
            mark = "PASS" if row["pass"] else "FAIL"
            print(f"  [{mark}] {row['rule']:<{width}}  {row['measured']}")
            if row["detail"]:
                print(f"         {' ' * width}  {row['detail']}")
        print()
        if self.failed:
            print(f"NOT PRODUCTION READY: {len(self.failed)} of {len(self.rows)} rules failed")
            return 1
        print(f"PRODUCTION READY: all {len(self.rows)} rules pass")
        return 0


def installed(production):
    return ledger.installed_archives(production)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--production", default=None)
    ap.add_argument("--allow-unavailable", action="append", default=[],
                    metavar="STYLE",
                    help="a style that is deliberately not installed (e.g. satellite)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    production = args.production or settings.MAP_PRODUCTION_DIR
    gate = Gate()
    archives = installed(production)
    entries = ledger.load()
    pending = ledger.load_pending()

    # ---- 1. every installed archive has an ACTIVE verdict for its own bytes
    unverified = []
    for source_id in sorted(archives):
        content_ok, code, _msg = ledger.verdict_for(source_id, entries=entries,
                                                    production_dir=production)
        if content_ok is not True:
            unverified.append(f"{source_id} ({code})")
    gate.check("every installed archive is content-verified",
               not unverified and bool(archives),
               f"{len(archives) - len(unverified)}/{len(archives)} verified",
               "; ".join(unverified))

    # ---- 2. the recorded hash still matches the bytes on disk
    #         The stat tuple is the cheap runtime check; this is the authoritative one.
    mismatched = []
    for source_id, path in sorted(archives.items()):
        entry = entries.get(source_id) or {}
        recorded = entry.get("archive_sha256")
        if not recorded:
            mismatched.append(f"{source_id} (no recorded hash)")
            continue
        actual = ledger.archive_sha256(path)
        if actual != recorded:
            mismatched.append(f"{source_id} (recorded {recorded[:12]}, actual {actual[:12]})")
    gate.check("recomputed sha256 matches the ledger", not mismatched,
               f"{len(archives) - len(mismatched)}/{len(archives)} match",
               "; ".join(mismatched))

    # ---- 3. no pending verdict is authorizing a live dataset
    live_pending = [sid for sid in pending if sid in archives
                    and entries.get(sid, {}).get("state") != ledger.STATE_ACTIVE]
    gate.check("no pending verdict authorizes a live dataset", not live_pending,
               f"{len(pending)} pending entr{'y' if len(pending) == 1 else 'ies'}",
               "; ".join(live_pending))

    # ---- 4. no half-finished install left lying around
    orphans = []
    try:
        orphans = [f for f in sorted(os.listdir(production))
                   if f.endswith(ORPHAN_SUFFIXES)]
    except OSError:
        pass
    gate.check("no orphaned staged/previous/part files", not orphans,
               f"{len(orphans)} found", ", ".join(orphans))

    # ---- 5. content: no placeholder, no degenerate archive
    #         Read from the verdicts rather than re-decoding: rule 2 has just
    #         proven the verdicts describe these exact bytes.
    poisoned = [f"{sid} ({entries[sid].get('code')})" for sid in sorted(archives)
                if entries.get(sid, {}).get("pass") is False]
    gate.check("no installed archive carries placeholder or degenerate content",
               not poisoned, f"{len(archives)} archives measured", "; ".join(poisoned))

    # ---- 6. availability is honest and never nameless
    try:
        import asyncio
        from backend.core import map_availability
        snapshot = asyncio.run(map_availability.deep_check()).public()
    except Exception as exc:                                       # noqa: BLE001
        snapshot = None
        gate.check("availability could be evaluated", False, f"{type(exc).__name__}: {exc}")

    if snapshot:
        nameless = [name for name, d in snapshot["detail"].items()
                    if not d["available"] and not d.get("reason")]
        gate.check("no style is unavailable without a reason code", not nameless,
                   f"{len(snapshot['detail'])} styles checked", ", ".join(nameless))

        bad_codes = [f"{name}={d['reason']}" for name, d in snapshot["detail"].items()
                     if d.get("reason") and d["reason"] not in ledger.REASON_CODES]
        gate.check("every reason is a registered code", not bad_codes,
                   f"{len(ledger.REASON_CODES)} codes registered", "; ".join(bad_codes))

        gate.check("styles is a flat boolean map (the picker contract)",
                   all(isinstance(v, bool) for v in snapshot["styles"].values()),
                   str(snapshot["styles"]))

        unavailable = sorted(n for n, ok in snapshot["styles"].items() if not ok)
        unexpected = [n for n in unavailable if n not in args.allow_unavailable]
        gate.check("every style is available, or declared absent", not unexpected,
                   f"unavailable: {unavailable or 'none'}",
                   f"not declared with --allow-unavailable: {unexpected}" if unexpected else
                   f"declared absent: {args.allow_unavailable}")

        # Light and Dark are one dataset with two palettes; if they ever resolve
        # to different sources, they are no longer the same map.
        light, dark = snapshot["detail"].get("light", {}), snapshot["detail"].get("dark", {})
        gate.check("Light and Dark resolve to the SAME archive",
                   light.get("source") == dark.get("source"),
                   f"light={light.get('source')} dark={dark.get('source')}")

    # ---- 7. the scraper and its output stay gone
    forbidden = {
        "scripts/download_lebanon_tiles.py": "the OSM tile scraper",
        "scripts/tiles_to_mbtiles.py": "the scraper's converter",
        "tiles": "the scraped pyramid",
        "map-data/production/lebanon-streets-raster.mbtiles": "the poisoned archive",
    }
    present = [f"{path} ({what})" for path, what in forbidden.items()
               if os.path.exists(os.path.join("/app", path))]
    gate.check("the scraper and its output are absent", not present,
               f"{len(forbidden)} paths checked", "; ".join(present))

    # ---- 8. each dataset has its own committed builder
    builders = {"lebanon-streets-vector": "build_streets_vector.sh",
                "lebanon-satellite": "build_satellite.sh",
                "lebanon-dem": "build_dem.sh"}
    missing = [f"{ds} -> {name}" for ds, name in builders.items()
               if not os.path.isfile(f"/app/scripts/map_data/{name}")]
    gate.check("every dataset has an independent builder", not missing,
               f"{len(builders) - len(missing)}/{len(builders)} present", "; ".join(missing))

    # ---- 9. no runtime dependency on the internet
    #         Style JSONs are what the browser loads; a single absolute URL in
    #         one of them is an air-gapped deployment with a blank basemap.
    external = []
    styles_dir = "/app/frontend/maps/styles"
    for name in sorted(os.listdir(styles_dir)) if os.path.isdir(styles_dir) else []:
        if not name.endswith(".json"):
            continue
        with open(os.path.join(styles_dir, name), encoding="utf-8") as handle:
            body = handle.read()
        if "http://" in body or "https://" in body:
            external.append(name)
    gate.check("no style references an external URL", not external,
               f"{len(os.listdir(styles_dir)) if os.path.isdir(styles_dir) else 0} styles scanned",
               ", ".join(external))

    return gate.report(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
