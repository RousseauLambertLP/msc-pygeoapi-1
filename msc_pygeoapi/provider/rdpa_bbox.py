"""Native RDPA masks using shared polar-safe geographic clipping."""

from math import ceil, isfinite

from pyproj import CRS
from shapely.geometry import box, mapping
from shapely.ops import unary_union

from msc_pygeoapi.provider.bbox_utils import (
    WGS84, clip_bbox, polygon_parts,
)


def bbox_to_native_polygon(bbox, native_crs, grid_bounds):
    """Return a native GeoJSON mask, or None for no overlapping area.

    Crossing the antimeridian is supported with west > east. A full-world
    request uses west=-180 and east=180. No raster is reprojected.
    """
    if len(bbox) != 4 or not all(isfinite(v) for v in bbox):
        raise ValueError('Expected four finite bbox coordinates')
    west, south, east, north = bbox
    if not (-180 <= west <= 180 and -180 <= east <= 180
            and west != east and -90 <= south < north <= 90):
        raise ValueError('Invalid longitude/latitude bbox')
    crs = CRS.from_user_input(native_crs)
    bounds = tuple(grid_bounds)
    boxes = ([bbox] if west < east else
             [[west, south, 180, north], [-180, south, east, north]])
    # Split wide geographic sectors before projection. This prevents a
    # full-longitude ring from collapsing along the antimeridian/pole seam.
    sectors = []
    for left, bottom, right, top in boxes:
        count = max(1, ceil((right - left) / 90))
        for i in range(count):
            sectors.append([left + (right-left)*i/count, bottom,
                            left + (right-left)*(i+1)/count, top])
    parts = []
    for request in sectors:
        if request[0] == request[2]:
            continue
        shape = clip_bbox(request, WGS84, crs, bounds)
        if shape is not None:
            parts.extend(polygon_parts(shape))
    if not parts:
        return None
    return mapping(unary_union(parts).intersection(box(*bounds)))
