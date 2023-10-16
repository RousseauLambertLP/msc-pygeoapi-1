# =================================================================
#
# Authors: Louis-Philippe Rousseau-Lambert
#           <louis-philippe.rousseaulambert@ec.gc.ca>
#
# Copyright (c) 2023 Louis-Philippe Rousseau-Lambert
#
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

from datetime import datetime
import logging
from parse import search

from osgeo import gdal, osr
import rasterio
from rasterio.crs import CRS
from rasterio.io import MemoryFile
import rasterio.mask

from pygeoapi.provider.base import (BaseProvider,
                                    ProviderConnectionError,
                                    ProviderQueryError)

from msc_pygeoapi.provider.rdpa_rasterio import (RDPAProvider)

LOGGER = logging.getLogger(__name__)


# TODO: use RasterioProvider once pyproj is updated on bionic
class RDPAGDALProvider(RDPAProvider):
    """RDPA Provider"""

    def __init__(self, provider_def):
        """
        Initialize object
        :param provider_def: provider definition
        :returns: pygeoapi.provider.cangrdrasterio.CanGRDProvider
        """

        BaseProvider.__init__(self, provider_def)

        try:
            self.file_list = []


            pattern = 'APCP-{}_Sfc'
            self.var = search(pattern, self.data)[0]
            LOGGER.debug(self.var)
            RDPAProvider.get_file_list(self, self.var)

            if '*' in self.data:
                self.data = self.file_list[-1]

            self.timeformat = '%Y%m%dT%HZ'

            self._data = rasterio.open(self.data)
            self._coverage_properties = RDPAProvider._get_coverage_properties(self)
            self.axes = self._coverage_properties['axes']
            self.axes.append('time')

            self.num_bands = self._coverage_properties['num_bands']
            self.fields = [str(num) for num in range(1, self.num_bands+1)]
            self.native_format = provider_def['format']['name']

            # Needed to set the variable for each collection
            # We intialize the collection matadata through this function
            self.coverage = RDPAProvider.get_coverage_domainset(self)
        except Exception as err:
            LOGGER.warning(err)
            raise ProviderConnectionError(err)

    def query(self, properties=[1], subsets={}, bbox=[],
              datetime_=None, format_='json', **kwargs):
        """
        Extract data from collection collection
        :param properties: variable
        :param subsets: dict of subset names with lists of ranges
        :param bbox: bounding box [minx,miny,maxx,maxy]
        :param datetime_: temporal (datestamp or extent)
        :param format_: data format of output
        :returns: coverage data as dict of CoverageJSON or native format
        """

        nbits = 16

        bands = properties
        LOGGER.debug(f'Bands: {bands}, subsets: {subsets}')

        args = {
            'indexes': None
        }
        shapes = []

        if all([self._coverage_properties['x_axis_label'] in subsets,
                self._coverage_properties['y_axis_label'] in subsets,
                len(bbox) > 0]):
            msg = 'bbox and subsetting by coordinates are exclusive'
            LOGGER.warning(msg)
            raise ProviderQueryError(msg)

        if len(bbox) > 0:
            minx, miny, maxx, maxy = bbox

            crs_src = CRS.from_epsg(4326)
            crs_dest = self._data._crs

            LOGGER.debug('source bbox CRS and data CRS are different')
            LOGGER.debug('reprojecting bbox into native coordinates')

            temp_geom_min = {"type": "Point", "coordinates": [minx, miny]}
            temp_geom_max = {"type": "Point", "coordinates": [maxx, maxy]}
            temp_geom_minup = {"type": "Point", "coordinates": [minx, maxy]}
            temp_geom_maxdown = {"type": "Point", "coordinates": [maxx, miny]}

            min_coord = rasterio.warp.transform_geom(crs_src, crs_dest,
                                                     temp_geom_min)
            minx2, miny2 = min_coord['coordinates']

            max_coord = rasterio.warp.transform_geom(crs_src, crs_dest,
                                                     temp_geom_max)
            maxx2, maxy2 = max_coord['coordinates']

            upleft_coord = rasterio.warp.transform_geom(crs_src, crs_dest,
                                                        temp_geom_minup)
            minx2up, maxy2up = upleft_coord['coordinates']

            downright_coord = rasterio.warp.transform_geom(crs_src, crs_dest,
                                                           temp_geom_maxdown)
            maxx2down, miny2down = downright_coord['coordinates']

            LOGGER.debug(f'Source coordinates: {minx}, {miny}, {maxx}, {maxy}')
            LOGGER.debug(f'Destination coordinates: {minx2}, {miny2}, {maxx2}, {maxy2}')  # noqa

            shapes = [{
                'type': 'Polygon',
                'coordinates': [[
                    [minx2, miny2],
                    [minx2up, maxy2up],
                    [maxx2, maxy2],
                    [maxx2down, miny2down],
                    [minx2, miny2],
                ]]
            }]

        elif (self._coverage_properties['x_axis_label'] in subsets and
                self._coverage_properties['y_axis_label'] in subsets):
            LOGGER.debug('Creating spatial subset')

            x = self._coverage_properties['x_axis_label']
            y = self._coverage_properties['y_axis_label']

            shapes = [{
               'type': 'Polygon',
               'coordinates': [[
                   [subsets[x][0], subsets[y][0]],
                   [subsets[x][0], subsets[y][1]],
                   [subsets[x][1], subsets[y][1]],
                   [subsets[x][1], subsets[y][0]],
                   [subsets[x][0], subsets[y][0]]
               ]]
            }]

        date_file_list = False

        if datetime_:

            if '/' not in datetime_:
                try:
                    period = datetime.strptime(
                        datetime_, '%Y-%m-%dT%HZ').strftime(self.timeformat)
                    LOGGER.debug(period)
                    LOGGER.debug(self.file_list)
                    self.data = [v for v in self.file_list if period in v][0]
                except IndexError as err:
                    msg = 'Datetime value invalid or out of time domain'
                    LOGGER.error(err)
                    raise ProviderQueryError(msg)

            else:
                RDPAProvider.get_file_list(self, self.var, datetime_)
                date_file_list = self.file_list

        if bands:
            LOGGER.debug('Selecting bands')
            args['indexes'] = list(map(int, bands))

        with rasterio.open(self.data) as _data:
            LOGGER.debug('Creating output coverage metadata')
            out_meta = _data.meta

            if self.options is not None:
                LOGGER.debug('Adding dataset options')
                for key, value in self.options.items():
                    out_meta[key] = value

            if shapes:  # spatial subset
                try:
                    LOGGER.debug('Clipping data with bbox')
                    out_image, out_transform = rasterio.mask.mask(
                        _data,
                        filled=False,
                        shapes=shapes,
                        crop=True,
                        indexes=args['indexes'])
                except ValueError as err:
                    LOGGER.error(err)
                    raise ProviderQueryError(err)

                out_meta.update({'driver': self.native_format,
                                 'height': out_image.shape[1],
                                 'width': out_image.shape[2],
                                 'transform': out_transform})
            else:  # no spatial subset
                LOGGER.debug('Creating data in memory with band selection')
                out_image = _data.read(indexes=args['indexes'])

            if bbox:
                out_meta['bbox'] = [bbox[0], bbox[1], bbox[2], bbox[3]]
            elif shapes:
                out_meta['bbox'] = [
                    subsets[x][0], subsets[y][0],
                    subsets[x][1], subsets[y][1]
                ]
            else:
                out_meta['bbox'] = [
                    _data.bounds.left,
                    _data.bounds.bottom,
                    _data.bounds.right,
                    _data.bounds.top
                ]

            out_meta['units'] = _data.units

            self.filename = self.data.split('/')[-1].replace(
                '*', '')

            LOGGER.debug(f'out_meta: {out_meta}')

            # CovJSON output does not support multiple bands yet
            # Only the first timestep is returned
            if format_ == 'json':

                if date_file_list:
                    err = 'Date range not yet supported for CovJSON output'
                    LOGGER.error(err)
                    raise ProviderQueryError(err)
                else:
                    LOGGER.debug('Creating output in CoverageJSON')
                    out_meta['bands'] = [1]
                    return self.gen_covjson(out_meta, out_image)
            else:
                if date_file_list:
                    out_meta.update(count=len(date_file_list))

                    LOGGER.debug('Serializing data in memory')
                    with MemoryFile() as memfile:
                        with memfile.open(**out_meta, nbits=nbits) as dest:
                            for id, layer in enumerate(date_file_list,
                                                       start=1):
                                with rasterio.open(layer) as src1:
                                    if shapes:  # spatial subset
                                        try:
                                            LOGGER.debug('Clipping data')
                                            out_image, out_transform = \
                                                rasterio.mask.mask(
                                                    src1,
                                                    filled=False,
                                                    shapes=shapes,
                                                    crop=True,
                                                    indexes=args['indexes'])
                                        except ValueError as err:
                                            LOGGER.error(err)
                                            raise ProviderQueryError(err)
                                    else:
                                        out_image = src1.read(
                                            indexes=args['indexes'])
                                    dest.write_band(id, out_image[0])

                        # return data in native format
                        LOGGER.debug('Returning data in native format')
                        LOGGER.debug(f'CRS: {memfile.crs}')
                        return memfile.read()
                else:
                    LOGGER.debug('Serializing data in memory')
                    out_meta.update(count=len(args['indexes']))

                    # gd_driver = gdal.GetDriverByName("MEM")
                    # gd_raster = gd_driver.Create('',
                    #                              out_meta['width'],
                    #                              out_meta['height'],
                    #                              out_meta['count'],
                    #                              gdal.GDT_Float32)

                    # gd_raster.SetGeoTransform([out_meta['transform'][2],
                    #                            out_meta['transform'][0],
                    #                            out_meta['transform'][1],
                    #                            out_meta['transform'][5],
                    #                            out_meta['transform'][3],
                    #                            out_meta['transform'][4]])

                    # crs = '+proj=ob_tran +o_proj=longlat +o_lon_p=0 +o_lat_p=31.758312 +lon_0=-92.402969 +R=6371229 +no_defs'
                    # srs = osr.SpatialReference()
                    # srs.ImportFromProj4(crs)
                    # gd_raster.SetProjection(srs.ExportToWkt())

                    # print(out_meta['height'], out_meta['width'])
                    # print(out_image[0].shape)
                    # print(out_meta['count'])
                    # print(gd_raster.RasterXSize)
                    # print(gd_raster.RasterYSize)

                    # # for i in (0, out_meta['count']):
                    # #     print(i)
                    # gd_raster.GetRasterBand(1).WriteArray(out_image[0])

                    # print(type(gd_raster))
                    # print(type(gd_raster.ReadRaster()))

                    # # vsimem_data = '/vsimem/foo.grib2'
                    # # gdal.FileFromMemBuffer(vsimem_data, gd_raster.ReadRaster())

                    # # dest = gdal.Open(self.data)

                    # out_ds = gdal.Translate('./in_memory_output.grib2', gd_raster)


                    # #return out_ds.ReadRaster()

                    # # with gdal.VSIFOpenL('/vsimem/RDPA.grib2', 'rb') as mem_file:
                    # #     print(type(mem_file))
                    # #     print(type(mem_file.read()))
                    # #     return mem_file.read()

                    # import io

                    # band = out_ds.GetRasterBand(1)  # Assuming you want data from the first band
                    # width = band.XSize
                    # height = band.YSize
                    # data_type = band.DataType

                    # data_buffer = out_ds.ReadRaster(0, 0, width, height, width, height, data_type)
                    # data_stream = io.BytesIO(data_buffer)
                    # data_bytes = data_stream.getvalue()

                    # print(type(data_buffer))
                    # print(type(data_stream))
                    # print(type(data_bytes))
                    # print(self.data)
                    # print(out_ds.GetDriver().ShortName)
                    # print(gd_raster.GetDriver().ShortName)
                    # print(out_ds.GetMetadata())
                    # print(gd_raster.GetMetadata())

                    # srs = osr.SpatialReference()
                    # srs.ImportFromWkt(gd_raster.GetProjectionRef())
                    # crs_mem = srs.ExportToProj4()
                    # srs = osr.SpatialReference()
                    # srs.ImportFromWkt(out_ds.GetProjectionRef())
                    # crs_grib2 = srs.ExportToProj4()

                    # print(f'mem file: {crs_mem}')
                    # print(f'grib2 file: {crs_grib2}')

                    # return data_bytes

                    # return data_bytes

                    # with MemoryFile() as memfile:
                    #     with memfile.open(**out_meta, nbits=nbits) as dest:
                            
                    #         crs = '+proj=ob_tran +o_proj=longlat +o_lon_p=0 +o_lat_p=31.758312 +lon_0=-92.402969 +R=6371229 +no_defs'
                    #         # from osgeo import ogr, osr
                    #         # srs = osr.SpatialReference()
                    #         # srs.ImportFromProj4(crs)
                    #         # out_meta.update({'crs': CRS.from_wkt(srs.ExportToWkt())})                            
                    #         #out_image._crs = crs
                    #         dest.write(out_image)

                    #         print(out_meta)

                    #         # return data in native format
                    #         LOGGER.debug('Returning data in native format')
                    #         print(type(memfile.read()))
                    #         return memfile.read()


                    LOGGER.debug('Serializing data in memory')
                    out_meta.update(count=len(args['indexes']))
                    with MemoryFile() as memfile:
                        #with memfile.open(**out_meta, nbits=nbits) as dest:
                        with memfile.open(self.data) as dest:
                            dest.write(out_image)

                        # return data in native format
                        LOGGER.debug('Returning data in native format')
                        return memfile.read()