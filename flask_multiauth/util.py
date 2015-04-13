# This file is part of Flask-MultiAuth.
# Copyright (C) 2015 CERN
#
# Flask-MultiAuth is free software; you can redistribute it
# and/or modify it under the terms of the Revised BSD License.

from __future__ import unicode_literals

from functools import wraps
from inspect import isclass, getmro
from pkg_resources import iter_entry_points

from flask import current_app

from flask_multiauth._compat import iteritems, string_types, viewkeys, itervalues
from flask_multiauth.exceptions import MultiAuthException


def get_canonical_provider_map(provider_map):
    """Converts the configured provider map to a canonical form"""
    canonical = {}
    for auth_provider_name, identity_providers in iteritems(provider_map):
        if not isinstance(identity_providers, (list, tuple, set)):
            identity_providers = [identity_providers]
        identity_providers = tuple({'identity_provider': p} if isinstance(p, string_types) else p
                                   for p in identity_providers)
        canonical[auth_provider_name] = identity_providers
    return canonical


def get_state(app=None):
    """Gets the application-specific multiauth data.

    :param app: The Flask application. Defaults to the current app.
    :rtype: flask_multiauth.core._MultiAuthState
    """
    if app is None:
        app = current_app
    assert 'multiauth' in app.extensions, \
        'The multiauth extension was not registered to the current application. ' \
        'Please make sure to call init_app() first.'
    return app.extensions['multiauth']


def login_view(func):
    """Decorates a Flask view function as an authentication view.

    This catches multiauth-related exceptions and flashes a message and
    redirects back to the login page.
    """
    @wraps(func)
    def decorator(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except MultiAuthException as e:
            return get_state().multiauth.handle_auth_error(e, True)

    return decorator


def convert_data(data, mapping, keys=None):
    """Converts a data dict based on a mapping and a key list.

    The result will have all the keys listed in `keys` with values
    coming either from `data` (using the key mapping defined in
    `mapping`) or ``None`` in case the key is not present. If `keys`
    is ``None``, all keys from `data` will be used unless they are
    mapped to a different key in `mapping`.

    :param data: A dict containing data.
    :param mapping: A dict containing the mapping between the `data`
                    dict and the result dict. Each key in the dict will
                    be used as a key in the returned dict and each
                    value will be used as the key to get the value from
                    `data`.
    :param keys: A list containing the keys that should be preserved in
                 the returned dict. If it's ``None``, all items are
                 returned.
    :return: A dict based on `data`, `mapping` and `keys`.
    """
    mapped_keys = set(mapping.values())
    result = {key: value for key, value in iteritems(data) if key not in mapped_keys}
    result.update((target_key, data.get(source_key)) for target_key, source_key in iteritems(mapping))
    if keys is not None:
        keys = set(keys)
        result = {key: value for key, value in iteritems(result) if key in keys}
        result.update({key: None for key in keys - set(result)})
    return result


def get_provider_base(cls):
    """Returns the base class of a provider class.

    :param cls: A subclass of either :class:`.AuthProvider` or
                :class:`.IdentityProvider`.
    :return: :class:`.AuthProvider` or :class:`.IdentityProvider`
    """
    from flask_multiauth.auth import AuthProvider
    from flask_multiauth.identity import IdentityProvider
    if issubclass(cls, AuthProvider) and issubclass(cls, IdentityProvider):
        raise TypeError('Class inherits from both AuthProvider and IdentityProvider: ' + cls.__name__)
    elif issubclass(cls, AuthProvider):
        return AuthProvider
    elif issubclass(cls, IdentityProvider):
        return IdentityProvider
    else:
        raise TypeError('Class is neither an auth nor an identity provider: ' + cls.__name__)


def resolve_provider_type(base, type_, registry=None):
    """Resolves a provider type to its class

    :param base: The base class of the provider
    :param type_: The type of the provider. Can be a subclass of
                  `base` or the identifier of a registered type.
    :param registry: A dict containing registered providers. This
                     complements the entrypoint-based lookup. Any
                     provider type defined in this dict takes priority
                     over an entrypoint-based one with the same name.
    :return: The type's class, which is a subclass of `base`.
    """
    if isclass(type_):
        if not issubclass(type_, base):
            raise TypeError('Received a class {} which is not a subclass of {}'.format(type_, base))
        return type_

    if registry is not None and type_ in registry:
        cls = registry[type_]
    else:
        entry_points = list(iter_entry_points(base._entry_point, type_))
        if not entry_points:
            raise ValueError('Unknown type: ' + type_)
        elif len(entry_points) != 1:
            raise RuntimeError('Type {} is not unique. Defined in {}'.format(
                type_, ', '.join(ep.module_name for ep in entry_points)))
        entry_point = entry_points[0]
        cls = entry_point.load()
    if not issubclass(cls, base):
        raise TypeError('Found a class {} which is not a subclass of {}'.format(cls, base))
    return cls


def validate_provider_map(state):
    """Validates the provider map

    :param state: The :class:`._MultiAuthState` instance
    """
    invalid_keys = viewkeys(state.auth_providers) - viewkeys(state.provider_map)
    if invalid_keys:
        raise ValueError('Auth providers not linked to identity providers: ' + ', '.join(invalid_keys))
    targeted_providers = {p['identity_provider'] for providers in itervalues(state.provider_map) for p in providers}
    invalid_keys = targeted_providers - viewkeys(state.identity_providers)
    if invalid_keys:
        raise ValueError('Broken identity provider links: ' + ', '.join(invalid_keys))


class classproperty(property):
    """Like a :class:`property`, but for a class

    Usage::

        class Foo(object):
            @classproperty
            @classmethod
            def foo(cls):
                return 'bar'
    """
    def __get__(self, obj, type=None):
        return self.fget.__get__(None, type)()


class SupportsMeta(type):
    """
    Metaclass that requires/prohibits methods to be overridden
    depending on class attributes.

    The class using this metaclass must have a `__support_attrs__`
    attribute containing a dict mapping attribute names to method
    names (or lists of method names) which must be overridden if the
    attribute is True and may not mbe overridden if it isn't.

    Instead of a string key the dict may also contain a tuple returned
    from :meth:`callable`.
    """
    def __new__(mcs, name, bases, dct):
        cls = type.__new__(mcs, name, bases, dct)
        base = next((x for x in reversed(getmro(cls)) if type(x) is mcs and x is not cls), None)
        if base is None:
            return cls
        for attr, methods in iteritems(base.__support_attrs__):
            if isinstance(methods, string_types):
                methods = methods,
            if isinstance(attr, tuple):
                supported = attr[0](cls)
                message = attr[1]
            else:
                supported = getattr(cls, attr, getattr(base, attr))
                message = '{} is True'.format(attr)
            for method in methods:
                is_overridden = (getattr(base, method) != getattr(cls, method))
                if not supported and is_overridden:
                    raise TypeError('{} cannot override {} unless {}'.format(name, method, message))
                elif supported and not is_overridden:
                    raise TypeError('{} must override {} if {}'.format(name, method, message))
        return cls

    @staticmethod
    def callable(func, message):
        """Returns an object suitable for more complex

        :param func: A callable that is invoked with the dict of the
                     newly created object
        :param message: The message to show in case of a failure.
        """
        return func, message
