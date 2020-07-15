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
import logging
from lxml import etree
import os
import re

from msc_pygeoapi.env import (MSC_PYGEOAPI_ES_TIMEOUT, MSC_PYGEOAPI_ES_URL,
                              MSC_PYGEOAPI_ES_AUTH)
from msc_pygeoapi.loader.base import BaseLoader
from msc_pygeoapi.util import (click_abort_if_false, get_es,
                               json_pretty_print, strftime_rfc3339)

LOGGER = logging.getLogger(__name__)

# Index settings
INDEX_NAME = 'raqdps-fw_ce-hotspots'

DAYS_TO_KEEP = 365

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
                    'rep_date': {
                        'type': 'date',
                        'format': "YYYY-MM-DD'T'HH:mm:ss'Z'"
                    },
                    'tfc': {
                        'type': 'float'
                    }
                }
            }
        }
    }
}


class RAQDPSFWCEHotspotsRealtimeLoader(BaseLoader):
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

        :param filepath: filepath for parsing the hotspots file

        :returns: True/False
        """

        data = self.CEhotspots2geojson(filepath)

        try:
            self.bulk_data = []
            for doc in data:
                op_dict = {
                    'index': {
                        '_index': INDEX_NAME,
                        '_type': '_doc'
                    }
                }
                op_dict['index']['_id'] = doc['properties']['identifier']
                self.bulk_data.append(op_dict)
                self.bulk_data.append(doc)
            r = self.ES.bulk(index=INDEX_NAME, body=self.bulk_data)

            LOGGER.debug('Result: {}'.format(r))

            click.echo('done importing in ES')

        except Exception as err:
            LOGGER.warning('Error bulk indexing: {}'.format(err))
            return False


    def CEhotspots2geojson(self, filepath):
        """
        Create GeoJSON that will be use to display CE hotspots

        :param filepath: filepath to the json file

        :returns: xml as json object
        """

        with open(filepath, 'r') as ff:
            features = json.loads(ff.read())

            features = features['features']

            for i in range(0, len(features)):
                date_ = features[i]['rep_date']
                date_ = strftime_rfc3339(date_.strptime('%Y/%m/%d %H:%M:%S'))
                features[i]['rep_date'] = date_

        return features



@click.group()
def ce_hotspots():
    """Manages CE hotspots index"""
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
            for f in [file for file in files if file.endswith('.json')]:
                files_to_process.append(os.path.join(root, f))

    for file_to_process in files_to_process:
        plugin_def = {
            'filename_pattern': 'model_raqdps-fw/cumulative_effects/json',
            'handler': 'msc_pygeoapi.loader.raqdps-fw-ce-hotspots.RAQDPSFWCEHotspotsRealtimeLoader'  # noqa
        }
        loader = RAQDPSFWCEHotspotsRealtimeLoader(plugin_def)
        result = loader.load_data(file_to_process)
        if result:
            click.echo('GeoJSON features generated: {}'.format(
                json_pretty_print(loader.bulk_data)))


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
                'properties.expires': {
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


ce_hotspots.add_command(add)
ce_hotspots.add_command(clean_records)
ce_hotspots.add_command(delete_index)
