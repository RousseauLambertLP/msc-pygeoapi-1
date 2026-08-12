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
OGC API - Maps provider for the ECCC CMIP6 100km Zarr cubes: renders a
single (lat, lon) slice as a PNG.

Follows the msc-pygeoapi map-provider convention used by
MSCNWPGDPSMapProvider: a plain BaseProvider subclass that opens the
dataset directly, selects one 2D (lat, lon) frame with `.sel()`, and
draws it straight with matplotlib + cartopy's PlateCarree() - no
rasterio reprojection step. That's a reasonable simplification for this
data specifically since the cube is already stored on a plain
EPSG:4326/CRS84 lat/lon grid, so PlateCarree needs no reprojection to
render it; `crs` is accepted for interface compatibility but only CRS84
output is actually supported (see query() docstring).

Facet axes (scenario, percentile, season, period) and single-vs-range
subset handling follow the same exact-match logic used in
CMIP6CoverageProvider (see that module's docstring for why the upstream
pygeoapi single-value-subset behaviour needed fixing).

Example provider config:

    providers:
      - type: map
        name: msc_pygeoapi.provider.cmip6_xarray_maps.CMIP6MapsProvider
        data: /data/cmip6_zarr_store/monthly.zarr
        x_field: lon
        y_field: lat
        time_field: time
        options:
          coastlines: true
          # optional: pin facets for this collection instead of
          # requiring subset= on every request
          # fixed:
          #   scenario: ssp245

Example query:

    .../map?subset=percentile(50),scenario("ssp245")&datetime=2050-06
        &f=png&width=800&height=400&style=RdBu_r
"""

import io
import logging

import cartopy.crs as ccrs
import matplotlib
import matplotlib.pyplot as plt
import xarray as xr

from pygeoapi.provider.base import BaseProvider, ProviderInvalidDataError

matplotlib.use('Agg')

LOGGER = logging.getLogger(__name__)


class CMIP6MapsProvider(BaseProvider):
    """OGC API - Maps provider for the CMIP6 Zarr cubes"""

    def __init__(self, provider_def):
        """
        Initialize object

        :param provider_def: provider definition

        :returns: msc_pygeoapi.provider.cmip6_xarray_maps.CMIP6MapsProvider
        """
        super().__init__(provider_def)

        self.dpi = 100
        options = dict(self.options or {})
        self.squeeze = options.pop('squeeze', False)
        self.coastlines = options.pop('coastlines', False)
        # facets pinned at config time, e.g. {'scenario': 'ssp245'} -
        # same idea as CMIP6EDRProvider's options.fixed, optional here
        # since (unlike EDR) subsets already give full runtime control
        self.fixed = options.pop('fixed', {})

        open_func = xr.open_zarr if self.data.endswith('.zarr') else xr.open_dataset  # noqa
        self._data = open_func(self.data, **options) if options else open_func(self.data)  # noqa

        if self.squeeze:
            LOGGER.debug('Squeezing data')
            self._data = self._data.squeeze()

        if self.fixed:
            LOGGER.debug(f'Applying fixed facet selection: {self.fixed}')
            self._data = self._data.sel(self.fixed)

        core_dims = {d for d in
                     (self.x_field, self.y_field, self.time_field, self.z_field)  # noqa
                     if d}
        self.extra_dims = sorted(d for d in self._data.dims if d not in core_dims)  # noqa
        LOGGER.debug(f'Facet dimensions: {self.extra_dims}')

        self._fields = {}
        self.get_fields()

    def get_fields(self):
        """
        Get provider field information (names, types, units)

        Deliberately does not drop variables missing a 'units' attribute
        (these cubes' data variables don't carry CF 'units' attrs) -
        falls back to an empty string instead.

        :returns: dict of fields
        """
        if not self._fields:
            for key, value in self._data.variables.items():
                if key in self._data.coords:
                    continue
                LOGGER.debug('Adding variable')
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

    def query(self, style=None, bbox=[], width=500, height=300, crs='CRS84',
              datetime_=None, format_='png', transparent=True, subsets={},
              **kwargs):
        """
        Render a single 2D slice of the cube as a map image

        :param style: colormap name (matplotlib), e.g. 'RdBu_r'
        :param bbox: bounding box [minx,miny,maxx,maxy]
        :param width: output image width in pixels
        :param height: output image height in pixels
        :param crs: only 'CRS84'/EPSG:4326 output is supported - the
                     cube is already on that grid so no reprojection is
                     performed (see module docstring)
        :param datetime_: single timestamp (ranges are not supported for
                           a single map image)
        :param format_: only 'png' is supported
        :param subsets: dict of facet subset names with lists of values,
                         same exact-match/range semantics as
                         CMIP6CoverageProvider.query()
        :param kwargs: expects 'select_properties' with exactly one
                        variable name; defaults to the first field if
                        omitted

        :returns: bytes of PNG image
        """
        if format_.lower() != 'png':
            msg = f'Invalid format ({format_})'
            raise ProviderInvalidDataError(user_msg=msg)

        if 'select_properties' in kwargs and kwargs['select_properties']:
            select_property = kwargs['select_properties'][0]
        else:
            select_property = next(iter(self.fields))
        LOGGER.debug(f'Select property: {select_property}')

        if not bbox:
            bbox = [
                float(self._data.coords[self.x_field].values.min()),
                float(self._data.coords[self.y_field].values.min()),
                float(self._data.coords[self.x_field].values.max()),
                float(self._data.coords[self.y_field].values.max()),
            ]

        lat_vals = self._data.coords[self.y_field].values
        lon_vals = self._data.coords[self.x_field].values
        query_params = {
            self.y_field: (slice(bbox[3], bbox[1])
                            if lat_vals[0] > lat_vals[-1]
                            else slice(bbox[1], bbox[3])),
            self.x_field: (slice(bbox[2], bbox[0])
                            if lon_vals[0] > lon_vals[-1]
                            else slice(bbox[0], bbox[2])),
        }

        LOGGER.debug('Processing subsets (facet axes)')
        for key, val in subsets.items():
            if key in (self.x_field, self.y_field):
                continue
            if len(val) == 1:
                query_params[key] = val[0]
            else:
                coord_vals = self._data.coords[key].values
                lo, hi = val[0], val[1]
                query_params[key] = (slice(hi, lo)
                                      if coord_vals[0] > coord_vals[-1]
                                      else slice(lo, hi))

        LOGGER.debug('Processing time field')
        if self.time_field is not None:
            if self.time_field in subsets:
                if len(subsets[self.time_field]) != 1:
                    msg = 'Only single time allowed'
                    raise ProviderInvalidDataError(user_msg=msg)
                query_params[self.time_field] = subsets[self.time_field][0]
            elif datetime_ is not None:
                if '/' in datetime_:
                    msg = 'Date range not supported for Maps output'
                    raise ProviderInvalidDataError(user_msg=msg)
                query_params[self.time_field] = datetime_
            else:
                LOGGER.debug('Setting default time')
                query_params[self.time_field] = self._data[self.time_field].values[0]  # noqa

        LOGGER.debug(f'Query params: {query_params}')
        try:
            response = self._data.sel(query_params)[select_property]
        except Exception as err:
            msg = f'Invalid query {err}'
            raise ProviderInvalidDataError(user_msg=msg)

        LOGGER.debug('Query result')
        LOGGER.debug(response)

        # a map is a single 2D (y, x) frame - every other axis (facets,
        # time) must already be pinned to one value at this point. Note
        # partial-string time indexing (e.g. .sel(time='2050-06')) keeps
        # 'time' as a size-1 dimension rather than dropping it - that's
        # fine, just squeeze it before rendering.
        leftover = [
            d for d in response.dims
            if d not in (self.x_field, self.y_field) and response.sizes[d] > 1  # noqa
        ]
        if leftover:
            axes = ', '.join(leftover)
            msg = (f'Please subset these axes to a single value: {axes} '
                   f'(e.g. subset={leftover[0]}(<value>), or datetime= '
                   'for the time axis)')
            raise ProviderInvalidDataError(user_msg=msg)
        response = response.squeeze(drop=True)

        # imshow + origin='lower' expects row 0 = south; flip if the
        # coordinate itself runs north-to-south
        data_values = response.transpose(self.y_field, self.x_field).values
        if lat_vals[0] > lat_vals[-1]:
            data_values = data_values[::-1, :]

        fig = plt.figure(figsize=(width / self.dpi, height / self.dpi),
                          dpi=self.dpi)
        ax = plt.axes(projection=ccrs.PlateCarree())
        ax.set_position([0, 0, 1, 1])  # left, bottom, width, height (0-1)

        if self.coastlines:
            LOGGER.debug('Setting coastlines')
            ax.coastlines()

        ax.set_axis_off()
        ax.set_title(None)

        ax.imshow(
            data_values,
            origin='lower',
            extent=[bbox[0], bbox[2], bbox[1], bbox[3]],
            cmap=style,
            interpolation='nearest',
            aspect='auto'
        )

        buf = io.BytesIO()
        fig.savefig(buf, dpi=self.dpi, bbox_inches=None, pad_inches=0,
                    format='png', transparent=transparent)
        plt.close(fig)
        buf.seek(0)
        return buf.read()