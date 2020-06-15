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

import numpy as np
from osgeo import gdal, osr
from pyproj import Proj, transform
import yaml
from yaml import CLoader

LOGGER = logging.getLogger(__name__)

PROCESS_METADATA = {
    'version': '0.1.0',
    'id': 'generate-vigilance',
    'title': 'Generate vigilance process for weather data',
    'description': 'Generate vigilance process for weather data',
    'keywords': ['generate vigilance weather'],
    'links': [{
        'type': 'text/html',
        'rel': 'canonical',
        'title': 'information',
        'href': 'https://example.org/process',
        'hreflang': 'en-US'
    }],
    'inputs': [{
        'id': 'model',
        'title': 'model name: REPS or GEPS',
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
        'id': 'variable',
        'title': 'variable used for vigilance',
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
        'id': 'accumulation-hour',
        'title': 'accumulation period (3, 6, 12, 24, 48, 72)',
        'input': {
            'literalDataDomain': {
                'dataType': 'int',
                'valueDefinition': {
                    'anyValue': True
                }
            }
        },
        'minOccurs': 1,
        'maxOccurs': 1
    }, {
        'id': 'thresholds',
        'title': 'Thresholds values for vigilance products',
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
        'id': 'forecast-hour',
        'title': 'forecast hour to use',
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
   }],
    'outputs': [{
        'id': 'generate-vigilance-response',
        'title': 'output wms vigilance product',
        'output': {
            'formats': [{
                'mimeType': 'application/json'
            }, {
                'mimeType': 'text/csv'
            }]
        }
    }]
}


def get_layer_name(model, var, ah, thresholds):

    layer_array = []
    for i in thresholds:
        lyr_tmp = '{}.DIAG.{}_{}.ERGE{}'.format(model.upper(),
                                                ah,
                                                var.upper(),
                                                i)
        layer_array.append(lyr_tmp)

    return layer_array


def get_file_name(layer_conf, fh, mr, GEOMET_WEATHER_BASEPATH):

    basepath = layer_conf['forecast_model']['basepath']
    wx_variable = layer_conf['name']

    yyyymmdd = '{}{}{}'.format(mr.year,
                               str(mr.month).zfill(2),
                               str(mr.day).zfill(2))

    mr_hour = str(mr.hour).zfill(2)

    variable_path = layer_conf['forecast_model']['variable_intermediate_path']
    filename_pattern = layer_conf['forecast_model']['filename_pattern']

    forecast_hour = fh - mr
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

    if os.path.isfile(filename):
        return filename 
    else:
        LOGGER.error('not a valid input')


def get_new_array(layers, lyr_dict):

    lyr = layers[0]
    try:
        ds = gdal.Open(lyr_dict[lyr]['filename'])
        srcband = ds.GetRasterBand(lyr_dict[lyr]['band'])
        array1 = srcband.ReadAsArray()
        array1[array1 < 40] = 0
        array1[(array1 >= 40) & (array1 < 60)] = 1
        array1[array1 >= 60] = 2
    except RuntimeError as err:
        msg = 'Cannot open file: {}, assigning NA'.format(err)
        LOGGER.error(msg)

    lyr = layers[1]
    try:
        ds = gdal.Open(lyr_dict[lyr]['filename'])
        srcband = ds.GetRasterBand(lyr_dict[lyr]['band'])
        array2 = srcband.ReadAsArray()
        array2[(array2 >= 1) & (array2 < 20)] = 3
        array2[(array2 >= 20) & (array2 < 40)] = 4
        array2[(array2 >= 40) & (array2 < 60)] = 6
        array2[array2 >= 60] = 7
    except RuntimeError as err:
        msg = 'Cannot open file: {}, assigning NA'.format(err)
        LOGGER.error(msg)

    lyr = layers[2]
    try:
        ds = gdal.Open(lyr_dict[lyr]['filename'])
        srcband = ds.GetRasterBand(lyr_dict[lyr]['band'])
        array3 = srcband.ReadAsArray()
        array3[(array3 >= 1) & (array3 < 20)] = 5
        array3[(array3 >= 20) & (array3 < 40)] = 8
        array3[(array3 >= 40) & (array3 < 60)] = 9
        array3[array3 >= 60] = 10
    except RuntimeError as err:
        msg = 'Cannot open file: {}, assigning NA'.format(err)
        LOGGER.error(msg)

    max_array = np.maximum(array1, array2)
    max_array = np.maximum(max_array, array3)

    return max_array


def create_file(new_array, layers, lyr_dict):

    ds = gdal.Open(lyr_dict[layers[0]]['filename'])

    driver = gdal.GetDriverByName('GTiff')
    xsize = ds.RasterXSize
    ysize = ds.RasterYSize

    do = driver.Create('/users/dor/afss/lor/ENV/gitProject/geomet2/build/tmp/vigilance.tif',
                       xsize, ysize, 1, gdal.GDT_Byte)

    srs = osr.SpatialReference()
    wkt = ds.GetProjection()
    srs.ImportFromWkt(wkt)
    do.SetProjection(srs.ExportToWkt())
    gt = ds.GetGeoTransform()
    do.SetGeoTransform(gt)

    outband=do.GetRasterBand(1)
    outband.SetStatistics(np.min(new_array), np.max(new_array), np.average(new_array), np.std(new_array))
    outband.WriteArray(new_array)
    do = None


def generate_vigilance(model, var, ah, thresholds, fh, mr):

    from msc_pygeoapi.process.weather import (GEOMET_WEATHER_CONFIG,
                                              GEOMET_WEATHER_BASEPATH)
    LOGGER.info('start raster drilling')
    
    if len(thresholds) != 3:
        msg = 'Invalid number of thresholds'
        LOGGER.error(msg)
        raise ValueError(msg)
    
    with open(GEOMET_WEATHER_CONFIG) as ff: 
        cfg = yaml.load(ff, Loader=CLoader)

    layers = get_layer_name(model, var, ah, thresholds)

    lyr_dict = {}    
    for lyr in layers:    
        layer_conf = cfg['layers'][lyr]
 
        filename = get_file_name(layer_conf,
                                 fh, mr,
                                 GEOMET_WEATHER_BASEPATH)

        for j in layer_conf['processing']:
            if j.startswith('BANDS='):
                band = int(j.replace('BANDS=', ''))

        lyr_dict[lyr] = {'filename': filename,
                         'band': band}

    new_array = get_new_array(layers, lyr_dict)

    create_file(new_array, layers, lyr_dict)

    return 'http://geomet-dev-02.cmc.ec.gc.ca:8019/?service=wms&version=1.3.0&request=GetMap&LAYERS=VIGILANCE&height=1000&width=1500&format=image/png&BBOX=-90,-180,90,180&CRS=EPSG:4326'

@click.command('generate-vigilance')
@click.pass_context
@click.option('--model', type=click.Choice(['REPS', 'GEPS']),
              default='REPS', help='Model (REPS|GEPS) to process')
@click.option('--variable', 'var', help='variable to process')
@click.option('--accumulation', 'ah',  type=click.Choice(['3', '6', '12', '24', '48', '72']),
              default='3', help='Acuumulation hour for the products')
@click.option('--thresholds', help='Thresholds to create the vigilance product')
@click.option('--forecast-hour', 'fh',
              type=click.DateTime(formats=['%Y-%m-%dT%H:%M:%SZ']),
              help='Forecast hour to create the vigilance')
@click.option('--model-run', 'mr',
              type=click.DateTime(formats=['%Y-%m-%dT%H:%M:%SZ']),
              help='model run to use for the time serie')
def cli(ctx, model, var, ah, thresholds, fh, mr):

    output = generate_vigilance(model, var, ah, thresholds.split(','), fh, mr)
    click.echo(output)

try:
    from pygeoapi.process.base import BaseProcessor, ProcessorExecuteError

    class GenerateVigilanceProcessor(BaseProcessor):
        """Vigilance product Processor"""

        def __init__(self, provider_def):
            """
            Initialize object

            :param provider_def: provider definition

            :returns: pygeoapi.process.weather.generate_vigilance.GenerateVigilanceProcessor
             """

            BaseProcessor.__init__(self, provider_def, PROCESS_METADATA)

        def execute(self, data):
            model = data['model']
            var = data['variable']
            ah = data['accumulation-hour']
            thresholds = data['thresholds']
            fh = datetime.strptime(data['forecast-hour'],
                                    '%Y-%m-%dT%H:%M:%SZ')
            mr = datetime.strptime(data['model-run'],
                                   '%Y-%m-%dT%H:%M:%SZ')

            try:
                output = generate_vigilance(model, var, ah, thresholds.split(','), fh, mr)
                return output
            except ValueError as err:
                msg = 'Process execution error: {}'.format(err)
                LOGGER.error(msg)
                raise ProcessorExecuteError(msg)


        def __repr__(self):
            return '<GenerateVigilanceProcessor> {}'.format(self.name)
except (ImportError, RuntimeError):
    pass
