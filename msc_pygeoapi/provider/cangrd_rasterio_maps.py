# =================================================================
#
# Authors: Louis-Philippe Rousseau-Lambert
#          <louis-philippe.rousseaulambert@ec.gc.ca>
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

from contextlib import contextmanager 
import io
import logging
import os
from parse import search

import cartopy.crs as ccrs
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.warp import (calculate_default_transform,
                           reproject,
                           Resampling,
                           transform,
                           transform_bounds)

from msc_pygeoapi.provider.cangrd_rasterio import CanGRDProvider

from pygeoapi.provider.base import ProviderQueryError

matplotlib.use('Agg')

LOGGER = logging.getLogger(__name__)

DEFAULT_CRS = 'http://www.opengis.net/def/crs/OGC/1.3/CRS84'

class CanGRDMapsProvider(CanGRDProvider):
    """CanGRD Provider"""

    def __init__(self, provider_def):
        """
        Initialize object
        :param provider_def: provider definition
        :returns: pygeoapi.provider.cangrdrasterio.CanGRDProvider
        """

        self.dpi = 100
        self.coastlines = True

        super().__init__(provider_def)

    def query(self, style=None, bbox=[], width=500, height=300, crs=DEFAULT_CRS,
              datetime_=None, format_='png', transparent=True, subsets={}, **kwargs):
        """
        Extract data from collection collection
        :param properties: variable
        :param subsets: dict of subset names with lists of ranges
        :param bbox: bounding box [minx,miny,maxx,maxy]
        :param datetime_: temporal (datestamp or extent)
        :param format_: data format of output
        :returns: coverage data as dict of CoverageJSON or native format
        """

        LOGGER.debug(f'self.data: {self.data}')

        if datetime_:
            if '/' in datetime_:
                msg = 'Date range not supported for Maps output'
                LOGGER.error(msg)
                raise ProviderQueryError(user_msg=msg)
            elif 'trend' in self.data:
                msg = 'Datetime is not supported for trend'
                LOGGER.error(msg)
                raise ProviderQueryError(user_msg=msg)
            else:
                if 'month' in self.data:
                    month = search('_{:d}-{:d}.tif', self.data)
                    period = f'{month[0]}-{month[1]:02}'
                    self.data = self.data.replace(str(month), str(datetime_))
                else:
                    period = search('_{:d}.tif', self.data)[0]
                self.data = self.data.replace(str(period), str(datetime_))

        if 'select_properties' in kwargs:
            select_property = kwargs['select_properties'][0]
        else:
            select_property = 'TMEAN'

        LOGGER.debug(f'Select property: {select_property}')

        if 'season' in subsets:
            seasonal = subsets['season']

            try:
                if len(seasonal) > 1:
                    msg = 'multiple seasons are not supported'
                    LOGGER.error(msg)
                    raise ProviderQueryError(user_msg=msg)
                elif seasonal != ['DJF']:
                    season = str(seasonal[0])
                    self.data = self.data.replace('DJF',
                                                  season)

            except Exception as err:
                LOGGER.error(err)
                raise ProviderQueryError(err)

        if not os.path.isfile(self.data):
            msg = 'No such file'
            LOGGER.error(msg)
            raise ProviderQueryError(msg)

        with rasterio.open(self.data) as _data:
            LOGGER.debug('Creating output coverage metadata')

            LOGGER.debug(f'{format_=}')
            LOGGER.debug(f'{crs=}')

            if format_.lower() == 'png':

                maps_crs = CRS.from_string(crs)
                src_crs = _data.crs

                if 'bbox-crs' in kwargs:
                    bbox_crs = CRS.from_string(kwargs['bbox-crs'])
                else:
                    bbox_crs = maps_crs

                # this is required in order to add the bounds if missing 
                plt_crs = ccrs.Projection(maps_crs)

                if not plt_crs.bounds:
                    # we need to set the msc_crs projection bounds
                    default_crs = CRS.from_string(DEFAULT_CRS)
                    def_left, def_bottom, def_right, def_top = [-180, -90, 180, 90]
                    
                    x_min, y_min, x_max, y_max = transform_bounds(default_crs,
                                                                  maps_crs,
                                                                  def_left,
                                                                  def_bottom,
                                                                  def_right,
                                                                  def_top)
                    plt_crs.bounds = (x_min, x_max, y_min, y_max)

                LOGGER.debug(f'{bbox_crs=}')
                LOGGER.debug(f'{maps_crs=}')
                LOGGER.debug(f'{src_crs=}')

                with reproject_raster(_data,
                                      maps_crs,
                                      src_crs,
                                      bbox_crs,
                                      width,
                                      height,
                                      bbox) as in_mem_ds:

                    fig = plt.figure(figsize=(width / self.dpi, height / self.dpi),
                                dpi=self.dpi)
                    ax = plt.axes(projection=plt_crs)
                    ax.set_position([0, 0, 1, 1])  # left, bottom, width, height (0–1)
                    
                    if self.coastlines:
                        LOGGER.debug('Setting coastlines')
                        ax.coastlines()

                    ax.set_axis_off()
                    ax.set_title(None)

                    left, bottom, right, top = in_mem_ds.bounds

                    LOGGER.debug(f'{in_mem_ds.bounds=}')
                    LOGGER.debug(f'{in_mem_ds.meta=}')

                    ax.imshow(
                        in_mem_ds.read(indexes=1),
                        origin='upper',
                        extent=[left, right, bottom, top],
                        cmap=style,
                        interpolation='nearest',
                        aspect='auto'
                    )

                    buf = io.BytesIO()
                    fig.savefig(buf,
                                dpi=self.dpi,
                                bbox_inches=None,
                                pad_inches=0,
                                format='png',
                                transparent=True
                                )
                    plt.close(fig)
                    buf.seek(0)
                    return buf.read()

            else:
                msg = f'Invalid format ({format_})'
                raise ProviderQueryError(user_msg=msg)


@contextmanager
def reproject_raster(_data, maps_crs, src_crs, bbox_crs, width, height, bbox):

    # update bbox for src <--> dst crs
    # Converting the input bbox (bbox_crs) into the _data.crs bbox 
    x = np.array([bbox[0], bbox[2]])
    y = np.array([bbox[1], bbox[3]])

    x_dst, y_dst = rasterio.warp.transform(bbox_crs,
                                           src_crs,
                                           x,
                                           y)

    # bbox in data (src) crs
    left, right = x_dst
    bottom, top = y_dst

    # reproject raster to requested crs and bbox (in dst crs)
    transform, t_width, t_height = calculate_default_transform(
        src_crs, maps_crs, _data.width, _data.height,
        dst_width=width, dst_height=height,
        left=left, bottom=bottom, right=right, top=top)

    out_meta = _data.meta.copy()

    out_meta.update({
        'crs': maps_crs,
        'transform': transform,
        'width': t_width,
        'height': t_height})

    with MemoryFile() as memfile:
        with memfile.open(**out_meta) as dst:
            reproject(
                source=rasterio.band(_data, 1), # single band to read from
                destination=rasterio.band(dst, 1), # single band to write to
                src_transform=_data.transform,
                src_crs=src_crs,
                dst_transform=transform,
                dst_crs=maps_crs,
                dst_nodata=np.nan,
                resampling=Resampling.nearest)
        with memfile.open() as dataset:  # Reopen as DatasetReader
            yield dataset  # Note yield not return as we're a contextmanager