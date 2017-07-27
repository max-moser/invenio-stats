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

"""Module tests."""

from __future__ import absolute_import, print_function

import datetime
import uuid

import pytest
from elasticsearch_dsl import Search
from flask import Flask
from invenio_queues.proxies import current_queues
from invenio_search import current_search, current_search_client
from mock import patch

from invenio_stats import InvenioStats
from invenio_stats.aggregations import StatAggregator
from invenio_stats.proxies import current_stats
from invenio_stats.tasks import aggregate_events, process_events


def test_version():
    """Test version import."""
    from invenio_stats import __version__
    assert __version__


def test_init():
    """Test extension initialization."""
    app = Flask('testapp')
    ext = InvenioStats(app)
    assert 'invenio-stats' in app.extensions

    app = Flask('testapp')
    ext = InvenioStats()
    assert 'invenio-stats' not in app.extensions
    ext.init_app(app)
    assert 'invenio-stats' in app.extensions


def test_event_queues_declare(app, event_entrypoints):
    """Test that event queues are declared properly."""
    for event in current_stats.events.values():
        assert not event.queue.exists
    current_queues.declare()
    for event in current_stats.events.values():
        assert event.queue.exists


def test_publish_and_consume_events(app, event_entrypoints):
    """Test that events are published and consumed properly."""
    event_type = 'file-download'
    events = [{"payload": "test {}".format(idx)} for idx in range(3)]
    current_queues.declare()
    current_stats.publish(event_type, events)
    assert list(current_stats.consume(event_type)) == events


def test_wrong_intervals(app):
    """Test wrong interval error."""
    with pytest.raises(ValueError):
        StatAggregator(current_search_client, 'test', 'test', 'month', 'day')


def test_overwriting_aggregations(app, mock_user_ctx, sequential_ids):
    """1. Create sample file download event and process it.
       2. Run aggregator and write count, in aggregation index.
       3. Create new events and repeat procedure to assert that the
          results within the interval of the previous events
          overwrite the aggregation,
          by checking that the document version has increased."""
    for t in current_search.put_templates(ignore=[400]):
        pass

    event_type = 'file-download'
    events = [dict(timestamp=datetime.datetime.strptime('2017-06-01',
                                                        '%Y-%m-%d').
                   isoformat(),
                   # What:
                   bucket_id=str(sequential_ids[0]),
                   file_id=str(sequential_ids[0]),
                   filename='test.pdf',
                   visitor_id=100),
              dict(timestamp=datetime.datetime.utcnow().
                   replace(microsecond=0).isoformat(),
                   # What:
                   bucket_id=str(sequential_ids[0]),
                   file_id=str(sequential_ids[0]),
                   filename='test.pdf',
                   visitor_id=100)]
    current_queues.declare()
    current_stats.publish(event_type, events)
    process_events(['file-download'])
    current_search_client.indices.flush(index='*')
    aggregate_events(['file-download-agg'])

    res = current_search_client.search(index='stats-file-download',
                                       version=True)
    for hit in res['hits']['hits']:
        if 'file_id' in hit['_source'].keys():
            assert hit['_version'] == 1

    new_events = [dict(timestamp=datetime.datetime.utcnow().
                       replace(microsecond=0).isoformat(),
                       # What:
                       bucket_id=str(sequential_ids[0]),
                       file_id=str(sequential_ids[0]),
                       filename='test.pdf',
                       visitor_id=100),
                  dict(timestamp=datetime.datetime.strptime('3000-01-01',
                                                            '%Y-%m-%d').
                       isoformat(),
                       # What:
                       bucket_id=str(sequential_ids[0]),
                       file_id=str(sequential_ids[0]),
                       filename='test.pdf',
                       visitor_id=100)]
    current_stats.publish(event_type, new_events)
    process_events(['file-download'])
    current_search_client.indices.flush(index='*')

    class NewDate(datetime.datetime):

        @classmethod
        def utcnow(cls):
            return cls(3000, 2, 1)
    datetime.datetime = NewDate

    with patch('datetime.datetime', NewDate):
        aggregate_events(['file-download-agg'])

    res = current_search_client.search(index='stats-file-download',
                                       version=True)
    for hit in res['hits']['hits']:
        if 'file_id' in hit['_source'].keys() and \
                hit['_index'] == 'stats-file-download-2017-07':
            assert hit['_version'] == 2
        elif 'file_id' in hit['_source'].keys():
            assert hit['_version'] == 1

    current_search_client.indices.delete(index='events-stats-file-download')
    current_search_client.indices.delete(index='stats-file-download')


@pytest.mark.parametrize('indexed_events',
                         [dict(file_number=5,
                               event_number=50,
                               start_date=datetime.date(2015, 1, 28),
                               end_date=datetime.date(2015, 2, 3))],
                         indirect=['indexed_events'])
def test_date_range(app, indexed_events):
    aggregate_events(['file-download-agg'])
    current_search_client.indices.flush(index='*')

    query = Search(using=current_search_client,
                   index='stats-file-download')[0:30].sort('file_id')
    results = query.execute()
    for result in results:
        if 'file_id' in result:
            assert result.count == 50


def test_file_download_receiver(app, mock_user_ctx, sequential_ids, objects):
    from invenio_files_rest.signals import file_downloaded
    for j in range(len(objects)):
        file_obj = objects[0]
        file_obj.bucket_id = sequential_ids[0]
        with app.test_request_context(
            headers={'USER_AGENT':
                     'Mozilla/5.0 (Windows NT 6.1; WOW64) '
                     'AppleWebKit/537.36 (KHTML, like Gecko)'
                     'Chrome/45.0.2454.101 Safari/537.36'}):
            file_downloaded.send(app, obj=file_obj)
            process_events(['file-download'])
