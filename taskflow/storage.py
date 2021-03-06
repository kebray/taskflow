# -*- coding: utf-8 -*-

# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2013 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import contextlib
import logging

from taskflow import exceptions
from taskflow.openstack.common import uuidutils
from taskflow.persistence import logbook
from taskflow import states
from taskflow.utils import misc
from taskflow.utils import threading_utils


LOG = logging.getLogger(__name__)


STATES_WITH_RESULTS = (states.SUCCESS, states.REVERTING, states.FAILURE)


def _item_from_result(result, index, name):
    if index is None:
        return result
    try:
        return result[index]
    except (IndexError, KeyError, ValueError, TypeError):
        # NOTE(harlowja): The result that the uuid returned can not be
        # accessed in the manner that the index is requesting. Perhaps
        # the result is a dictionary-like object and that key does
        # not exist (key error), or the result is a tuple/list and a
        # non-numeric key is being requested (index error), or there
        # was no result and an attempt to index into None is being
        # requested (type error).
        raise exceptions.NotFound("Unable to find result %r" % name)


class Storage(object):
    """Interface between engines and logbook

    This class provides simple interface to save task details and
    results to persistence layer for use by engines.
    """

    injector_name = '_TaskFlow_INJECTOR'

    def __init__(self, flow_detail, backend=None):
        self._result_mappings = {}
        self._reverse_mapping = {}
        self._backend = backend
        self._flowdetail = flow_detail

    def _with_connection(self, functor, *args, **kwargs):
        if self._backend is None:
            return
        with contextlib.closing(self._backend.get_connection()) as conn:
            functor(conn, *args, **kwargs)

    def add_task(self, uuid, task_name):
        """Add the task to storage

        Task becomes known to storage by that name and uuid.
        Task state is set to PENDING.
        """
        # TODO(imelnikov): check that task with same uuid or
        # task name does not exist
        td = logbook.TaskDetail(name=task_name, uuid=uuid)
        td.state = states.PENDING
        self._flowdetail.add(td)
        self._with_connection(self._save_flow_detail)
        self._with_connection(self._save_task_detail, task_detail=td)

    def _save_flow_detail(self, conn):
        # NOTE(harlowja): we need to update our contained flow detail if
        # the result of the update actually added more (aka another process
        # added item to the flow detail).
        self._flowdetail.update(conn.update_flow_details(self._flowdetail))

    def get_uuid_by_name(self, task_name):
        """Get uuid of task with given name"""
        td = self._flowdetail.find_by_name(task_name)
        if td is not None:
            return td.uuid
        else:
            raise exceptions.NotFound("Unknown task name: %r" % task_name)

    def _taskdetail_by_uuid(self, uuid):
        td = self._flowdetail.find(uuid)
        if td is None:
            raise exceptions.NotFound("Unknown task: %r" % uuid)
        return td

    def _save_task_detail(self, conn, task_detail):
        # NOTE(harlowja): we need to update our contained task detail if
        # the result of the update actually added more (aka another process
        # is also modifying the task detail).
        task_detail.update(conn.update_task_details(task_detail))

    def set_task_state(self, uuid, state):
        """Set task state"""
        td = self._taskdetail_by_uuid(uuid)
        td.state = state
        self._with_connection(self._save_task_detail, task_detail=td)

    def get_task_state(self, uuid):
        """Get state of task with given uuid"""
        return self._taskdetail_by_uuid(uuid).state

    def set_task_progress(self, uuid, progress, **kwargs):
        """Set task progress.

        :param uuid: task uuid
        :param progress: task progress
        :param kwargs: task specific progress information
        """
        td = self._taskdetail_by_uuid(uuid)
        if not td.meta:
            td.meta = {}
        td.meta['progress'] = progress
        if kwargs:
            td.meta['progress_details'] = kwargs
        else:
            if 'progress_details' in td.meta:
                td.meta.pop('progress_details')
        self._with_connection(self._save_task_detail, task_detail=td)

    def get_task_progress(self, uuid):
        """Get progress of task with given uuid.

        :param uuid: task uuid
        :returns: current task progress value
        """
        meta = self._taskdetail_by_uuid(uuid).meta
        if not meta:
            return 0.0
        return meta.get('progress', 0.0)

    def get_task_progress_details(self, uuid):
        """Get progress details of task with given uuid.

        :param uuid: task uuid
        :returns: None if progress_details not defined, else progress_details
                 dict
        """
        meta = self._taskdetail_by_uuid(uuid).meta
        if not meta:
            return None
        return meta.get('progress_details')

    def _check_all_results_provided(self, uuid, task_name, data):
        """Warn if task did not provide some of results

        This may happen if task returns shorter tuple or list or dict
        without all needed keys. It may also happen if task returns
        result of wrong type.
        """
        result_mapping = self._result_mappings.get(uuid, None)
        if result_mapping is None:
            return
        for name, index in result_mapping.items():
            try:
                _item_from_result(data, index, name)
            except exceptions.NotFound:
                LOG.warning("Task %s did not supply result "
                            "with index %r (name %s)",
                            task_name, index, name)

    def save(self, uuid, data, state=states.SUCCESS):
        """Put result for task with id 'uuid' to storage"""
        td = self._taskdetail_by_uuid(uuid)
        td.state = state
        td.results = data
        self._with_connection(self._save_task_detail, task_detail=td)

        # Warn if result was incomplete
        if not isinstance(data, misc.Failure):
            self._check_all_results_provided(uuid, td.name, data)

    def get(self, uuid):
        """Get result for task with id 'uuid' to storage"""
        td = self._taskdetail_by_uuid(uuid)
        if td.state not in STATES_WITH_RESULTS:
            raise exceptions.NotFound("Result for task %r is not known" % uuid)
        return td.results

    def reset(self, uuid, state=states.PENDING):
        """Remove result for task with id 'uuid' from storage"""
        td = self._taskdetail_by_uuid(uuid)
        td.results = None
        td.state = state
        self._with_connection(self._save_task_detail, task_detail=td)

    def inject(self, pairs):
        """Add values into storage

        This method should be used by job in order to put flow parameters
        into storage and put it to action.
        """
        pairs = dict(pairs)
        injector_uuid = uuidutils.generate_uuid()
        self.add_task(injector_uuid, self.injector_name)
        self.save(injector_uuid, pairs)
        for name in pairs.iterkeys():
            entries = self._reverse_mapping.setdefault(name, [])
            entries.append((injector_uuid, name))

    def set_result_mapping(self, uuid, mapping):
        """Set mapping for naming task results

        The result saved with given uuid would be accessible by names
        defined in mapping. Mapping is a dict name => index. If index
        is None, the whole result will have this name; else, only
        part of it, result[index].
        """
        if not mapping:
            return
        self._result_mappings[uuid] = mapping
        for name, index in mapping.iteritems():
            entries = self._reverse_mapping.setdefault(name, [])
            entries.append((uuid, index))

    def fetch(self, name):
        """Fetch named task result"""
        try:
            indexes = self._reverse_mapping[name]
        except KeyError:
            raise exceptions.NotFound("Name %r is not mapped" % name)
        # Return the first one that is found.
        for uuid, index in indexes:
            try:
                result = self.get(uuid)
                return _item_from_result(result, index, name)
            except exceptions.NotFound:
                pass
        raise exceptions.NotFound("Unable to find result %r" % name)

    def fetch_all(self):
        """Fetch all named task results known so far

        Should be used for debugging and testing purposes mostly.
        """
        result = {}
        for name in self._reverse_mapping:
            try:
                result[name] = self.fetch(name)
            except exceptions.NotFound:
                pass
        return result

    def fetch_mapped_args(self, args_mapping):
        """Fetch arguments for the task using arguments mapping"""
        return dict((key, self.fetch(name))
                    for key, name in args_mapping.iteritems())

    def set_flow_state(self, state):
        """Set flowdetails state and save it"""
        self._flowdetail.state = state
        self._with_connection(self._save_flow_detail)

    def get_flow_state(self):
        """Set state from flowdetails"""
        return self._flowdetail.state


class ThreadSafeStorage(Storage):
    __metaclass__ = threading_utils.ThreadSafeMeta
