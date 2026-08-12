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
OGC API - EDR provider for the ECCC CMIP6 100km Zarr cubes.

Important design constraint, read before configuring this provider:
-----------------------------------------------------------------
OGC API - EDR's query model only has generic axes for x/y/z/t plus a
per-collection 'instance' selector. There is nowhere in the EDR spec -
or in pygeoapi's EDR routing layer - for arbitrary extra facets like
scenario/percentile/season/period to travel through as runtime query
parameters. Concretely: pygeoapi.api.environmental_data_retrieval's
get_collection_edr_query() builds a *fixed* set of query_args (query_type,
instance, datetime_, select_properties, wkt, z, bbox, ...) and passes
exactly that to provider.position()/.cube() - nothing else in the query
string reaches the provider.

So facets on this provider are pinned AT CONFIGURATION TIME via
provider_def['options']['fixed'], and you deploy one pygeoapi collection
per (scenario, percentile, [season], [period]) combination you want to
expose over EDR - e.g.:

    providers:
      - type: edr
        name: msc_pygeoapi.provider.cmip6_xarray_edr.CMIP6EDRProvider
        data: /data/cmip6_zarr_store/monthly.zarr
        x_field: lon
        y_field: lat
        time_field: time
        options:
          fixed:
            scenario: ssp245
            percentile: 50

This mirrors the pattern MSC's own GeoMet-Climate WMS already uses for
this exact dataset - one layer per facet combination (e.g.
CMIP6-SSP585_AirTempAnomaly-Pct50_2081-2100_P0Y) rather than one layer
with N generic custom axes.

If you need genuine runtime facet selection instead, use the OGC API -
Coverages endpoint (cmip6_xarray.CMIP6CoverageProvider) - the Coverages
`subset=` parameter supports arbitrary axes, unlike EDR.

Implementation note: position()/cube() and their helper methods are
reused verbatim from pygeoapi's XarrayEDRProvider (they only reference
generic attributes - self.x_field, self.time_field, etc. - already set
up correctly by CMIP6CoverageProvider, so no CMIP6-specific changes are
needed there). Only __init__ differs, to apply the fixed-facet selection
before anything else runs.
"""

import logging

from pygeoapi.provider.base_edr import BaseEDRProvider
from pygeoapi.provider.xarray_edr import XarrayEDRProvider

from msc_pygeoapi.provider.cmip6_xarray import CMIP6CoverageProvider

LOGGER = logging.getLogger(__name__)


class CMIP6EDRProvider(BaseEDRProvider, CMIP6CoverageProvider):
    """OGC API - EDR provider for the CMIP6 Zarr cubes"""

    def __init__(self, provider_def):
        """
        Initialize object

        :param provider_def: provider definition

        :returns: msc_pygeoapi.provider.cmip6_xarray_edr.CMIP6EDRProvider
        """
        BaseEDRProvider.__init__(self, provider_def)
        CMIP6CoverageProvider.__init__(self, provider_def)

        if self.extra_dims:
            msg = (
                'CMIP6EDRProvider has unresolved facet axes '
                f'{self.extra_dims} - EDR has no runtime mechanism for '
                'these, pin them via provider options.fixed (see module '
                'docstring). Configure one collection per facet '
                'combination.'
            )
            LOGGER.warning(msg)

    # Reused verbatim from pygeoapi's XarrayEDRProvider - see module
    # docstring for why no CMIP6-specific override is needed here.
    position = XarrayEDRProvider.position
    cube = XarrayEDRProvider.cube
    _make_datetime = XarrayEDRProvider._make_datetime
    _get_time_range = XarrayEDRProvider._get_time_range
    _parse_time_metadata = XarrayEDRProvider._parse_time_metadata
    _configure_bbox = XarrayEDRProvider._configure_bbox