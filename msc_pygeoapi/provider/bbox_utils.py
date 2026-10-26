# =================================================================
#
# Authors: Etienne Pelletier
#          <etienne.pelletier@ec.gc.ca>
#
# Copyright (c) 2026 Etienne Pelletier
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.
#
# =================================================================

"""
Shared bounding box reprojection for providers backed by projected grids.

One rule, applied everywhere: clip the request against the grid's geographic
footprint *before* projecting it. Transforming a lon/lat bounding box straight
into a projected CRS fails in three ways on a polar grid:

1. Latitudes outside the projection's domain diverge. On a north polar
   stereographic, ``-90`` projects to infinity, and depending on the call used
   that either yields ``inf`` or raises ``CPLE_AppDefinedError``.
2. A lon/lat box projects to a curved wedge, so two transformed corners do not
   describe it. The read window that results silently drops data.
3. Corner ordering is not preserved. ``minx`` does not reliably project to the
   left edge, so an extent built by unpacking transformed corners can come out
   inverted, which xarray answers with an empty selection rather than an error.

Clipping first removes (1). Densifying and taking extrema removes (2) and (3).
"""

import logging
from functools import lru_cache
from math import ceil

import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

LOGGER = logging.getLogger(__name__)

WGS84 = CRS.from_epsg(4326)

# densification points per edge when projecting a ring
DENSIFY = 50

# longitude bins used to describe the edge of a pole-enclosing footprint
CAP_BINS = 72


@lru_cache(maxsize=64)
def get_transformer(crs_src, crs_dest):
    """
    Cached transformer between two CRSs

    :param crs_src: `pyproj.CRS` to transform from
    :param crs_dest: `pyproj.CRS` to transform to

    :returns: `pyproj.Transformer`
    """

    return Transformer.from_crs(crs_src, crs_dest, always_xy=True)


def densify(coords, n=DENSIFY):
    """
    Insert n-1 points along each segment of a coordinate ring

    :param coords: iterable of (x, y) pairs forming a closed ring
    :param n: points per segment

    :returns: `list` of (x, y) pairs
    """

    coords = list(coords)
    out = []
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        t = np.linspace(0, 1, n, endpoint=False)
        out.extend(zip(x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
    out.append(coords[-1])

    return out


def project_ring(ring, crs_src, crs_dest, n=DENSIFY, strict=False):
    """
    Project a densified ring, dropping points the projection cannot map

    :param ring: iterable of (x, y) pairs in crs_src
    :param crs_src: `pyproj.CRS` to transform from
    :param crs_dest: `pyproj.CRS` to transform to
    :param n: densification points per segment

    :returns: tuple of (x, y) `numpy.ndarray` of finite coordinates
    """

    # Geographic sampling has a bounded angular step, regardless of bbox size.
    if crs_src.is_geographic:
        ring = list(ring)
        sampled = []
        for a, b in zip(ring[:-1], ring[1:]):
            steps = max(n, ceil(max(abs(b[0] - a[0]),
                                    abs(b[1] - a[1])) / 0.1))
            sampled.extend(densify([a, b], steps)[:-1])
        sampled.append(ring[-1])
    else:
        sampled = densify(ring, n)
    xs, ys = zip(*sampled)
    x, y = get_transformer(crs_src, crs_dest).transform(
        np.array(xs), np.array(ys), errcheck=False
    )
    ok = np.isfinite(x) & np.isfinite(y)

    if strict and not ok.all():
        raise ValueError('Non-finite coordinates in projected mask boundary')
    return x[ok], y[ok]


def polar_cap(lon, lat, north=True, nbins=CAP_BINS):
    """
    Cap polygon bounded by the per-longitude extreme of a projected ring

    A grid enclosing a pole has a footprint that is a cap, not a simple
    polygon, so the projected ring cannot be used directly. Binning the ring
    by longitude and taking the extreme latitude in each bin describes the
    cap's edge as a staircase, which is far tighter than the full longitude
    band while still containing the grid.

    Each bin also takes the extreme of its immediate neighbours. That single
    bin of erosion is what keeps the result conservative: the ring is sampled,
    not continuous, so an excursion between two samples could otherwise cut
    inside the polygon and drop data the grid actually holds.

    :param lon: `numpy.ndarray` of ring longitudes
    :param lat: `numpy.ndarray` of ring latitudes
    :param north: True for a north polar cap, False for a south polar cap
    :param nbins: number of longitude bins

    :returns: `shapely.geometry.Polygon` in WGS84
    """

    edges = np.linspace(-180.0, 180.0, nbins + 1)
    binned = np.clip(np.digitize(lon, edges) - 1, 0, nbins - 1)
    extreme = np.min if north else np.max

    limits = np.array([
        extreme(lat[binned == b]) if (binned == b).any() else np.nan
        for b in range(nbins)
    ])
    limits = np.where(np.isnan(limits), extreme(lat), limits)

    # erode by one bin, wrapping at the antimeridian
    neighbours = np.vstack([np.roll(limits, k) for k in (-1, 0, 1)])
    limits = (np.min(neighbours, axis=0) if north
              else np.max(neighbours, axis=0))

    ring = []
    for i in range(nbins):
        ring.append((edges[i], limits[i]))
        ring.append((edges[i + 1], limits[i]))

    pole = 90.0 if north else -90.0

    return Polygon(ring + [(180.0, pole), (-180.0, pole)])


@lru_cache(maxsize=32)
def grid_footprint(crs, bounds, n=200):
    """
    Geographic footprint of a projected grid, as a WGS84 polygon

    Cached on (crs, bounds) rather than on a file path, so every file sharing
    a grid definition - every timestep, forecast hour and variable - resolves
    to a single entry. Keep maxsize bounded: if the hit rate ever collapses it
    means the key is varying, most likely from float noise in the bounds.

    :param crs: `pyproj.CRS` of the grid
    :param bounds: (left, bottom, right, top) in grid CRS units, hashable
    :param n: densification points per edge

    :returns: `shapely.geometry.Polygon` in WGS84
    """

    LOGGER.debug(f'Computing WGS84 footprint for {bounds}')

    left, bottom, right, top = bounds
    ring = [
        (left, bottom),
        (right, bottom),
        (right, top),
        (left, top),
        (left, bottom),
    ]
    lon, lat = project_ring(ring, crs, WGS84, n)

    if lon.size == 0:
        msg = f'Grid bounds {bounds} do not project to WGS84'
        LOGGER.error(msg)
        raise ValueError(msg)

    to_xy = get_transformer(WGS84, crs)
    for pole in (90.0, -90.0):
        px, py = to_xy.transform(0.0, pole, errcheck=False)
        if np.isfinite(px) and left <= px <= right and bottom <= py <= top:
            # the projected ring is a cap boundary, not a simple polygon
            LOGGER.debug(f'Grid encloses the {pole:+.0f} pole')
            return polar_cap(lon, lat, north=pole > 0)

    return Polygon(zip(lon, lat))


@lru_cache(maxsize=32)
def projection_domain(crs, step=2.0):
    """
    The area of the world a CRS can represent, as a WGS84 polygon

    Uses the CRS's declared area of use where one exists (any EPSG code) and
    otherwise probes a global graticule, keeping points that project finitely
    and round-trip. The probe is a fallback only: for a CRS without a declared
    area of use it reports every representable latitude, which is correct but
    not necessarily useful. Use grid_footprint for a source grid; this is for
    a requested output CRS.

    :param crs: `pyproj.CRS` of the target projection
    :param step: probe spacing in degrees when no area of use is declared

    :returns: `shapely.geometry.Polygon` in WGS84
    """

    aou = crs.area_of_use
    if aou is not None:
        LOGGER.debug(f'Using declared area of use for {crs.name}')
        return box(aou.west, aou.south, aou.east, aou.north)

    LOGGER.debug(f'Probing projection domain for {crs.name}')
    lon = np.arange(-180.0, 180.0 + step, step)
    lat = np.arange(-90.0, 90.0 + step, step)
    grid_lon, grid_lat = np.meshgrid(lon, lat)
    flat_lon, flat_lat = grid_lon.ravel(), grid_lat.ravel()

    x, y = get_transformer(WGS84, crs).transform(
        flat_lon, flat_lat, errcheck=False
    )
    ok = np.isfinite(x) & np.isfinite(y)

    # a finite result is not necessarily a meaningful one: require the
    # inverse to land back within half a probe step
    back_lon, back_lat = get_transformer(crs, WGS84).transform(
        x[ok], y[ok], errcheck=False
    )
    stable = (
        np.isfinite(back_lon)
        & np.isfinite(back_lat)
        & (np.abs(back_lat - flat_lat[ok]) < step / 2)
    )

    if not stable.any():
        msg = f'No representable domain found for {crs.name}'
        LOGGER.error(msg)
        raise ValueError(msg)

    good_lon = flat_lon[ok][stable]
    good_lat = flat_lat[ok][stable]

    return box(
        float(good_lon.min()),
        float(good_lat.min()),
        float(good_lon.max()),
        float(good_lat.max()),
    )


def clip_bbox(bbox, bbox_crs, grid_crs, grid_bounds):
    """
    Clip a request bbox to a grid's footprint and project it

    Clipping happens while the coordinates are still geographic, so latitudes
    outside the target projection's domain are removed before anything can
    diverge.

    :param bbox: (minx, miny, maxx, maxy) in bbox_crs
    :param bbox_crs: `pyproj.CRS` the bbox is expressed in
    :param grid_crs: `pyproj.CRS` of the target grid
    :param grid_bounds: (left, bottom, right, top) of the grid, hashable

    :returns: polygonal geometry in grid_crs, or None if disjoint
    """

    if bbox_crs == grid_crs:
        return box(*bbox)

    clipped = clip_bbox_geographic(bbox, bbox_crs, grid_crs, grid_bounds)

    if clipped is None:
        return None

    projected = []
    for part in polygon_parts(clipped):
        rings = []
        for ring in [part.exterior, *part.interiors]:
            x, y = project_ring(ring.coords, WGS84, grid_crs, strict=True)
            if x.size < 4:
                raise ValueError('Clipped bbox cannot be projected safely')
            rings.append(list(zip(x, y)))
        polygon = Polygon(rings[0], rings[1:])
        if not polygon.is_valid:
            raise ValueError('Invalid projected bbox polygon')
        projected.append(polygon)
    # Keep concavities and all components; intersect with actual native bounds.
    result = unary_union(projected).intersection(box(*grid_bounds))
    parts = polygon_parts(result)
    return unary_union(parts) if parts else None


def polygon_parts(geometry):
    """Return every polygon, recursively, excluding line/point contacts."""
    if geometry.is_empty:
        return []
    if geometry.geom_type == 'Polygon':
        return [geometry] if geometry.area > 0 else []
    if geometry.geom_type in ('MultiPolygon', 'GeometryCollection'):
        return [part for child in geometry.geoms
                for part in polygon_parts(child)]
    return []


def clip_bbox_geographic(bbox, bbox_crs, grid_crs, grid_bounds):
    """
    Clip a request bbox to a grid's footprint, staying in WGS84

    The geographic half of clip_bbox, kept separate because a consumer
    resampling into a geographic output grid needs the clip before it is
    projected, not after.

    :param bbox: (minx, miny, maxx, maxy) in bbox_crs
    :param bbox_crs: `pyproj.CRS` the bbox is expressed in
    :param grid_crs: `pyproj.CRS` of the source grid
    :param grid_bounds: (left, bottom, right, top) of the grid, hashable

    :returns: `shapely.geometry.Polygon` in WGS84, or None if disjoint
    """

    request = box(*bbox)

    if bbox_crs != WGS84:
        lon, lat = project_ring(request.exterior.coords, bbox_crs, WGS84)
        if lon.size == 0:
            return None
        request = Polygon(zip(lon, lat))

    clipped = request.intersection(grid_footprint(grid_crs, grid_bounds))

    if clipped.is_empty:
        LOGGER.debug(f'bbox {bbox} does not intersect the grid')
        return None

    parts = polygon_parts(clipped)
    return unary_union(parts) if parts else None


def geographic_extent(bbox, bbox_crs, grid_crs, grid_bounds):
    """
    Axis-aligned WGS84 extent of a request, clipped to the grid

    The geographic counterpart of native_extent: where that one gives the
    window to read from the source grid, this gives the lon/lat extent to
    resample that window into.

    :param bbox: (minx, miny, maxx, maxy) in bbox_crs
    :param bbox_crs: `pyproj.CRS` the bbox is expressed in
    :param grid_crs: `pyproj.CRS` of the source grid
    :param grid_bounds: (left, bottom, right, top) of the grid, hashable

    :returns: (west, south, east, north) in WGS84, or None if disjoint
    """

    clipped = clip_bbox_geographic(bbox, bbox_crs, grid_crs, grid_bounds)

    if clipped is None:
        return None

    west, south, east, north = clipped.bounds

    if west >= east or south >= north:
        return None

    return (west, south, east, north)


def native_extent(bbox, bbox_crs, grid_crs, grid_bounds):
    """
    Axis-aligned extent of a request in the grid CRS

    For consumers that need an extent rather than a shape: xarray coordinate
    slicing, and rasterio's calculate_default_transform. Ordering is
    guaranteed - left < right and bottom < top - because the values come from
    min/max over a densified ring rather than from transformed corners.

    :param bbox: (minx, miny, maxx, maxy) in bbox_crs
    :param bbox_crs: `pyproj.CRS` the bbox is expressed in
    :param grid_crs: `pyproj.CRS` of the target grid
    :param grid_bounds: (left, bottom, right, top) of the grid, hashable

    :returns: (left, bottom, right, top) in grid_crs, or None if disjoint
    """

    shape = clip_bbox(bbox, bbox_crs, grid_crs, grid_bounds)

    if shape is None:
        return None

    left, bottom, right, top = shape.bounds
    left = max(left, grid_bounds[0])
    bottom = max(bottom, grid_bounds[1])
    right = min(right, grid_bounds[2])
    top = min(top, grid_bounds[3])

    if left >= right or bottom >= top:
        return None

    return (left, bottom, right, top)


def xarray_grid_bounds(data, x_field, y_field, round_to=3):
    """
    Grid bounds from an xarray dataset's coordinate arrays

    Coordinates are cell centres, so the bounds are extended by half a cell.
    Rounding keeps float noise between files from fragmenting the cache key
    used by grid_footprint.

    :param data: `xarray.Dataset` or `xarray.DataArray`
    :param x_field: name of the x coordinate
    :param y_field: name of the y coordinate
    :param round_to: decimal places to round the bounds to

    :returns: (left, bottom, right, top) `tuple`, hashable and cache-stable
    """

    x = np.asarray(data.coords[x_field].values, dtype=float)
    y = np.asarray(data.coords[y_field].values, dtype=float)

    half_x = abs(x[1] - x[0]) / 2 if x.size > 1 else 0.0
    half_y = abs(y[1] - y[0]) / 2 if y.size > 1 else 0.0

    return (
        round(float(x.min()) - half_x, round_to),
        round(float(y.min()) - half_y, round_to),
        round(float(x.max()) + half_x, round_to),
        round(float(y.max()) + half_y, round_to),
    )


def xarray_slices(data, extent, x_field, y_field):
    """
    Coordinate slices for an extent, oriented to each axis's own direction

    xarray returns an empty selection for a slice that runs against the
    coordinate order, so the direction has to come from the coordinate array
    rather than from the order the extent was built in.

    :param data: `xarray.Dataset` or `xarray.DataArray`
    :param extent: (left, bottom, right, top) in the data's CRS
    :param x_field: name of the x coordinate
    :param y_field: name of the y coordinate

    :returns: `dict` of {field: slice} ready for .loc[]
    """

    left, bottom, right, top = extent
    slices = {}

    for field, low, high in (
        (x_field, left, right),
        (y_field, bottom, top),
    ):
        values = data.coords[field].values
        descending = values.size > 1 and values[1] < values[0]
        slices[field] = slice(high, low) if descending else slice(low, high)

    return slices
