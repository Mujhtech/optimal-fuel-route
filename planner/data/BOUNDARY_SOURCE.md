The bundled `us_boundary.geojson` is a union of the 50 US states and District of
Columbia from the US Census Bureau's 2025 Cartographic Boundary state shapefile
at 1:500,000 scale, downloaded on 2026-10-28. Territories are excluded.

Source: https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_500k.zip

Dataset: https://www.census.gov/geographies/mapping-files/2025/geo/carto-boundary-file.html

License: public domain (US federal government work).

State geometries were unioned to remove internal state borders without additional
simplification. The source coordinate system is NAD83 (EPSG:4269); its difference
from WGS84 is smaller than the cartographic accuracy of this source.
It includes Alaska and Hawaii. This is a generalized cartographic boundary,
not a cadastral or legal border: coordinates directly on coastlines or national
boundaries can be rejected. Use a nearby inland coordinate in that situation.

`us_states_map.geojson` is a separate display-only FeatureCollection from the
same cached Census shapefile. It has 51 state/DC features, each with `name` and
`abbr` properties. Each state's geometry is simplified by 0.015 degrees with
topology preservation; coordinates are rounded to five decimal places. All
state components, including offshore islands, are retained. The file is about
401 KB uncompressed (136 KB with gzip). This generalized file provides an
offline outline basemap and is never used for location validation, station
matching, or fuel-distance calculations.
