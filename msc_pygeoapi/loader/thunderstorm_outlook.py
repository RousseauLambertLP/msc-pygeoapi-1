# =================================================================
#
# Author: Louis-Philippe Rousseau-Lambert
#             <louis-philippe.rousseaulambert@ec.gc.ca>
#
# Copyright (c) 2026 Louis-Philippe Rousseau-Lambert
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the 'Software'), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED 'AS IS', WITHOUT WARRANTY OF ANY KIND,
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
import json
import logging
import os
from pathlib import Path
import re

import click
import parse

from msc_pygeoapi import cli_options
from msc_pygeoapi.connector.elasticsearch_ import ElasticsearchConnector
from msc_pygeoapi.env import GEOMET_LOCAL_BASEPATH
from msc_pygeoapi.loader.base import BaseLoader
from msc_pygeoapi.util import configure_es_connection

LOGGER = logging.getLogger(__name__)

# index settings
INDEX_BASENAME = 'lp-thunderstorm_outlook-'
JSON_FILENAME = 'lp-active-thunderstorm-outlooks.json'
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

MAPPINGS = {
    'properties': {
        'geometry': {'type': 'geo_shape'},
        'properties': {
            'properties': {
                'publication_datetime': {
                    'type': 'date',
                    'format': 'strict_date_time_no_millis'
                },
                'expiration_datetime': {
                    'type': 'date',
                    'format': 'strict_date_time_no_millis'
                },
                'validity_datetime': {
                    'type': 'date',
                    'format': 'strict_date_time_no_millis'
                }
            }
        }
    }
}

SETTINGS = {
    'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
    'mappings': {}
}


class ThunderstormOutlookLoader(BaseLoader):
    """Thunder storm outlook loader"""

    def __init__(self, conn_config={}):
        """initializer"""

        BaseLoader.__init__(self)

        self.datetime = None
        self.es_index = None
        self.file_id = None
        self.filename = None
        self.filepath = None
        self.product_sub_type = None
        self.product_type = None

        self.conn = ElasticsearchConnector(conn_config)

        SETTINGS['mappings'] = MAPPINGS
        if not self.conn.exists(INDEX_BASENAME):
            index_setting = {
                'mappings': SETTINGS['mappings'],
                'settings': SETTINGS['settings']
                }
            LOGGER.debug(f'Creating index {INDEX_BASENAME}')
            self.conn.create(INDEX_BASENAME, index_setting)

    def parse_filename(self, filename):
        """
        Parses an thunderstorm filename

        :return: `bool` of parse status
        """

        LOGGER.debug(f'{filename=}')
        self.file_id = re.sub(r'_v\d+\.json', '', filename)
        path_pattern = '{yyyyddmmThhmmZ}_MSC_ThunderstormOutlook_{product_type}_{product_sub_type}_PT{hours}H{minutes}M' # noqa
        filename_parse = parse.parse(path_pattern, self.file_id).named

        self.product_type = filename_parse['product_type'].lower()
        self.product_sub_type = filename_parse['product_sub_type'].lower()

        self.date_ = datetime.strptime(
            filename_parse['yyyyddmmThhmmZ'], '%Y%m%dT%H%MZ'
        )
        self.index_date = datetime.strftime(self.date_, '%Y-%m-%dt%H%Mz')

        return True

    def generate_geojson_features(self):
        """
        Generates and yields a series of thunderstorm outlook.
        They are returned as Elasticsearch bulk API upsert actions,
        with documents in GeoJSON to match the Elasticsearch index mappings.

        :returns: Generator of Elasticsearch actions to upsert the
                  thunderstorm outlook
        """

        with open(self.filepath.resolve()) as f:
            data = json.load(f)['features']

        features = []

        if len(data) > 0:
            for feature in data:

                # flatten metobject properties
                metobj = feature['properties']['metobject']
                for item, values in metobj.items():
                    try:
                        metobj_flat_item = self.flatten_json(item,
                                                             values,
                                                             'metobject')
                        feature['properties'].update(metobj_flat_item)
                        feature['properties']['file_id'] = self.file_id
                    except Exception as err:
                        msg = f'Error while flattening Thunderstorm JSON {err}'
                        LOGGER.error(f'{msg}')
                        pass

                del feature['properties']['metobject']
                f_exp_datetime = feature['properties']['expiration_datetime']
                exp_datetime = datetime.strptime(f_exp_datetime,
                                                 DATETIME_FORMAT)

                if exp_datetime > datetime.now():
                    features.append(feature)

            # check if id is already in ES and if amendment is +=1
            amendment = features[0]['properties']['amendment']
            is_amend = self.check_if_amend(self.file_id, amendment)

            if is_amend['update']:
                for outlook in features:
                    action = {
                        '_id': outlook['properties']['id'],
                        '_index': self.es_index,
                        '_op_type': 'update',
                        'doc': outlook,
                        'doc_as_upsert': True
                    }

                    yield action

                for id_ in is_amend['id_list']:
                    self.conn.Elasticsearch.delete(index=self.es_index,
                                                   id=id_)
        else:
            LOGGER.warning(f'empty thunderstorm outlook json in {self.filename}')

            version = re.search(r'v(\d+)\.json$', self.filename).group(1)
            if int(version) > 1:
                # we need to delete the associated outlooks
                query = {
                    "query": {
                        "match": {
                            "properties.file_id": self.file_id
                        }
                    }
                }
                self.conn.Elasticsearch.delete_by_query(index=self.es_index,
                                                        body=query)       

    def flatten_json(self, key, values, parent_key=''):
        """
        flatten GeoJSON properties

        :returns: item array
        """

        items = {}
        new_key = f'{parent_key}.{key}'
        value = values
        if isinstance(values, dict):
            for sub_key, sub_value in values.items():
                new_key = f'{parent_key}.{key}.{sub_key}'
                items[new_key] = sub_value
        else:
            items[new_key] = value
        return items

    def check_if_amend(self, file_id, amendment):
        """
        check if the thunderstorm outlook is the newest version

        :returns: `bool` if latest, id_list for ids to delete
        """

        upt_ = True
        id_list = []

        query = {
            "query": {
                "match": {
                    "properties.file_id": file_id
                }
            }
        }

        # Fetch the document
        try:
            result = self.conn.Elasticsearch.search(index=self.es_index,
                                                    body=query)
            if result:
                hit = result['hits']['hits'][0]
                es_amendement = hit["_source"]['properties']['amendment']
                if es_amendement >= amendment:
                    upt_ = False
                else:
                    for id_ in result['hits']['hits']:
                        id_list.append(id_['_id'])
        except Exception:
            LOGGER.warning(f'Item ({file_id}) does not exist in index')

        return {'update': upt_, 'id_list': id_list}

    def generate_local_copy(self):
        """
        Query Elasticsearch for all thunderstorm outlooks features
        and write them as a GeoJSON FeatureCollection to the local filesystem
        at::

            GEOMET_LOCAL_BASEPATH/thunderstorm-outlooks/active-thunderstorm-outlooks.json

        The output directory is created if it does not already exist.

        :returns: `bool` of status result
        """

        self.conn.Elasticsearch.indices.refresh(
            index=self.es_index,
            ignore_unavailable=True
        )

        indexes_to_fetch = []
        idx_name = '{}*'.format(INDEX_BASENAME)

        query = {
            'query': {'match_all': {}}
        }

        features = []

        try:
            count_result = self.conn.Elasticsearch.count(
                index=idx_name,
                body=query,
                ignore_unavailable=True
            )
            total = count_result['count']

            LOGGER.info(f'{total} items found for {idx_name}')

            page_size = 10000
            offset = 0

            while offset < total:
                result = self.conn.Elasticsearch.search(
                    index=INDEX_BASENAME,
                    body=query,
                    size=page_size,
                    from_=offset,
                    ignore_unavailable=True
                )
                hits = result.get('hits', {}).get('hits', [])
                for hit in hits:
                    features.append(hit['_source'])
                offset += page_size

        except Exception as err:
            LOGGER.warning(f'Failed to query ES for local copy: {err}')
            return False

        feature_collection = {
            'type': 'FeatureCollection',
            'features': features
        }

        output_path = os.path.join(GEOMET_LOCAL_BASEPATH,
                                    'thunderstorm-outlooks',
                                    JSON_FILENAME)

        output_dir = os.path.dirname(output_path)

        try:
            os.makedirs(output_dir, exist_ok=True)
            with open(output_path, 'w') as f:
                json.dump(feature_collection, f)
            LOGGER.debug(f'Local copy written to {output_path}')
        except Exception as err:
            LOGGER.warning(f'Failed to write local copy to {output_path}: {err}')  # noqa

        return True

    def load_data(self, filepath):
        """
        loads data from event to target

        :returns: `bool` of status result
        """

        self.filepath = Path(filepath)
        LOGGER.debug(f'Received file {self.filepath}')

        self.filename = self.filepath.name
        self.parse_filename(self.filename)

        sub_index = f'{self.product_type}-{self.product_sub_type}'
        index_name = f"{INDEX_BASENAME}{sub_index}"
        self.es_index = f'{index_name}.{self.index_date}'

        # using "or []" to avoid having current_indices = None
        current_indices = (self.conn.get(f"{index_name}*")) or []
        LOGGER.debug(f'Current indices {current_indices}')

        is_more_recent = all(
            self.date_ >= datetime.strptime('.'.join(idx.split('.')[1:]),
                                            '%Y-%m-%dt%H%Mz')
            for idx in current_indices
            )
        LOGGER.debug(f'Is new file more recent --> {is_more_recent}')

        if is_more_recent:

            # create index
            self.conn.create(self.es_index, {'mappings': MAPPINGS})

            # generate geojson features
            package = self.generate_geojson_features()
            try:
                r = self.conn.submit_elastic_package(package)
                LOGGER.debug(f'Result: {r}')

                # Delete old indices
                LOGGER.debug(f'Deleting previous indexes: {current_indices}')
                for idx in current_indices:
                    self.conn.delete(idx)

                # LOGGER.debug(f'Creating local copy for GeoMet-Weather')
                # self.generate_local_copy()

                return True
            except Exception as err:
                LOGGER.warning(f'Error indexing: {err}')
                return False


@click.group()
def thunderstorm_outlook():
    """Manages thunderstorm outlook index"""
    pass


@click.command()
@click.pass_context
@cli_options.OPTION_FILE()
@cli_options.OPTION_DIRECTORY()
@cli_options.OPTION_ELASTICSEARCH()
@cli_options.OPTION_ES_USERNAME()
@cli_options.OPTION_ES_PASSWORD()
@cli_options.OPTION_ES_IGNORE_CERTS()
def add(ctx, file_, directory, es, username, password, ignore_certs):
    """add data to system"""

    if file_ is None and directory is None:
        raise click.ClickException('Missing --file/-f or --dir/-d option')

    conn_config = configure_es_connection(es, username, password, ignore_certs)

    files_to_process = []

    if file_ is not None:
        files_to_process = [file_]
    elif directory is not None:
        for root, dirs, files in os.walk(directory):
            for f in [f for f in files if f.endswith('.json')]:
                files_to_process.append(os.path.join(root, f))
        files_to_process.sort(key=os.path.getmtime)

    for file_to_process in files_to_process:
        loader = ThunderstormOutlookLoader(conn_config)
        result = loader.load_data(file_to_process)

        if not result:
            click.echo('features not generated')


@click.command()
@click.pass_context
@cli_options.OPTION_ELASTICSEARCH()
@cli_options.OPTION_ES_USERNAME()
@cli_options.OPTION_ES_PASSWORD()
@cli_options.OPTION_ES_IGNORE_CERTS()
@cli_options.OPTION_YES(
    prompt='Are you sure you want to delete old outlooks?'
)
def clean_outlooks(ctx, es, username, password, ignore_certs):
    """Delete expired outlook documents"""

    conn_config = configure_es_connection(es, username, password, ignore_certs)
    conn = ElasticsearchConnector(conn_config)

    click.echo('Deleting documents older than datetime.now()')
    now = datetime.now().strftime(DATETIME_FORMAT)

    query = {
        'query': {
            'range': {
                'properties.expiration_datetime': {
                    'lte': now
                }
            }
        }
    }

    conn.Elasticsearch.delete_by_query(index=INDEX_BASENAME, body=query)

    # click.echo('Creating a local copy of thunderstorm outlooks')
    # loader = ThunderstormOutlookLoader(conn_config)
    # loader.generate_local_copy()


@click.command()
@click.pass_context
@cli_options.OPTION_ELASTICSEARCH()
@cli_options.OPTION_ES_USERNAME()
@cli_options.OPTION_ES_PASSWORD()
@cli_options.OPTION_ES_IGNORE_CERTS()
@cli_options.OPTION_INDEX_TEMPLATE()
@cli_options.OPTION_YES(
    prompt='Are you sure you want to delete this index?'
)
def delete_index(ctx, es, username, password, ignore_certs, index_template):
    """Delete thunderstorm outlooks index"""

    conn_config = configure_es_connection(es, username, password, ignore_certs)
    conn = ElasticsearchConnector(conn_config)

    click.echo(f'Deleting indexes {INDEX_BASENAME}')
    conn.delete(INDEX_BASENAME)

    if index_template:
        click.echo(f'Deleting index template {INDEX_BASENAME}')
        conn.delete_template(INDEX_BASENAME)

    click.echo('Done')


thunderstorm_outlook.add_command(add)
thunderstorm_outlook.add_command(clean_outlooks)
thunderstorm_outlook.add_command(delete_index)
