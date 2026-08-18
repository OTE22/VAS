#!/usr/bin/env python3
"""Generate light.json and dark.json for the OpenMapTiles-schema vector
street dataset (lebanon-streets-vector), from ONE layer definition.

    python scripts/map_data/build_vector_styles.py [--out frontend/maps/styles]

Why generated: Light and Dark must be the SAME layers over the SAME source
with only paint differing — that is what "dark is a proper style, not a
filter" means. Two hand-maintained JSONs drift; one generator with two
palettes cannot. The generator is committed and re-run whenever the layer
list changes; its output is committed too, so the app never depends on the
generator at runtime.

Schema contract: every `source-layer` below is an OpenMapTiles layer that
Planetiler's openmaptiles profile emits. tests/test_maplibre_stack.py
enumerates the archive's real layers (MBTiles metadata `json.vector_layers`)
and asserts each referenced source-layer exists — the style is proven against
the data, not assumed to match it.

Every URL is same-origin: tiles at /maps/lebanon-streets-vector, glyphs at
/maps/font/ (Martin renders them from the vendored Noto TTFs). No sprite:
the layers here use no icons, so there is nothing external to reference.
"""

import argparse
import json
import os

FONT = ["Noto Sans Regular", "Noto Sans Arabic Regular"]

PALETTES = {
    "light": {
        "bg": "#f2efe9", "water": "#a8cfe8", "waterway": "#a8cfe8",
        "landcover_wood": "#c8dcb4", "landcover_grass": "#d9e8c8", "landcover_sand": "#f0e6c8",
        "landuse_residential": "#ebe6df", "landuse_industrial": "#e6e0e6", "park": "#cfe6c1",
        "building": "#dcd6ce", "building_outline": "#c9c2b8",
        "aeroway": "#e8e4dc",
        "road_minor": "#ffffff", "road_minor_case": "#d6d0c6",
        "road_secondary": "#f7f0d0", "road_secondary_case": "#d3c48a",
        "road_primary": "#fbe4a8", "road_primary_case": "#d9b96a",
        "road_motorway": "#f2b26a", "road_motorway_case": "#c98a3a",
        "road_path": "#b8b0a3", "rail": "#a8a29a",
        "boundary": "#9a8fa8", "boundary_admin4": "#b8afc4",
        "label": "#2b2b2b", "label_halo": "#ffffff", "label_minor": "#4a4a4a",
        "water_label": "#3a6f94", "road_label": "#3a3a3a", "road_label_halo": "#ffffff",
    },
    "dark": {
        "bg": "#0f1419", "water": "#0d2a3d", "waterway": "#0d2a3d",
        "landcover_wood": "#152218", "landcover_grass": "#182619", "landcover_sand": "#22201a",
        "landuse_residential": "#171c22", "landuse_industrial": "#1a1a22", "park": "#14251a",
        "building": "#1e242b", "building_outline": "#2a323a",
        "aeroway": "#1c2128",
        "road_minor": "#242c35", "road_minor_case": "#151a20",
        "road_secondary": "#31404b", "road_secondary_case": "#1a2229",
        "road_primary": "#3d5060", "road_primary_case": "#1e2a33",
        "road_motorway": "#5a6f80", "road_motorway_case": "#26323b",
        "road_path": "#2c3640", "rail": "#3a444e",
        "boundary": "#5c5470", "boundary_admin4": "#3e3a4c",
        "label": "#d9dee3", "label_halo": "#0f1419", "label_minor": "#a8b0b8",
        "water_label": "#6fa3c4", "road_label": "#c8ced4", "road_label_halo": "#0f1419",
    },
}


def layers(p):
    """Ordered layer list; `p` is the palette."""
    L = []
    add = L.append
    add({"id": "background", "type": "background", "paint": {"background-color": p["bg"]}})

    # --- landcover / landuse / park -------------------------------------
    add({"id": "landcover-wood", "type": "fill", "source": "streets", "source-layer": "landcover",
         "filter": ["in", ["get", "class"], ["literal", ["wood", "forest"]]],
         "paint": {"fill-color": p["landcover_wood"], "fill-opacity": 0.8}})
    add({"id": "landcover-grass", "type": "fill", "source": "streets", "source-layer": "landcover",
         "filter": ["in", ["get", "class"], ["literal", ["grass", "farmland", "wetland"]]],
         "paint": {"fill-color": p["landcover_grass"], "fill-opacity": 0.6}})
    add({"id": "landcover-sand", "type": "fill", "source": "streets", "source-layer": "landcover",
         "filter": ["==", ["get", "class"], "sand"],
         "paint": {"fill-color": p["landcover_sand"]}})
    add({"id": "landuse-residential", "type": "fill", "source": "streets", "source-layer": "landuse",
         "filter": ["in", ["get", "class"], ["literal", ["residential", "suburb", "neighbourhood"]]],
         "paint": {"fill-color": p["landuse_residential"]}})
    add({"id": "landuse-industrial", "type": "fill", "source": "streets", "source-layer": "landuse",
         "filter": ["in", ["get", "class"], ["literal", ["industrial", "commercial", "retail"]]],
         "paint": {"fill-color": p["landuse_industrial"], "fill-opacity": 0.7}})
    add({"id": "park", "type": "fill", "source": "streets", "source-layer": "park",
         "paint": {"fill-color": p["park"], "fill-opacity": 0.7}})

    # --- water ------------------------------------------------------------
    add({"id": "water", "type": "fill", "source": "streets", "source-layer": "water",
         "paint": {"fill-color": p["water"]}})
    add({"id": "waterway", "type": "line", "source": "streets", "source-layer": "waterway",
         "paint": {"line-color": p["waterway"],
                   "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.5, 14, 2.5]}})

    # --- aeroway ----------------------------------------------------------
    add({"id": "aeroway-area", "type": "fill", "source": "streets", "source-layer": "aeroway",
         "filter": ["==", ["geometry-type"], "Polygon"], "paint": {"fill-color": p["aeroway"]}})
    add({"id": "aeroway-runway", "type": "line", "source": "streets", "source-layer": "aeroway",
         "filter": ["all", ["==", ["geometry-type"], "LineString"], ["==", ["get", "class"], "runway"]],
         "paint": {"line-color": p["road_minor"], "line-width": ["interpolate", ["linear"], ["zoom"], 11, 3, 15, 16]}})

    # --- buildings --------------------------------------------------------
    add({"id": "building", "type": "fill", "source": "streets", "source-layer": "building", "minzoom": 13,
         "paint": {"fill-color": p["building"], "fill-outline-color": p["building_outline"],
                   "fill-opacity": ["interpolate", ["linear"], ["zoom"], 13, 0.4, 15, 0.9]}})

    # --- transportation (casing then fill, minor -> motorway) ------------
    def widen(widths, extra):
        """Casing stops = road stops + `extra`, as its own interpolate.

        NOT `["+", <interpolate>, 2]`: the style spec requires ["zoom"] to be
        the input of a TOP-LEVEL "step"/"interpolate", so wrapping the
        interpolate in an arithmetic expression makes MapLibre reject the layer
        — and one rejected layer fails the WHOLE style, which is how Light and
        Dark loaded zero layers while the datasets behind them were perfect.
        """
        out = []
        for i in range(0, len(widths), 2):
            out.extend([widths[i], widths[i + 1] + extra])
        return out

    def road(id_, classes, color_key, case_key, widths, minzoom=None, dash=None):
        flt = ["all", ["==", ["geometry-type"], "LineString"], ["in", ["get", "class"], ["literal", classes]]]
        w = ["interpolate", ["exponential", 1.4], ["zoom"], *widths]
        w_case = ["interpolate", ["exponential", 1.4], ["zoom"], *widen(widths, 2)]
        casing = {"id": f"{id_}-case", "type": "line", "source": "streets", "source-layer": "transportation",
                  "filter": flt, "layout": {"line-cap": "round", "line-join": "round"},
                  "paint": {"line-color": p[case_key], "line-width": w_case, "line-gap-width": 0}}
        fill = {"id": id_, "type": "line", "source": "streets", "source-layer": "transportation",
                "filter": flt, "layout": {"line-cap": "round", "line-join": "round"},
                "paint": {"line-color": p[color_key], "line-width": w}}
        if minzoom:
            casing["minzoom"] = fill["minzoom"] = minzoom
        if dash:
            fill["paint"]["line-dasharray"] = dash
            return [fill]
        return [casing, fill]

    L.extend(road("road-path", ["path", "track", "footway", "cycleway", "pedestrian"], "road_path", "road_minor_case",
                  [13, 0.6, 18, 2.5], minzoom=13, dash=[2, 2]))
    L.extend(road("road-minor", ["minor", "service", "residential", "living_street", "unclassified"], "road_minor",
                  "road_minor_case", [12, 0.5, 14, 1.5, 18, 8], minzoom=11))
    L.extend(road("road-tertiary", ["tertiary"], "road_minor", "road_minor_case", [10, 0.6, 14, 2, 18, 10]))
    L.extend(road("road-secondary", ["secondary"], "road_secondary", "road_secondary_case", [8, 0.6, 14, 2.5, 18, 12]))
    L.extend(road("road-primary", ["primary"], "road_primary", "road_primary_case", [7, 0.8, 14, 3.5, 18, 16]))
    L.extend(road("road-trunk", ["trunk"], "road_primary", "road_primary_case", [6, 0.8, 14, 4, 18, 18]))
    L.extend(road("road-motorway", ["motorway"], "road_motorway", "road_motorway_case", [6, 1, 14, 5, 18, 22]))
    add({"id": "rail", "type": "line", "source": "streets", "source-layer": "transportation",
         "filter": ["all", ["==", ["geometry-type"], "LineString"], ["==", ["get", "class"], "rail"]],
         "paint": {"line-color": p["rail"], "line-width": 1.2, "line-dasharray": [3, 3]}})

    # --- boundaries -------------------------------------------------------
    add({"id": "boundary-admin4", "type": "line", "source": "streets", "source-layer": "boundary",
         "filter": ["all", ["==", ["get", "admin_level"], 4], ["!=", ["get", "maritime"], 1]],
         "paint": {"line-color": p["boundary_admin4"], "line-width": 1, "line-dasharray": [4, 3]}})
    add({"id": "boundary-admin2", "type": "line", "source": "streets", "source-layer": "boundary",
         "filter": ["all", ["==", ["get", "admin_level"], 2], ["!=", ["get", "maritime"], 1]],
         "paint": {"line-color": p["boundary"], "line-width": ["interpolate", ["linear"], ["zoom"], 4, 1, 10, 2.5]}})

    # --- labels (English name preferred, Arabic fallback -> Arabic glyphs) -
    NAME = ["coalesce", ["get", "name:en"], ["get", "name"]]
    add({"id": "water-name", "type": "symbol", "source": "streets", "source-layer": "water_name",
         # No "text-italic": it is not a MapLibre layout property (italics come
         # from the font stack name, and no italic Noto face is vendored), so
         # MapLibre logged it as an unknown property on every style load.
         "layout": {"text-field": NAME, "text-font": FONT, "text-size": 12},
         "paint": {"text-color": p["water_label"], "text-halo-color": p["label_halo"], "text-halo-width": 1}})
    add({"id": "road-name", "type": "symbol", "source": "streets", "source-layer": "transportation_name", "minzoom": 13,
         "layout": {"text-field": NAME, "text-font": FONT, "text-size": 11, "symbol-placement": "line",
                    "text-rotation-alignment": "map"},
         "paint": {"text-color": p["road_label"], "text-halo-color": p["road_label_halo"], "text-halo-width": 1.2}})
    add({"id": "place-village", "type": "symbol", "source": "streets", "source-layer": "place",
         "filter": ["in", ["get", "class"], ["literal", ["village", "hamlet", "suburb", "neighbourhood"]]], "minzoom": 11,
         "layout": {"text-field": NAME, "text-font": FONT, "text-size": 11},
         "paint": {"text-color": p["label_minor"], "text-halo-color": p["label_halo"], "text-halo-width": 1.2}})
    add({"id": "place-town", "type": "symbol", "source": "streets", "source-layer": "place",
         "filter": ["==", ["get", "class"], "town"], "minzoom": 8,
         "layout": {"text-field": NAME, "text-font": FONT, "text-size": 13},
         "paint": {"text-color": p["label"], "text-halo-color": p["label_halo"], "text-halo-width": 1.4}})
    add({"id": "place-city", "type": "symbol", "source": "streets", "source-layer": "place",
         "filter": ["==", ["get", "class"], "city"],
         "layout": {"text-field": NAME, "text-font": FONT, "text-size": ["interpolate", ["linear"], ["zoom"], 6, 12, 12, 18]},
         "paint": {"text-color": p["label"], "text-halo-color": p["label_halo"], "text-halo-width": 1.6}})
    add({"id": "place-country", "type": "symbol", "source": "streets", "source-layer": "place",
         "filter": ["==", ["get", "class"], "country"], "maxzoom": 8,
         "layout": {"text-field": NAME, "text-font": FONT, "text-size": 16, "text-transform": "uppercase",
                    "text-letter-spacing": 0.15},
         "paint": {"text-color": p["label"], "text-halo-color": p["label_halo"], "text-halo-width": 1.6}})
    return L


def style(name, p):
    return {
        "version": 8,
        "name": f"Lebanon {name.title()}",
        "metadata": {
            "armyeye:style": name,
            "armyeye:dataset": "lebanon-streets-vector",
            "armyeye:schema": "openmaptiles (Planetiler openmaptiles profile)",
            "armyeye:generated_by": "scripts/map_data/build_vector_styles.py — do not hand-edit; edit the generator",
            "armyeye:note": ("Light and Dark are the SAME layers over the SAME vector source; only paint "
                             "differs. Not a filter. Glyphs served by Martin from local Noto TTFs. "
                             "No sprite: no icon layers."),
            "armyeye:attribution": "© OpenStreetMap contributors (ODbL)",
        },
        "glyphs": "/maps/font/{fontstack}/{range}",
        "sources": {
            "streets": {
                "type": "vector",
                "tiles": ["/maps/lebanon-streets-vector/{z}/{x}/{y}"],
                "minzoom": 0, "maxzoom": 14,
                "bounds": [34.80, 32.84, 36.92, 34.89],
                "attribution": "© OpenStreetMap contributors",
            }
        },
        "layers": layers(p),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="frontend/maps/styles")
    a = ap.parse_args()
    for name, p in PALETTES.items():
        path = os.path.join(a.out, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(style(name, p), fh, indent=1)
            fh.write("\n")
        print(f"wrote {path} ({len(layers(p))} layers)")


if __name__ == "__main__":
    main()
