# Authors:
#   Jason Gerard DeRose <jderose@redhat.com>
#
# Copyright (C) 2008  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; version 2 only
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA 02111-1307 USA

"""
Base classes for all front-end plugins.
"""

import re
import inspect
import plugable
from plugable import lock, check_name
import errors
from errors import check_type, check_isinstance, raise_TypeError
import ipa_types


RULE_FLAG = 'validation_rule'

def rule(obj):
    assert not hasattr(obj, RULE_FLAG)
    setattr(obj, RULE_FLAG, True)
    return obj

def is_rule(obj):
    return callable(obj) and getattr(obj, RULE_FLAG, False) is True


class DefaultFrom(plugable.ReadOnly):
    """
    Derives a default for one value using other supplied values.

    Here is an example that constructs a user's initials from his first
    and last name:

    >>> df = DefaultFrom(lambda f, l: f[0] + l[0], 'first', 'last')
    >>> df(first='John', last='Doe') # Both keys
    'JD'
    >>> df() is None # Returns None if any key is missing
    True
    >>> df(first='John', middle='Q') is None # Still returns None
    True
    """
    def __init__(self, callback, *keys):
        """
        :param callback: The callable to call when all ``keys`` are present.
        :param keys: The keys used to map from keyword to position arguments.
        """
        assert callable(callback), 'not a callable: %r' % callback
        assert len(keys) > 0, 'must have at least one key'
        for key in keys:
            assert type(key) is str, 'not an str: %r' % key
        self.callback = callback
        self.keys = keys
        lock(self)

    def __call__(self, **kw):
        """
        If all keys are present, calls the callback; otherwise returns None.

        :param kw: The keyword arguments.
        """
        vals = tuple(kw.get(k, None) for k in self.keys)
        if None in vals:
            return None
        try:
            return self.callback(*vals)
        except Exception:
            return None


class Param(plugable.ReadOnly):
    def __init__(self, name, type_,
            doc='',
            required=False,
            multivalue=False,
            default=None,
            default_from=None,
            rules=tuple(),
            normalize=None):
        self.name = check_name(name)
        self.doc = check_type(doc, str, 'doc')
        self.type = check_isinstance(type_, ipa_types.Type, 'type_')
        self.required = check_type(required, bool, 'required')
        self.multivalue = check_type(multivalue, bool, 'multivalue')
        self.default = default
        self.default_from = check_type(default_from,
            DefaultFrom, 'default_from', allow_none=True)
        self.__normalize = normalize
        self.rules = (type_.validate,) + rules
        lock(self)

    def __dispatch(self, value, scalar):
        if self.multivalue:
            if type(value) in (tuple, list):
                if len(value) == 0:
                    return None
                return tuple(
                    scalar(v, i) for (i, v) in enumerate(value)
                )
            return (scalar(value, 0),) # tuple
        return scalar(value)

    def __normalize_scalar(self, value, index=None):
        if not isinstance(value, basestring):
            return value
        try:
            return self.__normalize(value)
        except StandardError:
            return value

    def normalize(self, value):
        """
        Normalize ``value`` using normalize callback.

        If this `Param` instance does not have a normalize callback,
        ``value`` is returned unchanged.

        If this `Param` instance has a normalize callback and ``value`` is
        a basestring, the normalize callback is called and its return value
        is returned.

        If ``value`` is not a basestring, or if an exception is caught
        when calling the normalize callback, ``value`` is returned unchanged.

        :param value: A proposed value for this parameter.
        """
        if self.__normalize is None:
            return value
        return self.__dispatch(value, self.__normalize_scalar)

    def __convert_scalar(self, value, index=None):
        if value is None:
            raise TypeError('value cannot be None')
        converted = self.type(value)
        if converted is None:
            raise errors.ConversionError(
                self.name, value, self.type, index=index
            )
        return converted

    def convert(self, value):
        return self.__dispatch(value, self.__convert_scalar)



    def __validate_scalar(self, value, index=None):
        if type(value) is not self.type.type:
            raise_TypeError(value, self.type.type, 'value')
        for rule in self.rules:
            error = rule(value)
            if error is not None:
                raise errors.RuleError(
                    self.name, value, error, rule, index=index
                )

    def validate(self, value):
        if self.multivalue:
            if type(value) is not tuple:
                raise_TypeError(value, tuple, 'value')
            for (i, v) in enumerate(value):
                self.__validate_scalar(v, i)
        else:
            self.__validate_scalar(value)

    def get_default(self, **kw):
        if self.default_from is not None:
            default = self.default_from(**kw)
            if default is not None:
                try:
                    return self.convert(self.normalize(default))
                except errors.ValidationError:
                    return None
        return self.default

    def get_values(self):
        if self.type.name in ('Enum', 'CallbackEnum'):
            return self.type.values
        return tuple()

    def __call__(self, value, **kw):
        if value in ('', tuple(), []):
            value = None
        if value is None:
            value = self.get_default(**kw)
        if value is None:
            if self.required:
                raise errors.RequirementError(self.name)
            return None
        else:
            value = self.convert(self.normalize(value))
            self.validate(value)
            return value

    def __repr__(self):
        return '%s(%r, %s())' % (
            self.__class__.__name__,
            self.name,
            self.type.name,
        )


def create_param(spec):
    """
    Create a `Param` instance from a param spec string.

    If ``spec`` is a `Param` instance, ``spec`` is returned unchanged.

    If ``spec`` is an str instance, then ``spec`` is parsed and an
    appropriate `Param` instance is created and returned.

    The spec string determines the param name, whether the param is required,
    and whether the param is multivalue according the following syntax:

    name => required=True, multivalue=False
    name? => required=False, multivalue=False
    name+ => required=True, multivalue=True
    name* => required=False, multivalue=True

    :param spec: A spec string or a `Param` instance.
    """
    if type(spec) is Param:
        return spec
    if type(spec) is not str:
        raise TypeError(
            'create_param() takes %r or %r; got %r' % (str, Param, spec)
        )
    if spec.endswith('?'):
        kw = dict(required=False, multivalue=False)
        name = spec[:-1]
    elif spec.endswith('*'):
        kw = dict(required=False, multivalue=True)
        name = spec[:-1]
    elif spec.endswith('+'):
        kw = dict(required=True, multivalue=True)
        name = spec[:-1]
    else:
        kw = dict(required=True, multivalue=False)
        name = spec
    return Param(name, ipa_types.Unicode(), **kw)


class Command(plugable.Plugin):
    __public__ = frozenset((
        'get_default',
        'convert',
        'normalize',
        'validate',
        'execute',
        '__call__',
        'args',
        'options',
        'params',
        'args_to_kw',
        'kw_to_args',
    ))
    takes_options = tuple()
    takes_args = tuple()
    args = None
    options = None
    params = None
    can_forward = True

    def finalize(self):
        self.args = plugable.NameSpace(self.__create_args(), sort=False)
        if len(self.args) == 0 or not self.args[-1].multivalue:
            self.max_args = len(self.args)
        else:
            self.max_args = None
        self.options = plugable.NameSpace(self.__create_options(), sort=False)
        self.params = plugable.NameSpace(
            tuple(self.args()) + tuple(self.options()), sort=False
        )
        super(Command, self).finalize()

    def get_args(self):
        return self.takes_args

    def get_options(self):
        return self.takes_options

    def __create_args(self):
        optional = False
        multivalue = False
        for arg in self.get_args():
            arg = create_param(arg)
            if optional and arg.required:
                raise ValueError(
                    '%s: required argument after optional' % arg.name
                )
            if multivalue:
                raise ValueError(
                    '%s: only final argument can be multivalue' % arg.name
                )
            if not arg.required:
                optional = True
            if arg.multivalue:
                multivalue = True
            yield arg

    def __create_options(self):
        for option in self.get_options():
            yield create_param(option)

    def convert(self, **kw):
        return dict(
            (k, self.params[k].convert(v)) for (k, v) in kw.iteritems()
        )

    def normalize(self, **kw):
        return dict(
            (k, self.params[k].normalize(v)) for (k, v) in kw.iteritems()
        )

    def __get_default_iter(self, kw):
        for param in self.params():
            if param.name not in kw:
                yield (param.name, param.get_default(**kw))

    def get_default(self, **kw):
        return dict(self.__get_default_iter(kw))

    def validate(self, **kw):
        for param in self.params():
            value = kw.get(param.name, None)
            if value is not None:
                param.validate(value)
            elif param.required:
                raise errors.RequirementError(param.name)

    def execute(self, *args, **kw):
        print '%s.execute():' % self.name
        print '  args =', args
        print '  kw =', kw

    def forward(self, *args, **kw):
        print '%s.execute():' % self.name
        print '  args =', args
        print '  kw =', kw

    def __call__(self, *args, **kw):
        if len(args) > 0:
            arg_kw = self.args_to_kw(*args)
            assert set(arg_kw).intersection(kw) == set()
            kw.update(arg_kw)
        kw = self.normalize(**kw)
        kw = self.convert(**kw)
        kw.update(self.get_default(**kw))
        self.validate(**kw)
        args = tuple(kw.pop(name) for name in self.args)
        return self.run(*args, **kw)

    def run(self, *args, **kw):
        if self.api.env.in_server_context:
            target = self.execute
        else:
            target = self.forward
        object.__setattr__(self, 'run', target)
        return target(*args, **kw)

    def args_to_kw(self, *values):
        if self.max_args is not None and len(values) > self.max_args:
            if self.max_args == 0:
                raise errors.ArgumentError(self, 'takes no arguments')
            if self.max_args == 1:
                raise errors.ArgumentError(self, 'takes at most 1 argument')
            raise errors.ArgumentError(self,
                'takes at most %d arguments' % len(self.args)
            )
        return dict(self.__args_to_kw_iter(values))

    def __args_to_kw_iter(self, values):
        multivalue = False
        for (i, arg) in enumerate(self.args()):
            assert not multivalue
            if len(values) > i:
                if arg.multivalue:
                    multivalue = True
                    yield (arg.name, values[i:])
                else:
                    yield (arg.name, values[i])
            else:
                break

    def kw_to_args(self, **kw):
        return tuple(kw.get(name, None) for name in self.args)


class Object(plugable.Plugin):
    __public__ = frozenset((
        'Method',
        'Property',
        'params'
    ))
    __Method = None
    __Property = None
    takes_params = tuple()

    def __init__(self):
        self.params = plugable.NameSpace(
            (create_param(p) for p in self.takes_params), sort=False
        )

    def __create_params(self):
        for param in self.takes_params:
            yield create_param(param)

    def __get_Method(self):
        return self.__Method
    Method = property(__get_Method)

    def __get_Property(self):
        return self.__Property
    Property = property(__get_Property)

    def set_api(self, api):
        super(Object, self).set_api(api)
        self.__Method = self.__create_namespace('Method')
        self.__Property = self.__create_namespace('Property')

    def __create_namespace(self, name):
        return plugable.NameSpace(self.__filter_members(name))

    def __filter_members(self, name):
        namespace = getattr(self.api, name)
        assert type(namespace) is plugable.NameSpace
        for proxy in namespace(): # Equivalent to dict.itervalues()
            if proxy.obj_name == self.name:
                yield proxy.__clone__('attr_name')


class Attribute(plugable.Plugin):
    __public__ = frozenset((
        'obj',
        'obj_name',
    ))
    __obj = None

    def __init__(self):
        m = re.match(
            '^([a-z][a-z0-9]+)_([a-z][a-z0-9]+)$',
            self.__class__.__name__
        )
        assert m
        self.__obj_name = m.group(1)
        self.__attr_name = m.group(2)

    def __get_obj_name(self):
        return self.__obj_name
    obj_name = property(__get_obj_name)

    def __get_attr_name(self):
        return self.__attr_name
    attr_name = property(__get_attr_name)

    def __get_obj(self):
        """
        Returns the obj instance this attribute is associated with, or None
        if no association has been set.
        """
        return self.__obj
    obj = property(__get_obj)

    def set_api(self, api):
        self.__obj = api.Object[self.obj_name]
        super(Attribute, self).set_api(api)


class Method(Attribute, Command):
    __public__ = Attribute.__public__.union(Command.__public__)

    def __init__(self):
        Attribute.__init__(self)
        Command.__init__(self)

    def get_options(self):
        for option in self.takes_options:
            yield option
        if self.obj is not None and self.obj.Property is not None:
            def get_key(p):
                if p.param.required:
                    if p.param.default_from is None:
                        return 0
                    return 1
                return 2
            for prop in sorted(self.obj.Property(), key=get_key):
                yield prop.param


class Property(Attribute):
    __public__ = frozenset((
        'rules',
        'param',
        'type',
    )).union(Attribute.__public__)

    type = ipa_types.Unicode()
    required = False
    multivalue = False
    default = None
    default_from = None
    normalize = None

    def __init__(self):
        super(Property, self).__init__()
        self.rules = tuple(sorted(
            self.__rules_iter(),
            key=lambda f: getattr(f, '__name__'),
        ))
        self.param = Param(self.attr_name, self.type,
            doc=self.doc,
            required=self.required,
            multivalue=self.multivalue,
            default=self.default,
            default_from=self.default_from,
            rules=self.rules,
            normalize=self.normalize,
        )

    def __rules_iter(self):
        """
        Iterates through the attributes in this instance to retrieve the
        methods implementing validation rules.
        """
        for name in dir(self.__class__):
            if name.startswith('_'):
                continue
            base_attr = getattr(self.__class__, name)
            if is_rule(base_attr):
                attr = getattr(self, name)
                if is_rule(attr):
                    yield attr


class Application(Command):
    """
    Base class for commands register by an external application.

    Special commands that only apply to a particular application built atop
    `ipalib` should subclass from ``Application``.

    Because ``Application`` subclasses from `Command`, plugins that subclass
    from ``Application`` with be available in both the ``api.Command`` and
    ``api.Application`` namespaces.
    """

    __public__ = frozenset((
        'application',
        'set_application'
    )).union(Command.__public__)
    __application = None

    def __get_application(self):
        """
        Returns external ``application`` object.
        """
        return self.__application
    application = property(__get_application)

    def set_application(self, application):
        """
        Sets the external application object to ``application``.
        """
        if self.__application is not None:
            raise AttributeError(
                '%s.application can only be set once' % self.name
            )
        if application is None:
            raise TypeError(
                '%s.application cannot be None' % self.name
            )
        object.__setattr__(self, '_Application__application', application)
        assert self.application is application
