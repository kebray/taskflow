# -*- coding: utf-8 -*-

# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
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

import collections

from networkx.algorithms import dag
from networkx.classes import digraph

from taskflow import exceptions as exc
from taskflow import flow


class Flow(flow.Flow):
    """Graph flow pattern

    Nested flows will be executed according to their dependencies
    that will be resolved using data tasks provide and require.

    Note: Cyclic dependencies are not allowed.
    """

    def __init__(self, name, uuid=None):
        super(Flow, self).__init__(name, uuid)
        self._graph = digraph.DiGraph()

    def link(self, u, v):
        if not self._graph.has_node(u):
            raise ValueError('Item %s not found to link from' % (u))
        if not self._graph.has_node(v):
            raise ValueError('Item %s not found to link to' % (v))
        self._graph.add_edge(u, v)

        # Ensure that there is a valid topological ordering.
        if not dag.is_directed_acyclic_graph(self._graph):
            self._graph.remove_edge(u, v)
            raise exc.DependencyFailure("No path through the items in the"
                                        " graph produces an ordering that"
                                        " will allow for correct dependency"
                                        " resolution")

    def add(self, *items):
        """Adds a given task/tasks/flow/flows to this flow."""
        requirements = collections.defaultdict(list)
        provided = {}

        def update_requirements(node):
            for value in node.requires:
                requirements[value].append(node)

        for node in self:
            update_requirements(node)
            for value in node.provides:
                provided[value] = node

        try:
            for item in items:
                self._graph.add_node(item)
                update_requirements(item)
                for value in item.provides:
                    if value in provided:
                        raise exc.DependencyFailure(
                            "%(item)s provides %(value)s but is already being"
                            " provided by %(flow)s and duplicate producers"
                            " are disallowed"
                            % dict(item=item.name,
                                   flow=provided[value].name,
                                   value=value))
                    provided[value] = item

                for value in item.requires:
                    if value in provided:
                        self.link(provided[value], item)

                for value in item.provides:
                    if value in requirements:
                        for node in requirements[value]:
                            self.link(item, node)

        except Exception:
            self._graph.remove_nodes_from(items)
            raise

        return self

    def __len__(self):
        return self._graph.number_of_nodes()

    def __iter__(self):
        for child in self._graph.nodes_iter():
            yield child

    @property
    def provides(self):
        provides = set()
        for subflow in self:
            provides.update(subflow.provides)
        return provides

    @property
    def requires(self):
        requires = set()
        for subflow in self:
            requires.update(subflow.requires)
        return requires - self.provides

    @property
    def graph(self):
        return self._graph
