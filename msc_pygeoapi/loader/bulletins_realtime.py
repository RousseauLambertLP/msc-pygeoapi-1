# =================================================================
#
# Author: Tom Kralidis <tom.kralidis@ec.gc.ca>
#
# Copyright (c) 2023 Tom Kralidis
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
from datetime import datetime
import logging

from msc_pygeoapi import cli_options
from msc_pygeoapi.connector.elasticsearch_ import ElasticsearchConnector
from msc_pygeoapi.loader.base import BaseLoader
from msc_pygeoapi.util import (
    check_es_indexes_to_delete,
    configure_es_connection,
)


LOGGER = logging.getLogger(__name__)

# cleanup settings
DAYS_TO_KEEP = 30

# index settings
INDEX_BASENAME = 'bulletins.'

SETTINGS = {
    'order': 0,
    'version': 1,
    'index_patterns': [INDEX_BASENAME],
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
                    'datetime': {
                        'type': 'date',
                        'format': 'strict_date_hour_minute||strict_date_optional_time'  # noqa
                    },
                    'identifier': {
                        'type': 'text',
                        'fields': {
                            'raw': {
                                'type': 'keyword'
                            }
                        }
                    },
                    'issuer_code': {
                        'type': 'text',
                        'fields': {
                            'raw': {
                                'type': 'keyword'
                            }
                        }
                    },
                    'issuing_office': {
                        'type': 'text',
                        'fields': {
                            'raw': {
                                'type': 'keyword'
                            }
                        }
                    },
                    'type': {
                        'type': 'text',
                        'fields': {
                            'raw': {
                                'type': 'keyword'
                            }
                        }
                    },
                    'url': {
                        'type': 'text',
                        'fields': {
                            'raw': {
                                'type': 'keyword'
                            }
                        }
                    }
                }
            },
        }
    }
}


class BulletinsRealtimeLoader(BaseLoader):
    """Bulletins real-time loader"""

    def __init__(self, filepath, conn_config={}):
        """initializer"""

        BaseLoader.__init__(self)

        today_date = datetime.today().strftime('%Y%m%d')
        self.DD_URL = f'https://dd.weather.gc.ca/{today_date}/WXO-DD/bulletins/alphanumeric' # noqa
        self.conn = ElasticsearchConnector(conn_config)
        self.conn.create_template(INDEX_BASENAME, SETTINGS)

    def load_data(self, filepath):
        """
        loads data from event to target

        :param filepath: filepath to data on disk

        :returns: `bool` of status result
        """

        LOGGER.debug(filepath)

        data = self.bulletin2dict(filepath)

        b_dt = datetime.strptime(data['properties']['datetime'],
                                 '%Y-%m-%dT%H:%M')
        b_dt2 = b_dt.strftime('%Y-%m-%d')
        es_index = f'{INDEX_BASENAME}{b_dt2}'

        try:
            r = self.conn.Elasticsearch.index(
                index=es_index, id=data['id'], body=data
            )
            LOGGER.debug(f'Result: {r}')
            return True
        except Exception as err:
            LOGGER.warning(f'Error indexing: {err}')
            return False

    def bulletin2dict(self, filepath):
        """
        convert a bulletin into a GeoJSON object

        :param filepath: path to filename

        :returns: `dict` of GeoJSON
        """

        dict_ = {
            'type': 'Feature',
            'geometry': None,
            'properties': {}
        }

        try:
            bulletin_path = filepath.split('/alphanumeric/')[1]
        except IndexError as err:
            LOGGER.warning(f'no bulletin path: {err}')
            raise RuntimeError(err)

        identifier = bulletin_path.replace('/', '.')
        issuer_name = None
        issuer_country = None

        dict_['id'] = dict_['properties']['identifier'] = identifier

        tokens = bulletin_path.split('/')

        yyyymmdd = tokens[0]
        hh = tokens[3]
        filename = tokens[-1]

        yyyy = yyyymmdd[0:4]
        mm = yyyymmdd[4:6]
        dd = yyyymmdd[6:8]

        min_ = filename.split('_')[2][-2:]

        datetime_ = f'{yyyy}-{mm}-{dd}T{hh}:{min_}'

        # TODO: use real coordinates

        dict_['properties']['datetime'] = datetime_
        dict_['properties']['type'] = tokens[1]
        dict_['properties']['issuer_code'] = tokens[2]
        dict_['properties']['issuer_name'] = issuer_name
        dict_['properties']['issuer_country'] = issuer_country
        dict_['properties']['issuing_office'] = tokens[2][2:]
        dict_['properties']['url'] = f'{self.DD_URL}/{bulletin_path}'

        return dict_


@click.group()
def bulletins_realtime():
    """Manages bulletins index"""
    pass


@click.command()
@click.pass_context
@cli_options.OPTION_DAYS(
    default=DAYS_TO_KEEP,
    help=f'Delete indexes older than n days (default={DAYS_TO_KEEP})'
)
@cli_options.OPTION_ELASTICSEARCH()
@cli_options.OPTION_ES_USERNAME()
@cli_options.OPTION_ES_PASSWORD()
@cli_options.OPTION_ES_IGNORE_CERTS()
@cli_options.OPTION_YES(
    prompt='Are you sure you want to delete old indexes?'
)
def clean_indexes(ctx, days, es, username, password, ignore_certs):
    """Clean bulletins indexes older than n number of days"""

    conn_config = configure_es_connection(es, username, password, ignore_certs)
    conn = ElasticsearchConnector(conn_config)

    indexes = conn.get(f'{INDEX_BASENAME}*')

    if indexes:
        indexes_to_delete = check_es_indexes_to_delete(indexes, days)
        if indexes_to_delete:
            click.echo(f'Deleting indexes {indexes_to_delete}')
            conn.delete(','.join(indexes_to_delete))

    click.echo('Done')


@click.command()
@click.pass_context
@cli_options.OPTION_ELASTICSEARCH()
@cli_options.OPTION_ES_USERNAME()
@cli_options.OPTION_ES_PASSWORD()
@cli_options.OPTION_ES_IGNORE_CERTS()
@cli_options.OPTION_INDEX_TEMPLATE()
@cli_options.OPTION_YES(
    prompt='Are you sure you want to delete these indexes?'
)
def delete_indexes(ctx, es, username, password, ignore_certs, index_template):
    """Delete all hydrometric realtime indexes"""

    conn_config = configure_es_connection(es, username, password, ignore_certs)
    conn = ElasticsearchConnector(conn_config)

    all_indexes = f'{INDEX_BASENAME}*'

    click.echo(f'Deleting indexes {all_indexes}')
    conn.delete(all_indexes)

    if index_template:
        click.echo(f'Deleting index template {INDEX_BASENAME}')
        conn.delete_template(INDEX_BASENAME)

    click.echo('Done')


bulletins_realtime.add_command(clean_indexes)
bulletins_realtime.add_command(delete_indexes)
