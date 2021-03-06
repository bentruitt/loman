import networkx as nx
from enum import Enum
from collections import OrderedDict, deque, namedtuple
import inspect
import decorator
import dill
import six
import sys
import pandas as pd
import matplotlib as mpl
import traceback
import logging
import types
import itertools
import functools
import pydotplus
from datetime import datetime
import os
import tempfile


LOG = logging.getLogger('loman.computeengine')


class States(Enum):
    """Possible states for a computation node"""
    PLACEHOLDER = 0
    UNINITIALIZED = 1
    STALE = 2
    COMPUTABLE = 3
    UPTODATE = 4
    ERROR = 5


_state_colors = {
    None: '#ffffff',                    # xkcd white
    States.PLACEHOLDER: '#f97306',      # xkcd orange
    States.UNINITIALIZED: '#0343df',    # xkcd blue
    States.STALE: '#ffff14',            # xkcd yellow
    States.COMPUTABLE: '#9dff00',       # xkcd bright yellow green
    States.UPTODATE: '#15b01a',         # xkcd green
    States.ERROR: '#e50000'             # xkcd red
}

# Node attributes
_AN_VALUE = 'value'
_AN_STATE = 'state'
_AN_FUNC = 'func'
_AN_GROUP = 'group'
_AN_TAG = 'tag'
_AN_ARGS = 'args'
_AN_KWDS = 'kwds'
_AN_TIMING = 'timing'

# Edge attributes
_AE_PARAM = 'param'

# System tags
_T_SERIALIZE = '__serialize__'
_T_EXPANSION = '__expansion__'

Error = namedtuple('Error', ['exception', 'traceback'])
NodeData = namedtuple('NodeData', [_AN_STATE, _AN_VALUE])
TimingData = namedtuple('TimingData', ['start', 'end', 'duration'])


class ComputationException(Exception):
    pass


class MapException(ComputationException):
    def __init__(self, message, results):
        super(MapException, self).__init__(message)
        self.results = results


class LoopDetectedException(ComputationException):
    pass


class NonExistentNodeException(ComputationException):
    pass


class _ParameterType(Enum):
    ARG = 1
    KWD = 2

_ParameterItem = namedtuple('ParameterItem', ['type', 'name', 'value'])


def _node(func, *args, **kws):
    return func(*args, **kws)


def node(comp, name=None, *args, **kw):
    def inner(f):
        if name is None:
            comp.add_node(f.__name__, f, *args, **kw)
        else:
            comp.add_node(name, f, *args, **kw)
        return decorator.decorate(f, _node)
    return inner


class ConstantValue(object):
    def __init__(self, value):
        self.value = value

C = ConstantValue


class _ComputationAttributeView(object):
    def __init__(self, get_attribute_list, get_attribute, get_item=None):
        self.get_attribute_list = get_attribute_list
        self.get_attribute = get_attribute
        self.get_item = get_item
        if self.get_item is None:
            self.get_item = get_attribute

    def __dir__(self):
        return self.get_attribute_list()

    def __getattr__(self, attr):
        try:
            return self.get_attribute(attr)
        except KeyError:
            raise AttributeError()

    def __getitem__(self, key):
        return self.get_item(key)

    def __getstate__(self):
        return {
            'get_attribute_list': self.get_attribute_list,
            'get_attribute': self.get_attribute,
            'get_item': self.get_item
        }

    def __setstate__(self, state):
        self.get_attribute_list = state['get_attribute_list']
        self.get_attribute = state['get_attribute']
        self.get_item = state['get_item']

_Signature = namedtuple('_Signature', ['kwd_params', 'default_params', 'has_var_args', 'has_var_kwds'])

if six.PY3:
    if sys.version_info >= (3, 5):
        def _get_signature(func):
            sig = inspect.signature(func)
            pk = inspect._ParameterKind
            has_var_args = any(p.kind == pk.VAR_POSITIONAL for p in sig.parameters.values())
            has_var_kwds = any(p.kind == pk.VAR_KEYWORD for p in sig.parameters.values())
            all_keyword_params = [param_name for param_name, param in sig.parameters.items()
                                  if param.kind in (pk.POSITIONAL_OR_KEYWORD, pk.KEYWORD_ONLY)]
            default_params = [param_name for param_name, param in sig.parameters.items()
                                  if param.kind in (pk.POSITIONAL_OR_KEYWORD, pk.KEYWORD_ONLY) and param.default != inspect._empty]
            return _Signature(all_keyword_params, default_params, has_var_args, has_var_kwds)
    elif sys.version_info >= (3, 4):
        def _get_signature(func):
            sig = inspect.signature(func)
            has_var_args = any(p.kind == inspect._VAR_POSITIONAL for p in sig.parameters.values())
            has_var_kwds = any(p.kind == inspect._VAR_KEYWORD for p in sig.parameters.values())
            all_keyword_params = [param_name for param_name, param in sig.parameters.items()
                                  if param.kind in (inspect._POSITIONAL_OR_KEYWORD, inspect._KEYWORD_ONLY)]
            default_params = [param_name for param_name, param in sig.parameters.items()
                                  if param.kind in (inspect._POSITIONAL_OR_KEYWORD, inspect._KEYWORD_ONLY) and param.default != inspect._empty]
            return _Signature(all_keyword_params, default_params, has_var_args, has_var_kwds)
    else:
        raise Exception("Only Python3 >=3.4 is supported")
elif six.PY2:
    def _get_signature(func):
        argspec = inspect.getargspec(func)
        has_var_args = argspec.varargs is not None
        has_var_kwds = argspec.keywords is not None
        all_keyword_params = argspec.args
        if argspec.defaults is None:
            default_params = []
        else:
            n_default_params = len(argspec.defaults)
            default_params = argspec.args[-n_default_params:]
        return _Signature(all_keyword_params, default_params, has_var_args, has_var_kwds)
else:
    raise Exception("Only Pythons 2 and 3 supported")


class Computation(object):
    def __init__(self):
        self.dag = nx.DiGraph()
        self.v = _ComputationAttributeView(self.nodes, self.value, self.value)
        self.s = _ComputationAttributeView(self.nodes, self.state, self.state)
        self.i = _ComputationAttributeView(self.nodes, self.get_inputs, self.get_inputs)
        self.t = _ComputationAttributeView(self.nodes, self.tags, self.tags)
        self.tim = _ComputationAttributeView(self.nodes, self.get_timing, self.get_timing)

    def add_node(self, name, func=None, **kwargs):
        """
        Adds or updates a node in a computation

        :param name: Name of the node to add. This may be any hashable object.
        :param func: Function to use to calculate the node if the node is a calculation node. By default, the input nodes to the function will be implied from the names of the function    parameters. For example, a parameter called ``a`` would be taken from the node called ``a``. This can be modified with the ``kwds`` parameter.
        :type func: Function, default None
        :param args: Specifies a list of nodes that will be used to populate arguments of the function positionally for a calculation node. e.g. If args is ``['a', 'b', 'c']`` then the function would be called with three parameters, taken from the nodes 'a', 'b' and 'c' respectively.
        :type args: List, default None
        :param kwds: Specifies a mapping from parameter name to the node that should be used to populate that parameter when calling the function for a calculation node. e.g. If args is ``{'x': 'a', 'y': 'b'}`` then the function would be called with parameters named 'x' and 'y', and their values would be taken from nodes 'a' and 'b' respectively. Each entry in the dictionary can be read as "take parameter [key] from node [value]".
        :type kwds: Dictionary, default None
        :param value: If given, the value is inserted into the node, and the node state set to UPTODATE.
        :type value: default None
        :param serialize: Whether the node should be serialized. Some objects cannot be serialized, in which case, set serialize to False
        :type serialize: boolean, default True
        :param inspect: Whether to use introspection to determine the arguments of the function, which can be slow. If this is not set, kwds and args must be set for the function to obtain parameters.
        :type inspect: boolean, default True
        :param group: Subgraph to render node in
        :type group: default None
        :param tags: Set of tags to apply to node
        :type tags: Iterable
        """
        LOG.debug('Adding node {}'.format(str(name)))
        args = kwargs.get('args', None)
        kwds = kwargs.get('kwds', None)
        value = kwargs.get(_AN_VALUE, None)
        serialize = kwargs.get('serialize', True)
        inspect = kwargs.get('inspect', True)
        group = kwargs.get('group', None)
        tags = kwargs.get('tags', [])

        self.dag.add_node(name)
        self.dag.remove_edges_from((p, name) for p in self.dag.predecessors(name))
        node = self.dag.node[name]

        node[_AN_STATE] = States.UNINITIALIZED
        node[_AN_VALUE] = None
        node[_AN_TAG] = set()
        node[_AN_GROUP] = group
        node[_AN_ARGS] = {}
        node[_AN_KWDS] = {}

        if func:
            node[_AN_FUNC] = func
            args_count = 0
            if args:
                args_count = len(args)
                for i, arg in enumerate(args):
                    if isinstance(arg, ConstantValue):
                        node[_AN_ARGS][i] = arg.value
                    else:
                        input_vertex_name = arg
                        if not self.dag.has_node(input_vertex_name):
                            self.dag.add_node(input_vertex_name, attr_dict={_AN_STATE: States.PLACEHOLDER})
                        self.dag.add_edge(input_vertex_name, name, attr_dict={_AE_PARAM: (_ParameterType.ARG, i)})
            if inspect:
                signature = _get_signature(func)
                param_names = set()
                if not signature.has_var_args:
                    param_names.update(signature.kwd_params[args_count:])
                if signature.has_var_kwds and kwds is not None:
                    param_names.update(kwds.keys())
                default_names = signature.default_params
            else:
                if kwds is None:
                    param_names = []
                else:
                    param_names = kwds.keys()
                default_names = []
            for param_name in param_names:
                value_source = kwds.get(param_name, param_name) if kwds else param_name
                if isinstance(value_source, ConstantValue):
                    node[_AN_KWDS][param_name] = value_source.value
                else:
                    in_node_name = value_source
                    if not self.dag.has_node(in_node_name):
                        if param_name in default_names:
                            continue
                        else:
                            self.dag.add_node(in_node_name, attr_dict={_AN_STATE: States.PLACEHOLDER})
                    self.dag.add_edge(in_node_name, name, attr_dict={_AE_PARAM: (_ParameterType.KWD, param_name)})

        if func or value is not None:
            self._set_descendents(name, States.STALE)
        if value is not None:
            self._set_uptodate(name, value)
        if node[_AN_STATE] == States.UNINITIALIZED:
            self._try_set_computable(name)
        self.set_tag(name, tags)
        if serialize:
            self.set_tag(name, _T_SERIALIZE)

    def _set_tag_one(self, name, tag):
        self.dag.node[name][_AN_TAG].add(tag)

    def set_tag(self, name, tag):
        """
        Set tags on a node or nodes. Ignored if tags are already set.
        
        :param name: Node or nodes to set tag for
        :param tag: Tag to set
        """
        _apply_n(self._set_tag_one, name, tag)

    def _clear_tag_one(self, name, tag):
        self.dag.node[name][_AN_TAG].discard(tag)

    def clear_tag(self, name, tag):
        """
        Clear tag on a node or nodes. Ignored if tags are not set.

        :param name: Node or nodes to clear tags for
        :param tag: Tag to clear
        """
        _apply_n(self._clear_tag_one, name, tag)

    def delete_node(self, name):
        """
        Delete a node from a computation

        When nodes are explicitly deleted with ``delete_node``, but are still depended on by other nodes, then they will be set to PLACEHOLDER status. In this case, if the nodes that depend on a PLACEHOLDER node are deleted, then the PLACEHOLDER node will also be deleted.

        :param name: Name of the node to delete. If the node does not exist, a ``NonExistentNodeException`` will be raised.
        """
        LOG.debug('Deleting node {}'.format(str(name)))

        if name not in self.dag:
            raise NonExistentNodeException('Node {} does not exist'.format(str(name)))

        if len(self.dag.successors(name)) == 0:
            preds = self.dag.predecessors(name)
            self.dag.remove_node(name)
            for n in preds:
                if self.dag.node[n][_AN_STATE] == States.PLACEHOLDER:
                    self.delete_node(n)
        else:
            self.dag.node[name][_AN_STATE] = States.PLACEHOLDER

    def insert(self, name, value):
        """
        Insert a value into a node of a computation

        Following insertation, the node will have state UPTODATE, and all its descendents will be COMPUTABLE or STALE.

        If an attempt is made to insert a value into a node that does not exist, a ``NonExistentNodeException`` will be raised.

        :param name: Name of the node to add.
        :param value: The value to be inserted into the node.
        """
        LOG.debug('Inserting value into node {}'.format(str(name)))

        if name not in self.dag:
            raise NonExistentNodeException('Node {} does not exist'.format(str(name)))

        node = self.dag.node[name]
        node[_AN_VALUE] = value
        node[_AN_STATE] = States.UPTODATE
        self._set_descendents(name, States.STALE)
        for n in self.dag.successors(name):
            self._try_set_computable(n)

    def insert_many(self, name_value_pairs):
        """
        Insert values into many nodes of a computation simultaneously

        Following insertation, the nodes will have state UPTODATE, and all their descendents will be COMPUTABLE or STALE. In the case of inserting many nodes, some of which are descendents of others, this ensures that the inserted nodes have correct status, rather than being set as STALE when their ancestors are inserted.

        If an attempt is made to insert a value into a node that does not exist, a ``NonExistentNodeException`` will be raised, and none of the nodes will be inserted.

        :param name_value_pairs: Each tuple should be a pair (name, value), where name is the name of the node to insert the value into.
        :type name_value_pairs: List of tuples
        """
        LOG.debug('Inserting value into nodes {}'.format(", ".join(str(name) for name, value in name_value_pairs)))

        for name, value in name_value_pairs:
            if name not in self.dag:
                raise NonExistentNodeException('Node {} does not exist'.format(str(name)))

        stale = set()
        computable = set()
        for name, value in name_value_pairs:
            self.dag.node[name][_AN_VALUE] = value
            self.dag.node[name][_AN_STATE] = States.UPTODATE
            stale.update(nx.dag.descendants(self.dag, name))
            computable.update(self.dag.successors(name))
        names = set([name for name, value in name_value_pairs])
        stale.difference_update(names)
        computable.difference_update(names)
        for name in stale:
            self.dag.node[name][_AN_STATE] = States.STALE
        for name in computable:
            self._try_set_computable(name)

    def insert_from(self, other, nodes=None):
        """
        Insert values into another Computation object into this Computation object

        :param other: The computation object to take values from
        :type Computation:
        :param nodes: Only populate the nodes with the names provided in this list. By default, all nodes from the other Computation object that have corresponding nodes in this Computation object will be inserted
        :type nodes: List, default None
        """
        if nodes is None:
            nodes = set(self.dag.nodes())
            nodes.intersection_update(other.dag.nodes())
        name_value_pairs = [(name, other.value(name)) for name in nodes]
        self.insert_many(name_value_pairs)

    def set_stale(self, name):
        """
        Set the state of a node and all its dependencies to STALE

        :param name: Name of the node to set as STALE.
        """
        self.dag.node[name][_AN_STATE] = States.STALE
        for n in nx.dag.descendants(self.dag, name):
            self.dag.node[n][_AN_STATE] = States.STALE
        self._try_set_computable(name)

    def _set_descendents(self, name, state):
        for n in nx.dag.descendants(self.dag, name):
            self.dag.node[n][_AN_STATE] = state

    def _set_uninitialized(self, name):
        n = self.dag.node[name]
        n[_AN_STATE] = States.UNINITIALIZED
        n.pop(_AN_VALUE, None)

    def _set_uptodate(self, name, value):
        node = self.dag.node[name]
        node[_AN_STATE] = States.UPTODATE
        node[_AN_VALUE] = value
        self._set_descendents(name, States.STALE)
        for n in self.dag.successors(name):
            self._try_set_computable(n)

    def _set_error(self, name, error):
        node = self.dag.node[name]
        node[_AN_STATE] = States.ERROR
        node[_AN_VALUE] = error
        self._set_descendents(name, States.STALE)

    def _try_set_computable(self, name):
        if _AN_FUNC in self.dag.node[name]:
            for n in self.dag.predecessors(name):
                if not self.dag.has_node(n):
                    return
                if self.dag.node[n][_AN_STATE] != States.UPTODATE:
                    return
            self.dag.node[name][_AN_STATE] = States.COMPUTABLE

    def _get_parameter_data(self, name):
        for arg, value in six.iteritems(self.dag.node[name][_AN_ARGS]):
            yield _ParameterItem(_ParameterType.ARG, arg, value)
        for param_name, value in six.iteritems(self.dag.node[name][_AN_KWDS]):
            yield _ParameterItem(_ParameterType.KWD, param_name, value)
        for in_node_name in self.dag.predecessors(name):
            param_value = self.dag.node[in_node_name][_AN_VALUE]
            edge = self.dag.edge[in_node_name][name]
            param_type, param_name = edge[_AE_PARAM]
            yield _ParameterItem(param_type, param_name, param_value)

    def _compute_node(self, name, raise_exceptions=False):
        LOG.debug('Computing node {}'.format(str(name)))
        node = self.dag.node[name]
        f = node[_AN_FUNC]
        args, kwds = [], {}
        for param in self._get_parameter_data(name):
            if param.type == _ParameterType.ARG:
                idx = param.name
                while len(args) <= idx:
                    args.append(None)
                args[idx] = param.value
            elif param.type == _ParameterType.KWD:
                kwds[param.name] = param.value
            else:
                raise Exception("Unexpected param type: {}".format(param.type))
        try:
            start_dt = datetime.utcnow()
            value = f(*args, **kwds)
            end_dt = datetime.utcnow()
            delta = (end_dt - start_dt).total_seconds()
            node[_AN_STATE] = States.UPTODATE
            node[_AN_VALUE] = value
            node[_AN_TIMING] = TimingData(start_dt, end_dt, delta)
            self._set_descendents(name, States.STALE)
            for n in self.dag.successors(name):
                self._try_set_computable(n)
        except Exception as e:
            node[_AN_STATE] = States.ERROR
            tb = traceback.format_exc()
            node[_AN_VALUE] = Error(e, tb)
            self._set_descendents(name, States.STALE)
            if raise_exceptions:
                raise

    def _get_calc_nodes(self, name):
        g = nx.DiGraph()
        g.add_nodes_from(self.dag.nodes_iter())
        g.add_edges_from(self.dag.edges_iter())
        for n in nx.ancestors(g, name):
            node = self.dag.node[n]
            state = node[_AN_STATE]
            if state == States.UPTODATE:
                g.remove_node(n)

        ancestors = nx.ancestors(g, name)
        for n in ancestors:
            if state == States.UNINITIALIZED and len(self.dag.predecessors(n)) == 0:
                raise Exception("Cannot compute {} because {} uninitialized".format(name, n))
            if state == States.PLACEHOLDER:
                raise Exception("Cannot compute {} because {} is placeholder".format(name, n))

        ancestors.add(name)
        nodes_sorted = nx.topological_sort(g, ancestors)
        return [n for n in nodes_sorted if n in ancestors]

    def _compute_one(self, name, raise_exceptions):
        calc_nodes = self._get_calc_nodes(name)
        for n in calc_nodes:
            preds = self.dag.predecessors(n)
            if all(self.dag.node[n1][_AN_STATE] == States.UPTODATE for n1 in preds):
                self._compute_node(n, raise_exceptions=raise_exceptions)

    def compute(self, name, raise_exceptions=False):
        """
        Compute a node and all necessary predecessors

        Following the computation, if successful, the target node, and all necessary ancestors that were not already UPTODATE will have been calculated and set to UPTODATE. Any node that did not need to be calculated will not have been recalculated.

        If any nodes raises an exception, then the state of that node will be set to ERROR, and its value set to an object containing the exception object, as well as a traceback. This will not halt the computation, which will proceed as far as it can, until no more nodes that would be required to calculate the target are COMPUTABLE.

        :param name: Name of the node to compute
        :param raise_exceptions: Whether to pass exceptions raised by node computations back to the caller
        :type raise_exceptions: Boolean, default False
        """
        _apply(self._compute_one, name, raise_exceptions=raise_exceptions)

    def _get_computable_nodes_iter(self):
        for n, node in self.dag.nodes_iter(data=True):

            if node[_AN_STATE] == States.COMPUTABLE:
                yield n

    def compute_all(self, raise_exceptions=False):
        """Compute all nodes of a computation that can be computed

        Nodes that are already UPTODATE will not be recalculated. Following the computation, if successful, all nodes will have state UPTODATE, except UNINITIALIZED input nodes and PLACEHOLDER nodes.

        If any nodes raises an exception, then the state of that node will be set to ERROR, and its value set to an object containing the exception object, as well as a traceback. This will not halt the computation, which will proceed as far as it can, until no more nodes are COMPUTABLE.

        :param raise_exceptions: Whether to pass exceptions raised by node computations back to the caller
        :type raise_exceptions: Boolean, default False
        """
        computed = set()
        while True:
            computable = self._get_computable_nodes_iter()
            any_computable = False
            for n in computable:
                if n in computed:
                    raise LoopDetectedException("compute_all is calculating {} for the second time".format(n))
                any_computable = True
                self._compute_node(n, raise_exceptions=raise_exceptions)
                computed.add(n)
            if not any_computable:
                break

    def nodes(self):
        """
        Get a list of nodes in this computation
        :return: List of nodes
        """
        return self.dag.nodes()

    def _state_one(self, name):
        return self.dag.node[name][_AN_STATE]

    def state(self, name):
        """
        Get the state of a node

        This can also be accessed using the attribute-style accessor ``s`` if ``name`` is a valid Python attribute name::

            >>> comp = Computation()
            >>> comp.add_node('foo', value=1)
            >>> comp.state('foo')
            <States.UPTODATE: 4>
            >>> comp.s.foo
            <States.UPTODATE: 4>

        :param name: Name or names of the node to get state for
        :type name: Key or [Keys]
        """
        return _apply(self._state_one, name)

    def _value_one(self, name):
        return self.dag.node[name][_AN_VALUE]

    def value(self, name):
        """
        Get the current value of a node

        This can also be accessed using the attribute-style accessor ``v`` if ``name`` is a valid Python attribute name::

            >>> comp = Computation()
            >>> comp.add_node('foo', value=1)
            >>> comp.value('foo')
            1
            >>> comp.v.foo
            1

        :param name: Name or names of the node to get the value of
        :type name: Key or [Keys]
        """
        return _apply(self._value_one, name)

    def _tag_one(self, name):
        node = self.dag.node[name]
        return node[_AN_TAG]

    def tags(self, name):
        """
        Get the tags associated with a node
        
            >>> comp = Computation()
            >>> comp.add_node('a', tags=['foo', 'bar'])
            >>> comp.t.a
            {'__serialize__', 'bar', 'foo'}
        :param name: Name or names of the node to get the tags of
        :return: 
        """
        return _apply(self._tag_one, name)

    def _get_item_one(self, name):
        node = self.dag.node[name]
        return NodeData(node[_AN_STATE], node[_AN_VALUE])

    def __getitem__(self, name):
        """
        Get the state and current value of a node

        :param name: Name of the node to get the state and value of
        """
        return _apply(self._get_item_one, name)

    def _get_timing_one(self, name):
        node = self.dag.node[name]
        return node.get(_AN_TIMING, None)

    def get_timing(self, name):
        """
        Get the timing information for a node
        
        :param name: Name or names of the node to get the timing information of
        :return: 
        """
        return _apply(self._get_timing_one, name)

    def to_df(self):
        """
        Get a dataframe containing the states and value of all nodes of computation

        ::

            >>> comp = loman.Computation()
            >>> comp.add_node('foo', value=1)
            >>> comp.add_node('bar', value=2)
            >>> comp.to_df()
                           state  value  is_expansion
            bar  States.UPTODATE      2           NaN
            foo  States.UPTODATE      1           NaN
        """
        df = pd.DataFrame(index=nx.topological_sort_recursive(self.dag))
        df[_AN_STATE] = pd.Series(nx.get_node_attributes(self.dag, _AN_STATE))
        df[_AN_VALUE] = pd.Series(nx.get_node_attributes(self.dag, _AN_VALUE))
        df_timing = pd.DataFrame.from_dict(nx.get_node_attributes(self.dag, 'timing'), orient='index')
        df = pd.merge(df, df_timing, left_index=True, right_index=True, how='left')
        return df

    def to_dict(self):
        """
        Get a dictionary containing the values of all nodes of a computation

        ::

            >>> comp = loman.Computation()
            >>> comp.add_node('foo', value=1)
            >>> comp.add_node('bar', value=2)
            >>> comp.to_dict()
            {'bar': 2, 'foo': 1}
        """
        return nx.get_node_attributes(self.dag, _AN_VALUE)

    def _get_inputs_one(self, name):
        args_dict = {}
        kwds = []
        max_arg_index = -1
        for input_node in self.dag.predecessors(name):
            input_edge = self.dag.edge[input_node][name]
            input_type, input_param = input_edge[_AE_PARAM]
            if input_type == _ParameterType.ARG:
                idx = input_param
                max_arg_index = max(max_arg_index, idx)
                args_dict[idx] = input_node
            elif input_type == _ParameterType.KWD:
                kwds.append(input_node)
        if max_arg_index >= 0:
            args = [None] * (max_arg_index + 1)
            for idx, input_node in six.iteritems(args_dict):
                args[idx] = input_node
            return args + kwds
        else:
            return kwds


    def get_inputs(self, name):
        """
        Get a list of the inputs for a node or set of nodes
        
        :param name: Name or names of nodes to get inputs for 
        :return: If name is scalar, return a list of upstream nodes used as input. If name is a list, return a list of list of inputs.
        """
        return _apply(self._get_inputs_one, name)

    def write_dill(self, file_):
        """
        Serialize a computation to a file or file-like object

        :param file_: If string, writes to a file
        :type file_: File-like object, or string
        """
        node_serialize = nx.get_node_attributes(self.dag, _AN_TAG)
        if all(serialize for name, serialize in six.iteritems(node_serialize)):
            obj = self
        else:
            obj = self.copy()
            for name, tags in six.iteritems(node_serialize):
                if _T_SERIALIZE not in tags:
                    obj._set_uninitialized(name)

        if isinstance(file_, six.string_types):
            with open(file_, 'wb') as f:
                dill.dump(obj, f)
        else:
            dill.dump(obj, file_)

    @staticmethod
    def read_dill(file_):
        """
        Deserialize a computation from a file or file-like object

        :param file_: If string, writes to a file
        :type file_: File-like object, or string
        """
        if isinstance(file_, six.string_types):
            with open(file_, 'rb') as f:
                return dill.load(f)
        else:
            return dill.load(file_)

    def copy(self):
        """
        Create a copy of a computation

        The copy is shallow. Any values in the new Computation's DAG will be the same object as this Computation's DAG. As new objects will be created by any further computations, this should not be an issue.

        :rtype: Computation
        """
        obj = Computation()
        obj.dag = nx.DiGraph(self.dag)
        return obj

    def add_named_tuple_expansion(self, name, namedtuple_type, group=None):
        """
        Automatically add nodes to extract each element of a named tuple type

        It is often convenient for a calculation to return multiple values, and it is polite to do this a namedtuple rather than a regular tuple, so that later users have same name to identify elements of the tuple. It can also help make a computation clearer if a downstream computation depends on one element of such a tuple, rather than the entire tuple. This does not affect the computation per se, but it does make the intention clearer.

        To avoid having to create many boiler-plate node definitions to expand namedtuples, the ``add_named_tuple_expansion`` method automatically creates new nodes for each element of a tuple. The convention is that an element called 'element', in a node called 'node' will be expanded into a new node called 'node.element', and that this will be applied for each element.

        Example::

            >>> from collections import namedtuple
            >>> Coordinate = namedtuple('Coordinate', ['x', 'y'])
            >>> comp = Computation()
            >>> comp.add_node('c', value=Coordinate(1, 2))
            >>> comp.add_named_tuple_expansion('c', Coordinate)
            >>> comp.compute_all()
            >>> comp.value('c.x')
            1
            >>> comp.value('c.y')
            2

        :param name: Node to cera
        :param namedtuple_type: Expected type of the node
        :type namedtuple_type: namedtuple class
        """
        def make_f(field):
            def get_field_value(tuple):
                return getattr(tuple, field)
            return get_field_value
        for field in namedtuple_type._fields:
            node_name = "{}.{}".format(name, field)
            self.add_node(node_name, make_f(field), kwds={'tuple': name}, group=group)
            self.set_tag(node_name, _T_EXPANSION)

    def add_map_node(self, result_node, input_node, subgraph, subgraph_input_node, subgraph_output_node):
        """
        Apply a graph to each element of iterable

        In turn, each element in the ``input_node`` of this graph will be inserted in turn into the subgraph's ``subgraph_input_node``, then the subgraph's ``subgraph_output_node`` calculated. The resultant list, with an element or each element in ``input_node``, will be inserted into ``result_node`` of this graph. In this way ``add_map_node`` is similar to ``map`` in functional programming.

        :param result_node: The node to place a list of results in **this** graph
        :param input_node: The node to get a list input values from **this** graph
        :param subgraph: The graph to use to perform calculation for each element
        :param subgraph_input_node: The node in **subgraph** to insert each element in turn
        :param subgraph_output_node: The node in **subgraph** to read the result for each element
        """
        def f(xs):
            results = []
            is_error = False
            for x in xs:
                subgraph.insert(subgraph_input_node, x)
                subgraph.compute(subgraph_output_node)
                if subgraph.state(subgraph_output_node) == States.UPTODATE:
                    results.append(subgraph.value(subgraph_output_node))
                else:
                    is_error = True
                    results.append(subgraph.copy())
            if is_error:
                raise MapException("Unable to calculate {}".format(result_node), results)
            return results
        self.add_node(result_node, f, kwds={'xs': input_node})

    def _repr_svg_(self):
        return self.to_pydot().create_svg().decode('utf-8')

    def to_pydot(self, colors='state', cmap=None, graph_attr=None, node_attr=None, edge_attr=None, show_expansion=False):
        struct_dag = self.dag.copy()
        if not show_expansion:
            hide_nodes = set(struct_dag.nodes_iter())
            for name1, name2 in struct_dag.edges_iter():
                if not show_expansion and _T_EXPANSION in self.tags(name2):
                    continue
                hide_nodes.discard(name1)
                hide_nodes.discard(name2)
            _contract_node(struct_dag, hide_nodes)
        viz_dag = _create_viz_dag(struct_dag, colors=colors, cmap=cmap)
        viz_dot = _to_pydot(viz_dag, graph_attr, node_attr, edge_attr)
        return viz_dot

    def draw(self, colors='state', cmap=None, graph_attr=None, node_attr=None, edge_attr=None, show_expansion=False):
        """
        Draw a computation's current state using the GraphViz utility

        :param graph_attr: Mapping of (attribute, value) pairs for the graph. For example ``graph_attr={'size': '"10,8"'}`` can control the size of the output graph
        :param node_attr: Mapping of (attribute, value) pairs set for all nodes.
        :param edge_attr: Mapping of (attribute, value) pairs set for all edges.
        :param show_expansion: Whether to show expansion nodes (i.e. named tuple expansion nodes) if they are not referenced by other nodes
        """
        d = self.to_pydot(colors=colors, cmap=cmap, graph_attr=graph_attr, node_attr=node_attr, edge_attr=edge_attr,
                          show_expansion=show_expansion)

        def repr_svg(self):
            return self.create_svg().decode('utf-8')

        d._repr_svg_ = types.MethodType(repr_svg, d)
        return d

    def view(self, colors='state', cmap=None):
        d = self.to_pydot(colors=colors, cmap=cmap)
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            f.write(d.create_pdf())
            os.startfile(f.name)


def _apply(f, xs, *args, **kwds):
    if isinstance(xs, types.GeneratorType):
        return (f(x, *args, **kwds) for x in xs)
    if isinstance(xs, list):
        return [f(x, *args, **kwds) for x in xs]
    return f(xs, *args, **kwds)


def _as_iterable(xs):
    if isinstance(xs, (types.GeneratorType, list, set)):
        return xs
    return (xs,)


def _apply_n(f, *xs, **kwds):
    for p in itertools.product(*[_as_iterable(x) for x in xs]):
        f(*p, **kwds)


def _contract_node_one(g, n):
    for p in g.predecessors(n):
        for s in g.successors(n):
            g.add_edge(p, s)
    g.remove_node(n)


def _contract_node(g, ns):
    _apply_n(functools.partial(_contract_node_one, g), ns)


def _create_viz_dag(comp_dag, colors='state', cmap=None):
    colors = colors.lower()
    if colors == 'state':
        if cmap is None:
            cmap = _state_colors
    elif colors == 'timing':
        if cmap is None:
            cmap = mpl.colors.LinearSegmentedColormap.from_list('blend', ['#15b01a', '#ffff14', '#e50000'])
        timings = nx.get_node_attributes(comp_dag, _AN_TIMING)
        max_duration = max(timing.duration for timing in six.itervalues(timings) if hasattr(timing, 'duration'))
        min_duration = min(timing.duration for timing in six.itervalues(timings) if hasattr(timing, 'duration'))
    else:
        raise ValueError('{} is not a valid loman colors parameter for visualization'.format(colors))

    viz_dag = nx.DiGraph()
    node_index_map = {}
    for i, (name, data) in enumerate(comp_dag.nodes_iter(data=True)):
        short_name = "n{}".format(i)
        attr_dict = {
            'label': name,
            'style': 'filled',
            '_group': data.get(_AN_GROUP)
        }

        if colors == 'state':
            attr_dict['fillcolor'] = cmap[data.get(_AN_STATE, None)]
        elif colors == 'timing':
            timing_data = data.get(_AN_TIMING)
            if timing_data is None:
                col = '#FFFFFF'
            else:
                duration = timing_data.duration
                norm_duration = (duration - min_duration) / (max_duration - min_duration)
                col = mpl.colors.rgb2hex(cmap(norm_duration))
            attr_dict['fillcolor'] = col

        viz_dag.add_node(short_name, attr_dict)
        node_index_map[name] = short_name
    for name1, name2 in comp_dag.edges_iter():
        short_name_1 = node_index_map[name1]
        short_name_2 = node_index_map[name2]

        group1 = comp_dag.node[name1].get(_AN_GROUP)
        group2 = comp_dag.node[name2].get(_AN_GROUP)
        group = group1 if group1 == group2 else None

        attr_dict = {'_group': group}

        viz_dag.add_edge(short_name_1, short_name_2, attr_dict)
    return viz_dag


def _to_pydot(viz_dag, graph_attr=None, node_attr=None, edge_attr=None):
    node_groups = {}
    for name, data in viz_dag.nodes_iter(data=True):
        group = data.get('_group')
        node_groups.setdefault(group, []).append(name)

    edge_groups = {}
    for name1, name2, data in viz_dag.edges_iter(data=True):
        group = data.get('_group')
        edge_groups.setdefault(group, []).append((name1, name2))

    viz_dot = pydotplus.Dot()

    if graph_attr is not None:
        for k, v in six.iteritems(graph_attr):
            viz_dot.set(k, v)

    if node_attr is not None:
        viz_dot.set_node_defaults(**node_attr)

    if edge_attr is not None:
        viz_dot.set_edge_defaults(**edge_attr)

    for group, names in six.iteritems(node_groups):
        if group is None:
            continue
        c = pydotplus.Subgraph('cluster_' + str(group))

        for name in names:
            node = pydotplus.Node(name)
            for k, v in six.iteritems(viz_dag.node[name]):
                if not k.startswith("_"):
                    node.set(k, v)
            c.add_node(node)

        for name1, name2 in edge_groups.get(group, []):
            edge = pydotplus.Edge(name1, name2)
            c.add_edge(edge)

        c.obj_dict['label'] = str(group)
        viz_dot.add_subgraph(c)

    for name in node_groups.get(None, []):
        node = pydotplus.Node(name)
        for k, v in six.iteritems(viz_dag.node[name]):
            if not k.startswith("_"):
                node.set(k, v)
        viz_dot.add_node(node)

    for name1, name2 in edge_groups.get(None, []):
        edge = pydotplus.Edge(name1, name2)
        viz_dot.add_edge(edge)

    return viz_dot
