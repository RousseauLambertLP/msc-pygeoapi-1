# =================================================================
#
# Author: Louis-Philippe Rousseau-Lambert
#         <Louis-Philippe.RousseauLambert2@canada.ca>
#
# Copyright (c) 2019 Louis-Philippe Rousseau-Lambert
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

import click
from datetime import date, datetime, timedelta
import unicodecsv as csv
import io
import json
import logging
import os
import re

from osgeo import gdal, osr
import pyproj
from pyproj import Proj, transform
import yaml
from yaml import CLoader

LOGGER = logging.getLogger(__name__)

PROCESS_METADATA = {
    'version': '0.1.0',
    'id': 'raster-drill-weather',
    'title': 'Raster Drill process for weather data',
    'description': 'Raster Drill process for weather data',
    'keywords': ['raster drill weather'],
    'links': [{
        'type': 'text/html',
        'rel': 'canonical',
        'title': 'information',
        'href': 'https://example.org/process',
        'hreflang': 'en-US'
    }],
    'inputs': [{
        'id': 'layer',
        'title': 'layer name',
        'input': {
            'literalDataDomain': {
                'dataType': 'string',
                'valueDefinition': {
                    'anyValue': True
                }
            }
        },
        'minOccurs': 1,
        'maxOccurs': 1
    }, {
        'id': 'y',
        'title': 'y coordinate',
        'input': {
            'literalDataDomain': {
                'dataType': 'float',
                'valueDefinition': {
                    'anyValue': True
                }
            }
        },
        'minOccurs': 1,
        'maxOccurs': 1
    }, {
        'id': 'x',
        'title': 'x coordinate',
        'input': {
            'literalDataDomain': {
                'dataType': 'float',
                'valueDefinition': {
                    'anyValue': True
                }
            }
        },
        'minOccurs': 1,
        'maxOccurs': 1
    }, {
        'id': 'time-begin',
        'title': 'forecast time to begin the serie',
        'input': {
            'literalDataDomain': {
                'dataType': 'timestamp',
                'valueDefinition': {
                    'anyValue': True
                }
            }
        },
        'minOccurs': 1,
        'maxOccurs': 1
    }, {
        'id': 'time-end',
        'title': 'forecast time to end the serie',
        'input': {
            'literalDataDomain': {
                'dataType': 'timestamp',
                'valueDefinition': {
                    'anyValue': True
                }
            }
        },
        'minOccurs': 1,
        'maxOccurs': 1
    }, {
        'id': 'model-run',
        'title': 'model run to use',
        'input': {
            'literalDataDomain': {
                'dataType': 'timestamp',
                'valueDefinition': {
                    'anyValue': True
                }
            }
        },
        'minOccurs': 1,
        'maxOccurs': 1
   }, {
        'id': 'format',
        'title': 'format: GeoJSON or CSV',
        'input': {
            'literalDataDomain': {
                'dataType': 'string',
                'valueDefinition': {
                    'anyValue': True
                }
            }
        },
        'minOccurs': 1,
        'maxOccurs': 1
    }],
    'outputs': [{
        'id': 'raster-drill-weather-response',
        'title': 'output raster drill weather',
        'output': {
            'formats': [{
                'mimeType': 'application/json'
            }, {
                'mimeType': 'text/csv'
            }]
        }
    }]
}


def get_time_list(layer_conf, time_begin, time_end):

    LOGGER.info('creating time list')
    time_list = []
    fh_int = layer_conf['forecast_model']['forecast_hour_interval']
    time_step = timedelta(hours=fh_int)
    if time_begin < time_end and time_begin != time_end:
        time_list.append(time_begin)
        time = time_begin
        while time < time_end:
            time += time_step
            time_list.append(time)
    else:
        LOGGER.error('invalid time begin and end')

    return time_list

def get_list_file(layer_conf, time_begin, time_end, model_run, GEOMET_WEATHER_BASEPATH):

    basepath = layer_conf['forecast_model']['basepath']
    wx_variable = layer_conf['name']

    time_list = get_time_list(layer_conf, time_begin, time_end)

    yyyymmdd = '{}{}{}'.format(model_run.year,
                               str(model_run.month).zfill(2),
                               str(model_run.day).zfill(2))

    mr_hour = str(model_run.hour).zfill(2)

    file_list = []
    for time_ in time_list:
        variable_path = layer_conf['forecast_model']['variable_intermediate_path']
        filename_pattern = layer_conf['forecast_model']['filename_pattern']

        forecast_hour = time_ - model_run
        forecast_hour = int((forecast_hour.days * 24) + (forecast_hour.seconds / 3600))

        variable_path = variable_path.format(model_run_path=mr_hour,
                                             forecast_hour_path=str(forecast_hour).zfill(3))
        filename_pattern = filename_pattern.format(wx_variable=wx_variable,
                                                   YYYYMMDD=yyyymmdd,
                                                   model_run=mr_hour,
                                                   forecast_hour=str(forecast_hour).zfill(3))

        filename = os.path.join(GEOMET_WEATHER_BASEPATH,
                                basepath,
                                variable_path,
                                filename_pattern)

        file_list.append(filename)

    return {'file_list': file_list, 'time_list': time_list}


def geo2xy(ds, x, y): 
    """ 
    transforms geographic coordinate to x/y pixel values

    :param ds: GDAL dataset object
    :param x: x coordinate
    :param y: y coordinate

    :returns: list of x/y pixel values
    """

    LOGGER.debug('Running affine transformation')
    geotransform = ds.GetGeoTransform()
    projection = ds.GetProjection()
    srs = osr.SpatialReference()
    srs.ImportFromWkt(projection)
    outProj = pyproj.Proj(projparams=srs.ExportToProj4())

    inProj = Proj(init='epsg:4326')

    x, y = transform(inProj, outProj, x, y)
    origin_x = geotransform[0]
    origin_y = geotransform[3]

    width = geotransform[1]
    height = geotransform[5]

    x = int((x - origin_x) / width)
    y = int((y - origin_y) / height)

    return (x, y)


def get_data(list_file, x_, y_):

    LOGGER.debug('Running through bands')
    data_ = []

    for file_ in list_file:
        try:
            ds = gdal.Open(file_)
            srcband = ds.GetRasterBand(1)
            array = srcband.ReadAsArray().tolist()
        except RuntimeError as err:
            msg = 'Cannot open file: {}, assigning NA'.format(err)
            data_.append('NA')

        try:
            data_.append(array[y_][x_])
        except IndexError as err:
            msg = 'Invalid x/y value: {}'.format(err)
            LOGGER.exception(msg)

    return data_


def serialize(data_, x, y, list_time, layer_conf, format_):

    LOGGER.debug('Creating the output file')

    if format_ == 'GeoJSON':

        data = {'type': 'FeatureCollection',
                'features': []
               }

        for i in range(0, len(data_)):
            geo_dict = {
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [x, y]
                },
                'properties': {
                    'variable': layer_conf['name'],
                    'label_en': layer_conf['label_en'],
                    'label_fr': layer_conf['label_fr'],
                    'time': list_time[i].strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'value': data_[i]
                }
            }
            data['features'].append(geo_dict)

    elif format_ == 'CSV':
        row = ['values',
               'longitude',
               'latitude',
               'time',
               'label_en',
               'label_fr']

        try:
            data = io.BytesIO()
            writer = csv.writer(data)
            writer.writerow(row)
        except TypeError:
            data = io.StringIO()
            writer = csv.writer(data)
            writer.writerow(row)

        for i in range(0, len(data_)):
            writer.writerow([data_[i],
                             x,
                             y,
                             list_time[i].strftime('%Y-%m-%dT%H:%M:%SZ'),
                             layer_conf['label_en'],
                             layer_conf['label_fr']]) 

    return data

def raster_drill_weather(layer, x, y, format_, time_begin, time_end, model_run):

    from msc_pygeoapi.process.weather import (GEOMET_WEATHER_CONFIG,
                                              GEOMET_WEATHER_BASEPATH)
    LOGGER.info('start raster drilling')

    if format_ not in ['CSV', 'GeoJSON']:
        msg = 'Invalid format'
        LOGGER.error(msg)
        raise ValueError(msg)

    with open(GEOMET_WEATHER_CONFIG) as fh:
        cfg = yaml.load(fh, Loader=CLoader)

    layer_conf = cfg['layers'][layer]

    list_ = get_list_file(layer_conf, time_begin,
                            time_end, model_run,
                            GEOMET_WEATHER_BASEPATH)

    list_file = list_['file_list']
    list_time = list_['time_list']

    try:
        LOGGER.debug('Opening first file in list for coordinates')
        ds = gdal.Open(list_file[0])
        LOGGER.debug('Transforming map coordinates into image coordinates')
        x_, y_ = geo2xy(ds, x, y)
    except RuntimeError as err:
        ds = None
        msg = 'Cannot open file: {}'.format(err)
        LOGGER.exception(msg)

    data_ = get_data(list_file, x_, y_)
    output = serialize(data_, x, y, list_time, layer_conf, format_)

    return output


@click.command('raster-drill-weather')
@click.pass_context
@click.option('--layer', help='Layer name to process')
@click.option('--x', help='x coordinate')
@click.option('--y', help='y coordinate')
@click.option('--time-begin', 'time_begin',
              type=click.DateTime(formats=['%Y-%m-%dT%H:%M:%SZ']),
              help='time series begin')
@click.option('--time-end', 'time_end',
              type=click.DateTime(formats=['%Y-%m-%dT%H:%M:%SZ']),
              help='time series end')
@click.option('--model-run', 'model_run',
              type=click.DateTime(formats=['%Y-%m-%dT%H:%M:%SZ']),
              help='model run to use for the time serie')
@click.option('--format', 'format_', type=click.Choice(['GeoJSON', 'CSV']),
              default='GeoJSON', help='output format')
def cli(ctx, layer, x, y, time_begin, time_end, model_run, format_='GeoJSON'):

    output = raster_drill_weather(layer, float(x), float(y),
                                  format_, time_begin, time_end,
                                  model_run)
    if format_ == 'GeoJSON':
        click.echo(json.dumps(output, ensure_ascii=False))
    elif format_ == 'CSV':
        click.echo(output.getvalue())


try:
    from pygeoapi.process.base import BaseProcessor, ProcessorExecuteError

    class RasterDrillWeatherProcessor(BaseProcessor):
        """Raster Drill Weather Processor"""

        def __init__(self, provider_def):
            """
            Initialize object

            :param provider_def: provider definition

            :returns: pygeoapi.process.cccs.raster_drill.RasterDrillProcessor
             """

            BaseProcessor.__init__(self, provider_def, PROCESS_METADATA)

        def execute(self, data):
            layer = data['layer']
            x = float(data['x'])
            y = float(data['y'])
            time_begin = datetime.strptime(data['time-begin'],
                                           '%Y-%m-%dT%H:%M:%SZ') 
            time_end = datetime.strptime(data['time-end'],
                                         '%Y-%m-%dT%H:%M:%SZ') 
            model_run = datetime.strptime(data['model-run'],
                                          '%Y-%m-%dT%H:%M:%SZ')
            format_ = data['format']
            
            try:
                output = raster_drill_weather(layer, float(x), float(y),
                                              format_, time_begin, time_end,
                                              model_run)
            except ValueError as err:
                msg = 'Process execution error: {}'.format(err)
                LOGGER.error(msg)
                raise ProcessorExecuteError(msg)

            if format_ == 'GeoJSON':
                dict_ = output
            elif format_ == 'CSV':
                dict_ = output.getvalue()
            else:
                msg = 'Invalid format'
                LOGGER.error(msg)
                raise ValueError(msg)

            return dict_

        def __repr__(self):
            return '<RasterDrillWeatherProcessor> {}'.format(self.name)
except (ImportError, RuntimeError):
    pass
