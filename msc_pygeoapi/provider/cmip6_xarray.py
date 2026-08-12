# =================================================================
#
# Author: Louis-Philippe Rousseau-Lambert
#         <louis-philippe.rousseaulambert@ec.gc.ca>
#
# Copyright (c) 2026 Louis-Philippe Rousseau-Lambert
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
OGC API - Coverages provider for the ECCC CMIP6 100km Zarr cubes
(monthly.zarr / seasonal_annual.zarr / climate_normals.zarr).

Extends pygeoapi's XarrayProvider (pygeoapi.provider.xarray_) with:

* auto-discovery of the cube's "facet" dimensions - whatever is left
  over once x/y/time are identified (percentile, scenario, and,
  depending on which cube is configured, season/period) - exposed as
  genuine OGC API - Coverages subsettable axes. This works with zero
  changes to pygeoapi's routing: the Coverages API layer already allows
  subsetting on any axis name present in provider.axes
  (see pygeoapi/api/coverages.py, get_collection_coverage()).

* corrected subset-value handling. pygeoapi's reference XarrayProvider
  always builds `slice(val[0], val[1])` for every subset, regardless of
  how many values were actually supplied - a *single*-value subset
  (subset=percentile(50) / subset=scenario("ssp245")), which is exactly
  how you pin one facet to one value, comes back from
  pygeoapi.api.validate_subset() as a 1-element list and hits
  `val[1]` -> IndexError. This provider fixes that: a 1-element subset
  is treated as an exact-match selection, a 2-element subset is treated
  as a range (identical behaviour to the base class otherwise).

* lenient field/unit handling. The base class silently drops any
  variable missing a CF 'units' attribute from self.fields - these
  cubes don't carry 'units' attrs on their data variables, which would
  otherwise drop every variable. Here the variable is kept and 'units'
  falls back to an empty string.

* a guardrail before generating CoverageJSON: pygeoapi's gen_covjson
  (inherited unchanged from the base class) only understands a
  (time, y, x) result grid. If a query leaves any facet axis - e.g.
  percentile or scenario - at more than one value, this provider raises
  a clear ProviderQueryError naming the axis, rather than silently
  returning CoverageJSON with wrong shape/axisNames metadata.

Example provider config (one collection per Zarr cube):

    providers:
      - type: coverage
        name: msc_pygeoapi.provider.cmip6_xarray.CMIP6CoverageProvider
        data: /data/cmip6_zarr_store/monthly.zarr
        x_field: lon
        y_field: lat
        time_field: time

Facets are then queried the normal OGC API - Coverages way, e.g.:

    .../coverage?subset=percentile(50),scenario("ssp245")&datetime=2050-06
"""

import logging

from pygeoapi.provider.base import ProviderQueryError
from pygeoapi.provider.xarray_ import XarrayProvider

LOGGER = logging.getLogger(__name__)


def _strip_encoding(data):
    """
    Drop every variable's carried-over `.encoding` (chunks, compressor,
    filters) before a second `.to_zarr()` write.

    Needed specifically for pygeoapi's `_get_zarr_data()` (reused
    unchanged here, per pygeoapi.provider.xarray_): a Dataset opened FROM
    an existing Zarr store keeps each variable's original per-array
    encoding attached, and re-writing it as-is to a *new* Zarr store can
    hand a stale filter (e.g. a string variable's VLenUTF8 codec) to the
    wrong variable during that second write - reproduced directly here
    with `numcodecs.vlen.VLenUTF8.encode(...) -> TypeError: expected
    unicode string, found 2` on an int64 coordinate. Stripping encoding
    first makes xarray infer fresh, correct encoding from each
    variable's actual runtime dtype instead.

    :param data: xarray Dataset

    :returns: same Dataset with every variable's .encoding cleared
    """
    data = data.copy()
    for v in data.variables:
        data[v].encoding = {}
    return data


class CMIP6CoverageProvider(XarrayProvider):
    """OGC API - Coverages provider for the CMIP6 Zarr cubes"""

    def __init__(self, provider_def):
        """
        Initialize object

        :param provider_def: provider definition

        :returns: msc_pygeoapi.provider.cmip6_xarray.CMIP6CoverageProvider
        """
        super().__init__(provider_def)
        self._apply_fixed_facets()

    def get_fields(self):
        """
        Get provider field information (names, types, units)

        Overridden from the base class only to stop dropping variables
        that lack a CF 'units' attribute - see module docstring.

        :returns: dict of fields
        """
        if not self._fields:
            for key, value in self._data.variables.items():
                if key in self._data.coords:
                    continue
                dtype = value.dtype
                if dtype.name.startswith('float'):
                    dtype = 'float'
                elif dtype.name.startswith('int'):
                    dtype = 'integer'
                elif dtype.name.startswith('str'):
                    dtype = 'string'
                self._fields[key] = {
                    'type': dtype,
                    'title': value.attrs.get('long_name', key),
                    'x-ogc-unit': value.attrs.get('units', '')
                }
        return self._fields

    def _get_coverage_properties(self):
        """
        Helper function to normalize coverage properties. Extends the
        base class version to fold every leftover dimension (percentile,
        scenario, season, period, ...) into the axes list, and to record
        each facet's full set of values for discoverability.

        :returns: `dict` of coverage properties
        """
        properties = super()._get_coverage_properties()

        core_dims = {properties['x_axis_label'], properties['y_axis_label']}
        if properties.get('time_axis_label'):
            core_dims.add(properties['time_axis_label'])

        self.extra_dims = sorted(
            d for d in self._data.dims if d not in core_dims
        )
        properties['axes'].extend(self.extra_dims)
        properties['facet_values'] = {
            dim: self._data.coords[dim].values.tolist()
            for dim in self.extra_dims
        }
        LOGGER.debug(f'Facet dimensions: {self.extra_dims}')
        return properties

    def _apply_fixed_facets(self):
        """
        Optionally pin one or more facet axes to a fixed value at
        provider-configuration time, e.g.:

            options:
              fixed:
                scenario: ssp245
                percentile: 50

        This is mainly here for CMIP6EDRProvider (EDR has no generic
        mechanism for extra axes - see that module's docstring), but is
        available on the coverage provider too, e.g. to expose one
        collection per scenario.
        """
        fixed = (self.options or {}).get('fixed')
        if not fixed:
            return
        LOGGER.debug(f'Applying fixed facet selection: {fixed}')
        try:
            self._data = self._data.sel(fixed)
        except KeyError as err:
            msg = f'Invalid fixed facet in provider options: {err}'
            LOGGER.error(msg)
            raise ProviderQueryError(msg)
        self._coverage_properties = self._get_coverage_properties()
        self.axes = self._coverage_properties['axes']
        self._fields = {}
        self.get_fields()

    def _build_subset_query_params(self, subsets):
        """
        Turn a pygeoapi `subsets` dict into an xarray .sel() query dict,
        fixing the base class's single-value bug (see module docstring).

        :param subsets: dict of axis name -> list of 1 or 2 values, as
                         produced by pygeoapi.api.validate_subset()

        :returns: dict suitable for `data.sel(**query_params)`
        """
        query_params = {}
        for key, val in subsets.items():
            LOGGER.debug(f'Processing subset: {key}={val}')
            if len(val) == 1:
                query_params[key] = val[0]
                continue
            coord_vals = self._data.coords[key].values
            lo, hi = val[0], val[1]
            if coord_vals[0] > coord_vals[-1]:
                LOGGER.debug('Reversing slicing from high to low')
                query_params[key] = slice(hi, lo)
            else:
                query_params[key] = slice(lo, hi)
        return query_params

    def _validate_collapsed(self, data):
        """
        Ensure every facet axis has been narrowed to a single value
        before CoverageJSON generation - gen_covjson only understands a
        (time, y, x) grid.

        :param data: xarray Dataset already selected/sliced by query()

        :raises ProviderQueryError: if any facet axis still has size > 1
        """
        leftover = [
            d for d in self.extra_dims
            if d in data.dims and data.sizes[d] > 1
        ]
        if leftover:
            axes = ', '.join(leftover)
            msg = (f'Please subset these axes to a single value: {axes} '
                   f'(e.g. subset={leftover[0]}(<value>))')
            LOGGER.error(msg)
            raise ProviderQueryError(msg)

    def query(self, properties=[], subsets={}, bbox=[], bbox_crs=4326,
              datetime_=None, format_='json', **kwargs):
        """
        Extract data from collection

        :param properties: list of data variables to return (all if blank)
        :param subsets: dict of subset names with lists of ranges
        :param bbox: bounding box [minx,miny,maxx,maxy]
        :param bbox_crs: CRS of bounding box
        :param datetime_: temporal (datestamp or extent)
        :param format_: data format of output

        :returns: coverage data as dict of CoverageJSON or native format
        """
        from pygeoapi.provider.base import ProviderNoDataError
        from pygeoapi.provider.xarray_ import (
            _convert_float32_to_float64, _get_zarr_data
        )

        # Only 'json' (CoverageJSON, also used for HTML rendering - see
        # pygeoapi.api.coverages.get_collection_coverage, which remaps
        # F_HTML to F_COVERAGEJSON == 'json' before calling query()) and
        # our native 'zarr' passthrough are actually supported. Anything
        # else (e.g. 'jsonld', reachable via ?f=jsonld even though
        # get_collection_coverage's own response dispatch has no branch
        # for it either) must be rejected explicitly here rather than
        # falling into the "no parameters -> hand back native data"
        # fast path below: that path calls pygeoapi.util.read_data(),
        # which does `Path(path).open('rb')` - fine for pygeoapi's own
        # NetCDF-file examples, but self.data here is a Zarr *directory*,
        # so that call raises IsADirectoryError (a 500), not a clean
        # 4xx. Reject unsupported formats up front instead.
        if format_ not in ('json', 'zarr'):
            msg = (f"Unsupported format '{format_}' for this coverage - "
                   "supported formats: json, zarr")
            LOGGER.error(msg)
            raise ProviderQueryError(msg)

        if not properties and not subsets and format_ == 'zarr':
            LOGGER.debug('No parameters specified, returning native data')
            return _get_zarr_data(_strip_encoding(self._data))

        if len(properties) < 1:
            properties = self.fields.keys()

        data = self._data[[*properties]]

        query_params = self._build_subset_query_params(subsets)

        if bbox:
            if all([self._coverage_properties['x_axis_label'] in subsets,
                    self._coverage_properties['y_axis_label'] in subsets,
                    len(bbox) > 0]):
                msg = 'bbox and subsetting by coordinates are exclusive'
                LOGGER.error(msg)
                raise ProviderQueryError(msg)
            else:
                x_axis_label = self._coverage_properties['x_axis_label']
                x_coords = data.coords[x_axis_label]
                if x_coords.values[0] > x_coords.values[-1]:
                    query_params[x_axis_label] = slice(bbox[2], bbox[0])
                else:
                    query_params[x_axis_label] = slice(bbox[0], bbox[2])

                y_axis_label = self._coverage_properties['y_axis_label']
                y_coords = data.coords[y_axis_label]
                if y_coords.values[0] > y_coords.values[-1]:
                    query_params[y_axis_label] = slice(bbox[3], bbox[1])
                else:
                    query_params[y_axis_label] = slice(bbox[1], bbox[3])
            LOGGER.debug('bbox_crs is not currently handled')

        if datetime_ is not None:
            if self._coverage_properties['time_axis_label'] is None:
                msg = 'Dataset does not contain a time axis'
                LOGGER.error(msg)
                raise ProviderQueryError(msg)
            elif self._coverage_properties['time_axis_label'] in subsets:
                msg = 'datetime and temporal subsetting are exclusive'
                LOGGER.error(msg)
                raise ProviderQueryError(msg)
            else:
                if '/' in datetime_:
                    begin, end = datetime_.split('/')
                    if begin < end:
                        query_params[self.time_field] = slice(begin, end)
                    else:
                        query_params[self.time_field] = slice(end, begin)
                else:
                    query_params[self.time_field] = datetime_

        LOGGER.debug(f'Query parameters: {query_params}')
        try:
            data = data.sel(query_params)
        except Exception as err:
            LOGGER.warning(err)
            raise ProviderQueryError(err)

        if any(size == 0 for size in data.sizes.values()):
            msg = 'No data found'
            LOGGER.warning(msg)
            raise ProviderNoDataError(msg)

        if format_ == 'json':
            self._validate_collapsed(data)
            data = _convert_float32_to_float64(data)

        out_meta = {
            'bbox': [
                data.coords[self.x_field].values[0],
                data.coords[self.y_field].values[0],
                data.coords[self.x_field].values[-1],
                data.coords[self.y_field].values[-1]
            ],
            'driver': 'xarray',
            'height': data.sizes[self.y_field],
            'width': data.sizes[self.x_field],
            'variables': {
                var_name: var.attrs
                for var_name, var in data.variables.items()
            }
        }

        if self.time_field is not None and self.time_field in data.dims:
            out_meta['time'] = [
                str(data.coords[self.time_field].values[0]),
                str(data.coords[self.time_field].values[-1])
            ]
            out_meta['time_steps'] = data.sizes[self.time_field]
        else:
            out_meta['time'] = None
            out_meta['time_steps'] = 1

        LOGGER.debug('Serializing data in memory')
        if format_ == 'json':
            LOGGER.debug('Creating output in CoverageJSON')
            return self.gen_covjson(out_meta, data, properties)
        else:
            LOGGER.debug('Returning data in native zarr format')
            return _get_zarr_data(_strip_encoding(data))