# Offline maps

[All guides](README.md) · Source review: 2026-09-24

VAS uses local MapLibre browser assets and a Martin tile server. On the same
server move, preserve `map-data/`, `config/martin.yaml` and the installed Martin
image. Map downloads are not part of moving networks.

| Page/control | Purpose |
|---|---|
| Pipelines → Coordinates → Save Location | Persist camera coordinates and location label |
| Intelligence → Cross-Camera Tracking → Map | Show selected identity's camera-based movement evidence |
| Security Intelligence → Map View → Load Map | Show the chosen identity's spatial evidence |
| Light / Dark / Satellite / Terrain | Select a locally available style/dataset |
| Map zoom / pan | Change the view without changing stored coordinates |
| `frontend/maps/_verify_map.html` | Internal visual map diagnostic, not an operator navigation page |

The production stack mounts `map-data` read-only into Martin and the API. The
API's metadata subdirectory is writable for verification records. Nginx proxies
`/maps/` to Martin; map content must be available before its dependent nginx
startup can succeed.

For an offline demo, open a test identity with appearances on a located camera,
load the map and switch styles. Expect a local basemap and camera markers.
A style without installed source data may fail even if another style works.
Locations show camera positions, not GPS tracking of a person.

If blank: check the browser request failures, Martin's container logs, configured
archive paths, existing metadata permissions and camera coordinates. Keep the
installed archive names consistent with [Martin config](../config/martin.yaml).
Acquisition/verification utilities live in [scripts/map_data](../scripts/map_data);
inspect each utility's help before acquiring or replacing large datasets.

Sources: [map client](../frontend/js/identity-map.js),
[production Compose](../docker/docker-compose.prod.yml),
[nginx configuration](../nginx.prod.conf).
