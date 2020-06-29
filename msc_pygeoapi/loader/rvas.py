# =================================================================
#
# Author: Louis-Philippe Rousseau-Lambert
#         <Louis-Philippe.RousseauLambert2@canada.ca>
#
# Copyright (c) 2020 Louis-Philippe Rousseau-Lambert
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
from datetime import datetime, timedelta
import json
import logging
from lxml import etree
import os
import random

from msc_pygeoapi.env import (MSC_PYGEOAPI_ES_TIMEOUT, MSC_PYGEOAPI_ES_URL,
                              MSC_PYGEOAPI_ES_AUTH, MSC_PYGEOAPI_BASEPATH)
from msc_pygeoapi.loader.base import BaseLoader
from msc_pygeoapi.util import (click_abort_if_false, get_es,
                               _get_date_format, DATETIME_RFC3339_FMT,
                               json_pretty_print)

LOGGER = logging.getLogger(__name__)

# cleanup settings
DAYS_TO_KEEP = 30

CARDINAL = ['N', 'NW', 'NE', 'NNW', 'NNE',
            'S', 'SW', 'SE', 'SSW', 'SSE',
            'W', 'WSW', 'WNW',
            'E', 'ESE', 'ENE']

# Index settings
INDEX_NAME = 'rvas'

SETTINGS = {
    'settings': {
        'number_of_shards': 1,
        'number_of_replicas': 0
    },
    'mappings': {
        'properties': {
            'geometry': {
                'type': 'geo_shape'
            },
            'properties': {
                'properties': {
                    'identifier': {
                        'type': 'text',
                        'fields': {
                            'raw': {
                                'type': 'keyword'
                            }
                        }
                    },
                    'sation': {
                        'type': 'text',
                        'fields': {
                            'raw': {
                                'type': 'keyword'
                            }
                        }
                    },
                    'image': {
                        'type': 'text',
                        'fields': {
                            'raw': {
                                'type': 'keyword'
                            }
                        }
                    },
                    'direction': {
                        'type': 'text',
                        'fields': {
                            'raw': {
                                'type': 'keyword'
                            }
                        }
                    },
                    'timestamp': {
                        'type': 'date',
                        'format': "YYYY-MM-DD'T'HH:mm:ss'Z'"
                    }
                }
            }
        }
    }
}


class RVASRealtimeLoader(BaseLoader):
    """Current conditions real-time loader"""

    def __init__(self, plugin_def):
        """initializer"""

        BaseLoader.__init__(self)

        self.ES = get_es(MSC_PYGEOAPI_ES_URL, MSC_PYGEOAPI_ES_AUTH)

        if not self.ES.indices.exists(INDEX_NAME):
            self.ES.indices.create(index=INDEX_NAME, body=SETTINGS,
                                   request_timeout=MSC_PYGEOAPI_ES_TIMEOUT)

    def load_data(self, filepath):
        """
        fonction from base to load the data in ES

        :param filepath: filepath for parsing the current condition file

        :returns: True/False
        """

        # For an eventual lookup table
        #with open(os.path.join(MSC_PYGEOAPI_BASEPATH,
        #                       'lib/msc_pygeoapi/',
        #                       'resources/wxo_lookup.json')) as json_file:
        #    wxo_lookup = json.load(json_file)

        rvas_lookup = None

        data = self.rvas2json(rvas_lookup, filepath)

        try:
            r = self.ES.index(index=INDEX_NAME,
                              id=data['properties']['identifier'],
                              body=data)
            LOGGER.debug('Result: {}'.format(r))
            return True
        except Exception as err:
            LOGGER.warning('Error indexing: {}'.format(err))
            return False

    def rvas2json(self, rvas_lookup, filepath):
        """
        main for generating rvas feature

        :param rvas_lookup: json file to have the station id
        :param filepath: filepath to parse and convert to json

        :returns: json object
        """
        # url: http://ddi.cmc.ec.gc.ca/20200521/MSC-INTERNET/RVAS/23/GVRD3-20200521-2330-SE.jpg
        # /data/geomet/ddi/20200521/MSC-INTERNET/RVAS/23/GVRD3-20200521-2330-SE.jpg'

        filename = filepath.split('/')[-1]

        station, date, time, dir_ = filename.split('-')

        datetime = '{}{}00'.format(date, time)

        dir_ = dir_.split('.')[0]

        if dir_ in CARDINAL:
            card_dir = dir_
        else:
            card_dir = 'null'

        filepath_split = filepath.split('ddi')[1]
        image = 'http://ddi.cmc.ec.gc.ca{}'.format(filepath_split)

        id_ = '{}-{}-{}'.format(station, datetime, dir_)


        ran_x = random.randrange(-175,-65,1)
        ran_y = random.randrange(20,85,1)
        geom = [ran_x, ran_y]

        timestamp = _get_date_format(datetime).strftime(DATETIME_RFC3339_FMT)

        rvas = {
            'type': "Feature",
            'properties': {
                'identifier': id_,
                'station': station,
                'image': image,
                'direction': card_dir,
                'timestamp': timestamp,
            },
            'geometry': {
                'type': "Point",
                'coordinates': geom
            }
        }

        return rvas

@click.group()
def rvas():
    """Manages rvas index"""
    pass


@click.command()
@click.pass_context
@click.option('--file', '-f', 'file_',
              type=click.Path(exists=True, resolve_path=True),
              help='Path to file')
@click.option('--directory', '-d', 'directory',
              type=click.Path(exists=True, resolve_path=True,
                              dir_okay=True, file_okay=False),
              help='Path to directory')
def add(ctx, file_, directory):
    """add data to system"""

    if all([file_ is None, directory is None]):
        raise click.ClickException('Missing --file/-f or --dir/-d option')

    files_to_process = []

    if file_ is not None:
        files_to_process = [file_]
    elif directory is not None:
        for root, dirs, files in os.walk(directory):
            for f in [file for file in files if file.endswith('.xml')]:
                files_to_process.append(os.path.join(root, f))
        files_to_process.sort(key=os.path.getmtime)

    for file_to_process in files_to_process:
        plugin_def = {
            'filename_pattern': 'MSC-INTERNET/RVAS',
            'handler': 'msc_pygeoapi.loader.rvas.RVASRealtimeLoader'  # noqa
        }
        loader = RVASRealtimeLoader(plugin_def)
        result = loader.load_data(file_to_process)


@click.command()
@click.pass_context
@click.option('--days', '-d', default=DAYS_TO_KEEP, type=int,
              help='delete documents older than n days (default={})'.format(
                  DAYS_TO_KEEP))
@click.option('--yes', is_flag=True, callback=click_abort_if_false,
              expose_value=False,
              prompt='Are you sure you want to delete old documents?')
def clean_records(ctx, days):
    """Delete old documents"""

    es = get_es(MSC_PYGEOAPI_ES_URL, MSC_PYGEOAPI_ES_AUTH)

    older_than = (datetime.now() - timedelta(days=days)).strftime(
        '%Y-%m-%d %H:%M')
    click.echo('Deleting documents older than {} ({} days)'.format(
        older_than, days))

    query = {
        'query': {
            'range': {
                'properties.datetime': {
                    'lte': older_than
                }
            }
        }
    }

    es.delete_by_query(index=INDEX_NAME, body=query)


@click.command()
@click.pass_context
@click.option('--yes', is_flag=True, callback=click_abort_if_false,
              expose_value=False,
              prompt='Are you sure you want to delete this index?')
def delete_index(ctx):
    """Delete current conditions index"""

    es = get_es(MSC_PYGEOAPI_ES_URL, MSC_PYGEOAPI_ES_AUTH)

    if es.indices.exists(INDEX_NAME):
        es.indices.delete(INDEX_NAME)

rvas.add_command(add)
rvas.add_command(clean_records)
rvas.add_command(delete_index)
