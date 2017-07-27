# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2016 CERN.
#
# Invenio is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.
#
# In applying this license, CERN does not
# waive the privileges and immunities granted to it by virtue of its status
# as an Intergovernmental Organization or submit itself to any jurisdiction.

"""Invenio module for collecting statistics."""

from __future__ import absolute_import, print_function

from collections import namedtuple

from invenio_queues.proxies import current_queues
from pkg_resources import iter_entry_points
from werkzeug.utils import cached_property

from . import config
from .errors import DuplicateAggregationError, DuplicateEventError, \
    UnknownAggregationError, UnknownEventError
from .indexer import EventsIndexer
from .utils import load_or_import_from_config


class _InvenioStatsState(object):
    """State object for Invenio stats."""

    def __init__(self, app,
                 entry_point_group_events,
                 entry_point_group_aggs,
                 entry_point_group_queries):
        self.app = app
        self.exchange = app.config['STATS_MQ_EXCHANGE']
        self.suffix = app.config['STATS_INDICES_SUFFIX']
        self.enabled_events = app.config['STATS_EVENTS']
        self.enabled_aggregations = app.config['STATS_AGGREGATIONS']
        self.enabled_queries = app.config['STATS_QUERIES']
        self.entry_point_group_events = entry_point_group_events
        self.entry_point_group_aggs = entry_point_group_aggs
        self.entry_point_group_queries = entry_point_group_queries

    @cached_property
    def _events_config(self):
        """Load events configuration."""
        # import iter_entry_points here so that it can be mocked in tests
        result = {}
        for ep in iter_entry_points(
                group=self.entry_point_group_events):
            for cfg in ep.load()():
                if cfg['event_type'] not in self.enabled_events:
                    continue
                elif cfg['event_type'] in result:
                    raise DuplicateEventError(
                        'Duplicate event {0} in entry point '
                        '{1}'.format(cfg['event_type'], ep.name))
                result[cfg['event_type']] = cfg
        return result

    @cached_property
    def events(self):
        EventConfig = namedtuple('EventConfig',
                                 ['queue', 'config',
                                  'templates', 'processor'])
        # import iter_entry_points here so that it can be mocked in tests
        result = {}
        config = self._events_config

        for event in self.enabled_events:
            if event not in config.keys():
                raise UnknownEventError(
                    'Unknown event {0} '.format(event))

        for cfg in config.values():
            queue = current_queues.queues[
                'stats-{}'.format(cfg['event_type'])]
            result[cfg['event_type']] = EventConfig(
                queue=queue,
                config=cfg,
                templates=cfg['templates'],
                processor=cfg['processor'](queue)
            )
        return result

    @cached_property
    def _aggregations_config(self):
        """Load aggregation configurations."""
        result = {}
        for ep in iter_entry_points(
                group=self.entry_point_group_aggs):
            for cfg in ep.load()():
                if cfg['aggregation_name'] not in self.enabled_aggregations:
                    continue
                elif cfg['aggregation_name'] in result:
                    raise DuplicateAggregationError(
                        'Duplicate aggregation {0} in entry point '
                        '{1}'.format(cfg['event_type'], ep.name))
                result[cfg['aggregation_name']] = cfg
        return result

    @cached_property
    def aggregations(self):
        AggregationConfig = namedtuple('AggregationConfig',
                                       ['config', 'templates',
                                        'aggregator', 'call_params'])
        result = {}
        config = self._aggregations_config

        for aggregation in self.enabled_aggregations:
            if aggregation not in config.keys():
                raise UnknownAggregationError(
                    'Unknown aggregation {0} '.format(aggregation))

        for cfg in config.values():
            result[cfg['aggregation_name']] = AggregationConfig(
                config=cfg,
                templates=cfg['templates'],
                aggregator=cfg['aggregator'],
                call_params=cfg['call_params']
            )
        return result

    # def __init__(self, app, entry_point_group):
    #     self.app = app
    #     self.event_types = []
    #     # self.queues = dict(app.extensions['invenio-queues'].queues
    @cached_property
    def _queries_config(self):
        """Load queries configuration."""
        result = {}
        for ep in iter_entry_points(group=self.entry_point_group_queries):
            for cfg in ep.load()():
                if cfg['query_name'] not in self.enabled_queries:
                    continue
                elif cfg['query_name'] in result:
                    raise DuplicateQueryError(
                        'Duplicate query {0} in entry point '
                        '{1}'.format(cfg['query'], ep.name))
                result[cfg['query_name']] = cfg
        return result

    @cached_property
    def queries(self):
        QueryConfig = namedtuple(
            'QueryConfig',
            ['query_class', 'permission_factory', 'query_config', 'config']
        )
        result = {}
        config = self._queries_config

        for query in self.enabled_queries:
            if query not in config.keys():
                raise UnknownQueryError(
                    'Unknown query {0} '.format(query))

        for cfg in config.values():
            queue = current_queues.queues[
                'stats-{}'.format(cfg['query_name'])]
            result[cfg['query_name']] = QueryConfig(
                config=cfg,
                query_class=cfg['query_class'],
                query_config=cfg.get('query_config', {}),
                permission_factory=cfg.get('permission_factory')
            )
        return result

    @cached_property
    def permission_factory(self):
        """Load default permission factory for Buckets collections."""
        return load_or_import_from_config(
            'STATS_PERMISSION_FACTORY', app=self.app
        )

    def indexer(self, event_type):
        # TODO: Allow customization of indexer and suffix
        return EventsIndexer(
            current_queues.queues['stats-{}'.format(event_type)],
            prefix=self.app.config['STATS_INDICES_PREFIX'],
            suffix=self.suffix
        )

    def publish(self, event_type, events):
        """Publish events."""
        assert event_type in self.events
        current_queues.queues['stats-{}'.format(event_type)].publish(events)

    def consume(self, event_type, no_ack=True, payload=True):
        """Comsume all pending events."""
        assert event_type in self.events
        return current_queues.queues['stats-{}'.format(event_type)].consume(
            payload=payload)

    # def register_eventtype(self, event_type, package_name):
    #     """Register an event type."""
    #     if event_type in self.event_types:
    #         raise RuntimeError('Event type already registered.')
    #     self.event_types.append(event_type)

    # def load_entry_point_group(self, entry_point_group):
    #     """Load actions from an entry point group."""
    #     for ep in iter_entry_points(group=entry_point_group):
    #         self.register_eventtype(ep.name, ep.module_name)


class InvenioStats(object):
    """Invenio-Stats extension."""

    def __init__(self, app=None, **kwargs):
        """Extension initialization."""
        if app:
            self.init_app(app, **kwargs)

    def init_app(self, app,
                 entry_point_group_events='invenio_stats.events',
                 entry_point_group_aggs='invenio_stats.aggregations',
                 entry_point_group_queries='invenio_stats.queries'):
        """Flask application initialization."""
        self.init_config(app)

        state = _InvenioStatsState(
            app,
            entry_point_group_events=entry_point_group_events,
            entry_point_group_aggs=entry_point_group_aggs,
            entry_point_group_queries=entry_point_group_queries
        )
        self._state = app.extensions['invenio-stats'] = state

        if app.config['STATS_REGISTER_RECEIVERS']:
            from .contrib.receivers import register_receivers
            register_receivers(app)

        return state

    def init_config(self, app):
        """Initialize configuration."""
        for k in dir(config):
            if k.startswith('STATS_'):
                app.config.setdefault(k, getattr(config, k))

    def __getattr__(self, name):
        """Proxy to state object."""
        return getattr(self._state, name, None)
