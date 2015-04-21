# This file is part of Flask-MultiAuth.
# Copyright (C) 2015 CERN
#
# Flask-MultiAuth is free software; you can redistribute it
# and/or modify it under the terms of the Revised BSD License.

from __future__ import absolute_import

from collections import namedtuple
from contextlib import contextmanager
from functools import wraps
from urlparse import urlparse
from warnings import warn

import ldap
from flask import appcontext_tearing_down, g, has_app_context
from ldap.controls import SimplePagedResultsControl
from ldap.filter import filter_format

from flask_multiauth._compat import iteritems, itervalues, text_type
from flask_multiauth.exceptions import MultiAuthException
from flask_multiauth.providers.ldap.exceptions import LDAPServerError
from flask_multiauth.providers.ldap.globals import _ldap_ctx_stack, current_ldap
from flask_multiauth.util import convert_app_data

#: A context holding the LDAP connection and the LDAP provider settings.
LDAPContext = namedtuple('LDAPContext', ('connection', 'settings'))


@appcontext_tearing_down.connect
def _clear_ldap_cache(*args, **kwargs):
    for conn in itervalues(g.get('_multiauth_ldap_connections', {})):
        try:
            conn.unbind_s()
        except ldap.LDAPError:
            # That's ugly but we couldn't care less about a failure while disconnecting
            pass


def _get_ldap_cache():
    """Returns the cache dictionary for ldap contexts"""
    if not has_app_context():
        return {}
    try:
        return g._multiauth_ldap_connections
    except AttributeError:
        g._multiauth_ldap_connections = cache = {}
        return cache


def _cache_ldap_connection(fn):
    """Decorator to cache the ldap connection"""
    conn_keys = {'uri', 'bind_dn', 'bind_password', 'tls', 'starttls'}

    @wraps(fn)
    def wrapper(settings):
        cache = _get_ldap_cache()
        key = frozenset((k, hash(v)) for k, v in iteritems(settings) if k in conn_keys)
        ldap_ctx = cache.get(key)
        if ldap_ctx is None:
            cache[key] = ldap_ctx = fn(settings)
        return ldap_ctx

    return wrapper


@contextmanager
def ldap_context(settings):
    """Establishes an LDAP session context.

    Establishes a connection to the LDAP server from the `uri` in the
    ``settings`` and makes the context available in ``current_ldap``.

    Yields a namedtuple containing the connection to the server and the
    provider settings.

    :param settings: dict -- The settings for a LDAP provider.
    """
    try:
        connection = ldap_connect(settings)
        ldap_ctx = LDAPContext(connection=connection, settings=settings)
        _ldap_ctx_stack.push(ldap_ctx)
        try:
            yield ldap_ctx
        finally:
            assert _ldap_ctx_stack.pop() is ldap_ctx, "Popped wrong LDAP context"
    except ldap.SERVER_DOWN:
        raise MultiAuthException("The LDAP server is unreachable")
    except ldap.INVALID_CREDENTIALS:
        raise ValueError("Invalid bind credentials")
    except ldap.SIZELIMIT_EXCEEDED:
        raise MultiAuthException("Size limit exceeded (try setting a smaller page size)")
    except ldap.TIMELIMIT_EXCEEDED:
        raise MultiAuthException("The time limit for the operation has been exceeded.")
    except ldap.TIMEOUT:
        raise MultiAuthException("The operation timed out.")
    except ldap.FILTER_ERROR:
        raise ValueError("The filter supplied to the operation is invalid. "
                         "(This is most likely due to a bad user or group filter.")
    # TODO: handle a MultiAuth time out exception


@_cache_ldap_connection
def ldap_connect(settings):
    """Establishes an LDAP connection.

    Establishes a connection to the LDAP server from the `uri` in the
    ``settings``.

    To establish a connection, the settings must be specified:
     - ``uri``: valid URI which points to a LDAP server,
     - ``bind_dn``: `dn` used to initially bind every LDAP connection
     - ``bind_password``" password used for the initial bind
     - ``tls``: ``True`` if the connection should use TLS encryption
     - ``starttls``: ``True`` to negotiate TLS with the server

    `Note`: ``starttls`` is ignored if the URI uses LDAPS and ``tls`` is
    set to ``True``.

    This function re-uses an existing LDAP connection if there is one
    available in the application context.

    :param settings: dict -- The settings for a LDAP provider.
    :return: The ldap connection.
    """
    uri_info = urlparse(settings['uri'])
    credentials = (settings['bind_dn'], settings['bind_password'])
    ldap_connection = ldap.initialize(settings['uri'])
    ldap_connection.protocol_version = ldap.VERSION3
    ldap_connection.set_option(ldap.OPT_REFERRALS, 0)
    ldap_connection.set_option(ldap.OPT_X_TLS, ldap.OPT_X_TLS_DEMAND if settings['tls'] else ldap.OPT_X_TLS_NEVER)
    if uri_info.scheme != 'ldaps' and settings['starttls']:
        ldap_connection.start_tls_s()
    elif settings['starttls']:
        warn("Unable to start TLS, LDAP connection already secured over SSL (LDAPS)")
    # TODO: allow anonymous bind
    ldap_connection.simple_bind_s(*credentials)
    return ldap_connection


def find_one(base_dn, search_filter, attributes=None):
    """Looks for a single entry in the LDAP server.

    This will return the first entry given by the server which matches
    the ``search_filter`` found in the ``base_dn`` sub tree. If the
    ``search_filter`` matches multiples entries there is no guarantee
    the same entry is returned.

    :param base_dn: str -- The base DN from which to start the search.
    :param search_filter: str -- Representation of the filter to locate
                          the entry.
    :param attributes: list -- Attributes to be retrieved for the entry.
                       If ``None``, all attributes will be retrieved.
    :return: A tuple containing the `dn` of the entry as ``str`` and the
             found attributes in a ``dict``.
    """
    entry = current_ldap.connection.search_ext_s(base_dn, ldap.SCOPE_SUBTREE,
                                                 attrlist=attributes, filterstr=search_filter,
                                                 timeout=current_ldap.settings['timeout'], sizelimit=1)
    return next(((dn, data) for dn, data in entry if dn), (None, None))


def build_search_filter(criteria, type_filter, mapping=None, exact=False):
    """Builds a valid LDAP search filter for retrieving entries.

    :param criteria: dict -- Criteria to be `AND`ed together to build
                     the filter.
    :param mapping: dict -- Mapping from criteria to LDAP attributes
    :param exact: bool -- Match attributes values exactly if ``True``,
                  othewise perform substring matching.
    :return: str -- Valid LDAP search filter.
    """
    assertions = convert_app_data(criteria, mapping or {})
    assertions = [(k, v) for k, v in iteritems(assertions) if k and v]
    if not assertions:
        return None
    assert_template = '(%s=%s)' if exact else '(%s=*%s*)'
    filter_template = '(&{}{})'.format(assert_template * len(assertions), type_filter)
    return filter_format(filter_template, (item for assertion in assertions for item in assertion))


def get_page_cookie(server_ctrls):
    """Get the page control cookie from the server control list.

    :param server_ctrls: list -- Server controls including page control.
    :return: Cookie for page control or ``None`` if last page reached.
    :raises LDAPServerError: If the server doesn't support paging of
                             search results.
    """
    page_ctrls = [ctrl for ctrl in server_ctrls if ctrl.controlType == SimplePagedResultsControl.controlType]
    if not page_ctrls:
        raise LDAPServerError("The LDAP server ignores the RFC 2696 specification")
    return page_ctrls[0].cookie


def to_unicode(data):
    return {text_type(k): [x.decode('utf-8', 'replace') for x in v] for k, v in iteritems(data)}
