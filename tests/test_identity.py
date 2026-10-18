import httpx
import pytest

from cloudyfan_jev.identity import BearerAuthenticator, IdentityError, IdentityStore


PEPPER = b'fixture-only-identity-pepper-32-bytes'


def request(token, *, method='GET', path='/v1/tasks/task-1', scheme='https', remote='127.0.0.1'):
    return {'HTTP_AUTHORIZATION': 'Bearer ' + token, 'REQUEST_METHOD': method,
            'PATH_INFO': path, 'wsgi.url_scheme': scheme, 'REMOTE_ADDR': remote}


def test_operator_issues_scoped_expiring_credentials_that_survive_restart(tmp_path):
    now = [1000.0]
    path = tmp_path / 'identities' / 'identity.db'
    store = IdentityStore(path, pepper=PEPPER, clock=lambda: now[0])
    store.create_principal('alice')
    issued = store.issue('alice', scopes={'tasks:read'}, expires_at=1100)
    auth = BearerAuthenticator(IdentityStore(path, pepper=PEPPER, clock=lambda: now[0]))
    assert auth(request(issued.token)) == 'alice'
    assert auth(request(issued.token, method='POST', path='/v1/tasks')) is None
    assert store.credentials('alice') == [{'id': issued.id, 'principal': 'alice',
        'scopes': ['tasks:read'], 'created_at': 1000.0, 'expires_at': 1100.0, 'revoked_at': None}]
    now[0] = 1100
    assert auth(request(issued.token)) is None


def test_rotation_revocation_and_account_disable_apply_on_every_request(tmp_path):
    now = [1000.0]
    path = tmp_path / 'identity.db'
    store = IdentityStore(path, pepper=PEPPER, clock=lambda: now[0])
    store.create_principal('alice')
    old = store.issue('alice', scopes={'tasks:read', 'tasks:write'}, expires_at=1100)
    auth = BearerAuthenticator(IdentityStore(path, pepper=PEPPER, clock=lambda: now[0]))
    replacement = store.rotate(old.id, expires_at=1200)
    assert auth(request(old.token)) is None
    assert auth(request(replacement.token)) == 'alice'
    store.set_active('alice', False)
    assert not store.is_active('alice')
    assert auth(request(replacement.token)) is None
    with pytest.raises(IdentityError, match='Active'):
        store.issue('alice', scopes={'tasks:read'}, expires_at=1300)
    store.set_active('alice', True)
    assert auth(request(replacement.token)) == 'alice'
    store.revoke(replacement.id)
    assert auth(request(replacement.token)) is None
    assert all(row['revoked_at'] == 1000.0 for row in store.credentials('alice'))
    assert not store.is_active('not-registered')


def test_failed_rotation_preserves_old_credential_and_does_not_issue_another(tmp_path):
    store = IdentityStore(tmp_path / 'identity.db', pepper=PEPPER, clock=lambda: 1000)
    store.create_principal('alice')
    old = store.issue('alice', scopes={'tasks:read'}, expires_at=1100)
    with pytest.raises(IdentityError, match='future'):
        store.rotate(old.id, expires_at=999)
    assert BearerAuthenticator(store)(request(old.token)) == 'alice'
    assert len(store.credentials('alice')) == 1
    store.revoke(old.id)
    with pytest.raises(IdentityError, match='active'):
        store.rotate(old.id, expires_at=1200)


def test_https_is_required_and_forwarded_headers_cannot_enable_plaintext_credentials(tmp_path):
    store = IdentityStore(tmp_path / 'identity.db', pepper=PEPPER, clock=lambda: 1000)
    store.create_principal('alice')
    token = store.issue('alice', scopes={'tasks:read'}, expires_at=1100).token
    auth = BearerAuthenticator(store)
    plain = {**request(token, scheme='http'), 'HTTP_X_FORWARDED_PROTO': 'https',
             'HTTP_FORWARDED': 'proto=https', 'HTTP_HOST': 'localhost'}
    assert auth(plain) is None
    local = BearerAuthenticator(store, allow_insecure_loopback=True)
    assert local(request(token, scheme='http')) == 'alice'
    assert local(request(token, scheme='http', remote='::1')) == 'alice'
    assert local(request(token, scheme='http', remote='203.0.113.20')) is None
    assert local(request(token, scheme='http', remote='localhost')) is None
    assert local(request(token, scheme='ftp')) is None


def test_http_application_uses_server_scopes_and_live_revoke_without_dispatch(tmp_path):
    from cloudyfan_jev.http_api import HTTPApp
    from test_http_api import TASK, service_client
    original, service, _, api, _ = service_client(tmp_path)
    original.close()
    store = IdentityStore(tmp_path / 'identity' / 'identity.db', pepper=PEPPER, clock=lambda: 1000)
    store.create_principal('admin')
    read = store.issue('admin', scopes={'tasks:read'}, expires_at=1100)
    write = store.issue('admin', scopes={'tasks:write'}, expires_at=1100)
    app = HTTPApp(service, authenticate=BearerAuthenticator(store))
    with httpx.Client(transport=httpx.WSGITransport(app), base_url='https://fixture') as client:
        denied = client.post('/v1/tasks', json=TASK, headers={'Authorization': 'Bearer ' + read.token})
        assert denied.status_code == 401 and api.requests == []
        response = client.post('/v1/tasks', json=TASK, headers={'Authorization': 'Bearer ' + write.token})
        assert response.status_code == 200
        assert client.get('/v1/tasks/task-1/result', headers={'Authorization': 'Bearer ' + read.token}).json()['answer'] == '2'
        assert client.get('/v1/tasks/task-1', headers={'Authorization': 'Bearer ' + write.token}).status_code == 401
        store.revoke(read.id)
        assert client.get('/v1/tasks/task-1', headers={'Authorization': 'Bearer ' + read.token}).status_code == 401
        assert len(api.requests) == 1


def test_credentials_are_redacted_and_private_database_contains_no_plaintext_token(tmp_path):
    path = tmp_path / 'private' / 'identity.db'
    store = IdentityStore(path, pepper=PEPPER, clock=lambda: 1000)
    store.create_principal('alice')
    issued = store.issue('alice', scopes={'tasks:read'}, expires_at=1100)
    assert issued.token not in repr(issued)
    assert issued.token not in str(store.credentials('alice'))
    assert issued.token.encode() not in path.read_bytes()
    assert issued.token.split('.')[1].encode() not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(IdentityError, match='secret does not match'):
        IdentityStore(path, pepper=b'different-fixture-pepper-of-32-bytes', clock=lambda: 1000)
    assert BearerAuthenticator(store)(request(issued.token)) == 'alice'


@pytest.mark.parametrize('unsafe', ['directory_symlink', 'database_symlink', 'public_parent',
                                    'public_database', 'journal_symlink', 'hardlinked_database'])
def test_unsafe_registry_paths_are_rejected_without_touching_external_targets(tmp_path, unsafe):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    outside = tmp_path / 'outside'
    outside.mkdir(mode=0o700)
    marker = outside / 'untouched'
    marker.write_text('private fixture sentinel')
    path = private / 'identity.db'
    if unsafe == 'directory_symlink':
        private = tmp_path / 'alias'
        private.symlink_to(outside, target_is_directory=True)
        path = private / 'identity.db'
    elif unsafe == 'database_symlink':
        path.symlink_to(marker)
    elif unsafe == 'public_parent':
        private.chmod(0o755)
    elif unsafe == 'public_database':
        path.write_bytes(b'')
        path.chmod(0o644)
    elif unsafe == 'journal_symlink':
        path.with_name(path.name + '-journal').symlink_to(marker)
    else:
        import os
        path.write_bytes(b'')
        path.chmod(0o600)
        os.link(path, outside / 'linked')
    with pytest.raises(IdentityError, match='[Pp]rivate|[Uu]nsafe'):
        IdentityStore(path, pepper=PEPPER)
    assert marker.read_text() == 'private fixture sentinel'


@pytest.mark.parametrize('header', ['', 'Bearer', 'Basic anything', 'Bearer invalid',
                                   'Bearer x\ny', 'Bearer a b', 'Bearer ' + 'x' * 1024])
def test_malformed_authorization_is_denied_without_logging_or_returning_token(tmp_path, header, caplog):
    store = IdentityStore(tmp_path / 'identity.db', pepper=PEPPER)
    auth = BearerAuthenticator(store)
    assert auth({**request('placeholder'), 'HTTP_AUTHORIZATION': header}) is None
    assert caplog.records == []


def test_forged_secret_and_unknown_identifier_are_denied(tmp_path):
    store = IdentityStore(tmp_path / 'identity.db', pepper=PEPPER, clock=lambda: 1000)
    store.create_principal('alice')
    issued = store.issue('alice', scopes={'tasks:read'}, expires_at=1100)
    auth = BearerAuthenticator(store)
    identifier, secret = issued.token.split('.')
    assert auth(request(identifier + '.' + ('x' if secret[0] != 'x' else 'y') + secret[1:])) is None
    assert auth(request('cf1_' + '0' * 32 + '.' + secret)) is None


@pytest.mark.parametrize('scopes,expiry', [('tasks:read', 1100), ({'*'}, 1100), (set(), 1100),
                                          ({'tasks:read'}, float('inf')), ({'tasks:read'}, True)])
def test_operator_must_supply_supported_scopes_and_finite_explicit_expiry(tmp_path, scopes, expiry):
    store = IdentityStore(tmp_path / 'identity.db', pepper=PEPPER, clock=lambda: 1000)
    store.create_principal('alice')
    with pytest.raises(IdentityError):
        store.issue('alice', scopes=scopes, expires_at=expiry)
    assert store.credentials('alice') == []


def test_competing_rotations_have_only_one_committed_successor(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'identity.db'
    store = IdentityStore(path, pepper=PEPPER, clock=lambda: 1000)
    store.create_principal('alice')
    issued = store.issue('alice', scopes={'tasks:read'}, expires_at=1100)
    def rotate():
        try:
            other = IdentityStore(path, pepper=PEPPER, clock=lambda: 1000)
            return other.rotate(issued.id, expires_at=1200)
        except IdentityError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        successors = list(pool.map(lambda _: rotate(), range(2)))
    assert sum(result is not None for result in successors) == 1
    assert len(store.credentials('alice')) == 2
    assert BearerAuthenticator(store)(request(issued.token)) is None


def test_ollama_tags_and_generation_require_separate_scopes(tmp_path):
    from cloudyfan_jev.ollama_api import OllamaApp
    from test_http_api import service_client
    original, service, _, api, _ = service_client(tmp_path)
    original.close()
    store = IdentityStore(tmp_path / 'identity' / 'identity.db', pepper=PEPPER, clock=lambda: 1000)
    store.create_principal('admin')
    listed = store.issue('admin', scopes={'models:read'}, expires_at=1100)
    writer = store.issue('admin', scopes={'tasks:write'}, expires_at=1100)
    app = OllamaApp(service, models={'cloudyfan/sum:v1': 'arithmetic'}, authenticate=BearerAuthenticator(store))
    body = {'model': 'cloudyfan/sum:v1', 'prompt': 'Return 1+1', 'stream': False}
    with httpx.Client(transport=httpx.WSGITransport(app), base_url='https://fixture',
                      headers={'X-CloudyFan-Run-ID': 'native-1'}) as client:
        assert client.get('/api/tags', headers={'Authorization': 'Bearer ' + listed.token}).status_code == 200
        assert client.post('/api/generate', json=body, headers={'Authorization': 'Bearer ' + listed.token}).status_code == 401
        assert client.get('/api/tags', headers={'Authorization': 'Bearer ' + writer.token}).status_code == 401
        response = client.post('/api/generate', json=body, headers={'Authorization': 'Bearer ' + writer.token})
        assert response.json()['response'] == '2'
        assert len(api.requests) == 1


def test_queue_submit_and_cancel_require_write_and_status_requires_read(tmp_path):
    from cloudyfan_jev.scheduler import QueueHTTPApp
    from test_scheduler import scheduler
    queue, original, _, api, _ = scheduler(tmp_path)
    original.close()
    store = IdentityStore(tmp_path / 'identity' / 'identity.db', pepper=PEPPER, clock=lambda: 1000)
    store.create_principal('alice')
    reader = store.issue('alice', scopes={'tasks:read'}, expires_at=1100)
    writer = store.issue('alice', scopes={'tasks:write'}, expires_at=1100)
    app = QueueHTTPApp(queue, authenticate=BearerAuthenticator(store))
    body = {'run_id': 'queued-1', 'contract_id': 'sum', 'prompt': 'Return 1+1'}
    with httpx.Client(transport=httpx.WSGITransport(app), base_url='https://fixture') as client:
        read = {'Authorization': 'Bearer ' + reader.token}
        write = {'Authorization': 'Bearer ' + writer.token}
        assert client.post('/v1/tasks', json=body, headers=read).status_code == 401
        assert client.post('/v1/tasks', json=body, headers=write).status_code == 202
        assert client.get('/v1/tasks/queued-1', headers=read).json()['status'] == 'queued'
        assert client.post('/v1/tasks/queued-1/cancel', json={}, headers=read).status_code == 401
        assert client.post('/v1/tasks/queued-1/cancel', json={}, headers=write).json()['status'] == 'cancelled'
        assert api.requests == []


def test_expired_credential_stays_denied_after_clock_rollback_and_reopen(tmp_path):
    now = [1000.0]
    path = tmp_path / 'identity.db'
    store = IdentityStore(path, pepper=PEPPER, clock=lambda: now[0])
    store.create_principal('alice')
    issued = store.issue('alice', scopes={'tasks:read'}, expires_at=1100)
    now[0] = 1100
    assert BearerAuthenticator(store)(request(issued.token)) is None
    now[0] = 1050
    reopened = IdentityStore(path, pepper=PEPPER, clock=lambda: now[0])
    assert BearerAuthenticator(reopened)(request(issued.token)) is None


def test_credential_is_not_valid_before_its_issuance_when_wall_clock_rolls_back(tmp_path):
    now = [2000.0]
    path = tmp_path / 'identity.db'
    store = IdentityStore(path, pepper=PEPPER, clock=lambda: now[0])
    store.create_principal('alice')
    issued = store.issue('alice', scopes={'tasks:read'}, expires_at=2100)
    now[0] = 1999
    reopened = IdentityStore(path, pepper=PEPPER, clock=lambda: now[0])
    assert BearerAuthenticator(reopened)(request(issued.token)) is None
    with pytest.raises(IdentityError, match='backwards'):
        reopened.issue('alice', scopes={'tasks:read'}, expires_at=2200)
    now[0] = 2000
    assert BearerAuthenticator(reopened)(request(issued.token)) == 'alice'


def test_bearer_authenticator_fails_closed_on_registry_and_clock_errors(tmp_path, caplog):
    now = [1000.0]
    path = tmp_path / 'identity.db'
    store = IdentityStore(path, pepper=PEPPER, clock=lambda: now[0])
    store.create_principal('alice')
    issued = store.issue('alice', scopes={'tasks:read'}, expires_at=1100)
    auth = BearerAuthenticator(store)
    now[0] = float('nan')
    assert auth(request(issued.token)) is None
    now[0] = 1000
    path.chmod(0o644)
    assert auth(request(issued.token)) is None
    assert caplog.records == []
