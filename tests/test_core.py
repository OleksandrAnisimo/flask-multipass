# This file is part of Flask-MultiAuth.
# Copyright (C) 2015 CERN
#
# Flask-MultiAuth is free software; you can redistribute it
# and/or modify it under the terms of the Revised BSD License.

from __future__ import unicode_literals

import pytest
from flask import Flask, request, session
from mock import Mock

from flask_multiauth import MultiAuth, AuthProvider, AuthenticationFailed


def test_init_app_twice():
    multiauth = MultiAuth()
    app = Flask('test')
    multiauth.init_app(app)
    with pytest.raises(RuntimeError):
        multiauth.init_app(app)


def test_init_app_late():
    app = Flask('text')
    multiauth = MultiAuth()
    multiauth.init_app(app)
    assert app.extensions['multiauth'].multiauth is multiauth


def test_init_app_immediately():
    app = Flask('test')
    multiauth = MultiAuth(app)
    assert app.extensions['multiauth'].multiauth is multiauth


def test_multiple_apps():
    apps = Flask('test'), Flask('test')
    multiauth = MultiAuth()
    for app in apps:
        multiauth.init_app(app)
    # The separate loop here is on purpose as the extension needs to
    # be present on all apps after initializing them
    for app in apps:
        assert app.extensions['multiauth'].multiauth is multiauth


class FooProvider(AuthProvider):
    pass


class UniqueProvider(AuthProvider):
    multi_instance = False


def test_initialize_providers():
    app = Flask('test')
    app.config['MULTIAUTH_AUTH_PROVIDERS'] = {
        'test': {'type': 'foo', 'foo': 'bar'},
        'test2': {'type': 'unique', 'hello': 'world'},
    }
    multiauth = MultiAuth()
    multiauth.register_provider(FooProvider, 'foo')
    multiauth.register_provider(UniqueProvider, 'unique')
    with app.app_context():
        auth_providers = multiauth._create_providers('AUTH', AuthProvider)
        assert auth_providers['test'].settings == {'foo': 'bar'}
        assert auth_providers['test2'].settings == {'hello': 'world'}


def test_initialize_providers_unique():
    app = Flask('test')
    app.config['MULTIAUTH_AUTH_PROVIDERS'] = {
        'test': {'type': 'unique', 'foo': 'bar'},
        'test2': {'type': 'unique', 'hello': 'world'},
    }
    multiauth = MultiAuth()
    multiauth.register_provider(FooProvider, 'foo')
    multiauth.register_provider(UniqueProvider, 'unique')
    with pytest.raises(RuntimeError):
        multiauth.init_app(app)


def test_create_login_rule(mocker):
    process_login = mocker.patch.object(MultiAuth, 'process_login')
    app = Flask('test')
    MultiAuth(app)
    with app.test_client() as c:
        for url in app.config['MULTIAUTH_LOGIN_URLS']:
            c.get(url)
    assert process_login.call_count == 2


def test_create_login_rule_disabled(mocker):
    process_login = mocker.patch.object(MultiAuth, 'process_login')
    app = Flask('test')
    app.config['MULTIAUTH_LOGIN_URLS'] = None
    MultiAuth(app)
    with app.test_client() as c:
        for url in ('/login/', '/login/<provider>'):
            assert c.get(url).status_code == 404
    assert not process_login.called


def test_render_template(mocker):
    render_template = mocker.patch('flask_multiauth.core.render_template')
    app = Flask('test')
    app.config['MULTIAUTH_FOO_TEMPLATE'] = None
    app.config['MULTIAUTH_BAR_TEMPLATE'] = 'bar.html'
    multiauth = MultiAuth(app)
    with app.app_context():
        with pytest.raises(RuntimeError):
            multiauth.render_template('FOO', foo='bar')
        multiauth.render_template('BAR', foo='bar')
        render_template.assert_called_with('bar.html', foo='bar')


def test_next_url():
    app = Flask('test')
    app.add_url_rule('/success', 'success')
    app.config['SECRET_KEY'] = 'testing'
    app.config['MULTIAUTH_SUCCESS_ENDPOINT'] = 'success'
    multiauth = MultiAuth(app)
    with app.test_request_context():
        # default url - not in session
        assert multiauth._get_next_url() == '/success'
        multiauth._set_next_url()
        # default url - in session
        assert multiauth._get_next_url() == '/success'
        request.args = {'next': '/private'}
        # next url specified, but not in session yet
        assert multiauth._get_next_url() == '/success'
        multiauth._set_next_url()
        # removed from session after retrieving it once
        assert multiauth._get_next_url() == '/private'
        assert multiauth._get_next_url() == '/success'


def test_login_finished():
    multiauth = MultiAuth()
    with pytest.raises(AssertionError):
        multiauth.login_finished(None)
    callback = Mock()
    multiauth.identity_handler(callback)
    multiauth.login_finished('foo')
    callback.assert_called_with('foo')


def test_identity_handler():
    multiauth = MultiAuth()
    callback = Mock()
    assert multiauth.identity_handler(callback) is callback


def test_handle_auth_error(mocker):
    flash = mocker.patch('flask_multiauth.core.flash')
    app = Flask('test')
    app.config['SECRET_KEY'] = 'testing'
    multiauth = MultiAuth(app)
    with app.test_request_context():
        multiauth.handle_auth_error(AuthenticationFailed())
        assert flash.called
        assert session['multiauth_auth_failed']


def test_handle_auth_error_with_redirect(mocker):
    flash = mocker.patch('flask_multiauth.core.flash')
    redirect = mocker.patch('flask_multiauth.core.redirect')
    app = Flask('test')
    app.config['SECRET_KEY'] = 'testing'
    multiauth = MultiAuth(app)
    with app.test_request_context():
        multiauth.handle_auth_error(AuthenticationFailed(), redirect_to_login=True)
        assert flash.called
        redirect.assert_called_with(app.config['MULTIAUTH_LOGIN_URLS'][0])
