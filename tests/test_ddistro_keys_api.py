"""Consumer socket regressions with fake key/dashboard modules and a temporary socket. No store, runtime or network."""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / 'bin'


def load(name):
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(BIN / name)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api, dash = load('ddistro_keys_api'), load('ddistro_dashboard_keys')
KEY = '11111111-2222-4333-8444-555555555555'
OTHER = '99999999-2222-4333-8444-555555555555'
TOKEN = 'reign-token-' + 'x' * 30
ROUTER = 'https://openrouter.ai:443'


class FakeKeys:
    Refusal = RuntimeError

    def __init__(self):
        self.calls, self.revision, self.conflict = [], 3, False

    def invoke(self, verb, payload):
        self.calls.append(verb)
        if verb == 'global-status':
            return {'ok': True, 'outcome': 'listed', 'data': {'providers': {
                'openrouter': {'origin': ROUTER}, 'custom_openai': {'origin': None},
                'anthropic': {'origin': 'https://api.anthropic.com:443'}}, 'keys': [
                {'key_id': KEY, 'provider': 'openrouter', 'label': 'Reign', 'preview': 'sk-...abcd',
                 'revision': self.revision, 'configured': True, 'origin': ROUTER},
                {'key_id': OTHER, 'provider': 'anthropic', 'label': 'Other', 'preview': '...zz',
                 'revision': 1, 'configured': True, 'origin': 'https://api.anthropic.com:443'}]}}
        if verb == 'global-reveal':
            if self.conflict or payload['revision'] != self.revision:
                return {'ok': False, 'outcome': 'conflict'}
            return {'ok': True, 'outcome': 'revealed', 'data': {'secret': 'sk-or-secret-value'}}
        if verb == 'global-save':
            data = {'key_id': payload.get('key_id', OTHER), 'provider': payload.get('provider', 'openrouter'),
                    'label': payload['label'], 'preview': '...', 'revision': self.revision + 1, 'configured': True}
            return {'ok': True, 'outcome': 'updated' if 'key_id' in payload else 'created', 'data': data}
        raise AssertionError(verb)

    def migration_lock(self):
        return contextlib.nullcontext()


class Socket(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.keys = FakeKeys()
        clients = {'reign': {'sha256': hashlib.sha256(TOKEN.encode()).hexdigest(),
                             'providers': ['custom_openai', 'openrouter']}}
        fake = types.SimpleNamespace(**{n: getattr(dash, n) for n in ('view', 'listed', 'SECRET', 'parse_edit',
                                                                      'parse_create', 'save', 'catalog')})
        fake.keys_module = lambda: self.keys
        fake.ready = lambda keys: True
        self.fake = fake
        self.patches = [patch.object(api, 'SOCKET', os.path.join(self.tmp.name, 's.sock')),
                        patch.object(api, 'load_clients', return_value=clients)]
        for p in self.patches:
            p.start()
        self.server = api.Server(api.SOCKET, api.Handler)
        self.server.slots = threading.BoundedSemaphore(api.SLOTS)
        self.server.broker = api.Broker(fake)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def call(self, op, body=None, token=TOKEN):
        return api.call({'op': op, 'token': token, 'body': body})

    def test_token_is_required_and_status_is_masked_and_filtered(self):
        self.assertEqual(self.call('status', token='wrong-token-' + 'y' * 20)['outcome'], 'unauthorized')
        self.assertEqual(self.call('resolve', token=None)['outcome'], 'unauthorized')
        self.assertEqual(self.call('version', token=None)['outcome'], 'version')
        status = self.call('status')
        self.assertEqual([(k['key_id'], k['origin']) for k in status['keys']], [(KEY, ROUTER)])
        self.assertNotIn('secret', json.dumps(status))
        self.assertEqual({p['provider'] for p in status['providers']}, {'openrouter', 'custom_openai'})

    def test_resolve_checks_uuid_provider_origin_and_current_revision(self):
        good = {'key_id': KEY, 'provider': 'openrouter', 'origin': ROUTER}
        self.assertEqual(self.call('resolve', good),
                         {'ok': True, 'outcome': 'resolved', 'secret': 'sk-or-secret-value', 'revision': 3})
        self.assertEqual(self.call('resolve', dict(good, origin='https://evil.example:443'))['outcome'], 'wrong_provider')
        self.assertEqual(self.call('resolve', dict(good, key_id=OTHER, provider='anthropic'))['outcome'], 'invalid')
        self.assertEqual(self.call('resolve', dict(good, key_id=OTHER.replace('9', '8')))['outcome'], 'missing')
        self.keys.conflict = True
        self.keys.calls.clear()
        self.assertEqual(self.call('resolve', good)['outcome'], 'changed')
        self.assertEqual(self.keys.calls.count('global-reveal'), 1)  # never retried

    def test_edit_and_create_are_limited_to_allowed_providers(self):
        edit = {'key_id': KEY, 'revision': 3, 'label': 'Reign', 'secret': 'sk-or-new'}
        self.assertEqual(self.call('edit', edit)['outcome'], 'updated')
        self.assertEqual(self.call('edit', dict(edit, key_id=OTHER))['outcome'], 'invalid')
        create = {'provider': 'anthropic', 'label': 'x', 'origin': None, 'secret': 'abc'}
        self.assertEqual(self.call('create', create)['outcome'], 'invalid')
        made = self.call('create', dict(create, provider='openrouter'))
        self.assertEqual((made['outcome'], made['key']['origin']), ('created', ROUTER))

    def test_bracketed_ipv6_loopback_origin_is_listed_resolved_and_created(self):
        loopback, local = 'http://[::1]:1234', '22222222-2222-4333-8444-555555555555'
        invoke = self.keys.invoke

        def with_local(verb, payload):
            result = invoke(verb, payload)
            if verb == 'global-status':
                result['data']['keys'].append({'key_id': local, 'provider': 'custom_openai', 'label': 'Local',
                                               'preview': '...', 'revision': 3, 'configured': True, 'origin': loopback})
            return result
        self.keys.invoke = with_local
        self.assertIn((local, loopback), [(k['key_id'], k['origin']) for k in self.call('status')['keys']])
        resolved = self.call('resolve', {'key_id': local, 'provider': 'custom_openai', 'origin': loopback})
        self.assertEqual(resolved['outcome'], 'resolved')
        made = self.call('create', {'provider': 'custom_openai', 'label': 'x', 'origin': loopback, 'secret': 'abc'})
        self.assertEqual((made['outcome'], made['key']['origin']), ('created', loopback))
        for bad in ('http://::1:1234', 'http://[::1%eth0]:1234', 'http://[::1]', 'ftp://[::1]:1', 'http://[::G]:1'):
            self.assertIsNone(api.ORIGIN.fullmatch(bad), bad)

    def test_version_reports_the_generation_captured_at_startup(self):
        installed = os.path.join(self.tmp.name, 'ddistro_keys_api')
        Path(installed).write_bytes(b'v1')
        with patch.object(api, 'SELF', installed):
            self.server.broker = api.Broker(self.fake, api.generation())
            self.assertTrue(api.running())
            Path(installed).write_bytes(b'v2')  # an update replaced the file under the live process
            self.assertEqual(self.call('version', token=None)['generation'], hashlib.sha256(b'v1').hexdigest())
            self.assertFalse(api.running())  # so ensure replaces the outdated server

    def test_save_failure_after_dispatch_is_unknown(self):
        def broken(verb, payload):
            if verb == 'global-save':
                raise TimeoutError
            return FakeKeys.invoke(self.keys, verb, payload)
        self.keys.invoke = broken
        edit = {'key_id': KEY, 'revision': 3, 'label': 'Reign', 'secret': 'sk-or-new'}
        self.assertEqual(self.call('edit', edit)['outcome'], 'unknown')

    def test_oversized_request_is_refused_without_store_calls(self):
        self.keys.calls.clear()
        with socket.socket(socket.AF_UNIX) as sock:
            sock.connect(api.SOCKET)
            sock.sendall(b'{' + b'x' * (api.MAX_REQUEST + 10))
            self.assertEqual(json.loads(sock.recv(4096))['outcome'], 'invalid')
        self.assertEqual(self.keys.calls, [])

    def test_untrusted_socket_dir_is_refused_not_repaired(self):
        path = os.path.join(self.tmp.name, 'open')
        os.mkdir(path)
        os.chmod(path, 0o777)
        with self.assertRaises(api.Refusal):
            api.trusted_dir(path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o777)

    def test_broker_never_runs_store_setup(self):
        self.assertNotIn('ensure_global_runtime', (BIN / 'ddistro_keys_api').read_text())
        self.assertNotIn('pg_isready', (BIN / 'ddistro_keys_api').read_text())

    def test_unexpected_handler_error_never_echoes_the_request(self):
        def boom(raw):
            raise RuntimeError(raw.decode())
        self.server.broker.handle = boom
        with patch('sys.stdout') as out, patch('sys.stderr') as err:
            self.assertEqual(self.call('status'), {'ok': False, 'outcome': 'unavailable'})
        self.assertNotIn(TOKEN, repr(out.mock_calls) + repr(err.mock_calls))


class Lifecycle(unittest.TestCase):
    """Disposable lock files, pidfile and fake /proc; no real server is signalled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.patches = [patch.object(api, n, os.path.join(root, n.lower())) for n in
                        ('LIFECYCLE_LOCK', 'SERVER_LOCK', 'PIDFILE', 'SOCKET_DIR')]
        for p in self.patches:
            p.start()
        self.proc = os.path.join(root, 'proc')

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fake_process(self, pid, argv):
        os.makedirs(f'{self.proc}/{pid}', exist_ok=True)
        Path(f'{self.proc}/{pid}/cmdline').write_bytes(b'\0'.join(argv) + b'\0')
        Path(api.PIDFILE).write_text(str(pid))

    def test_exact_shebang_cmdline_is_stopped_and_awaited(self):
        self.fake_process(4321, api.SERVE_CMDLINE)
        killed = []

        def kill(pid, sig):
            killed.append(pid)
            threading.Timer(0.2, shutil.rmtree, [f'{self.proc}/{pid}']).start()
        api.stop_owned(self.proc, kill, owner=os.getuid())
        self.assertEqual(killed, [4321])
        self.assertFalse(os.path.exists(f'{self.proc}/4321'))  # returned only after the verified exit

    def test_unrelated_or_lookalike_processes_are_never_signalled(self):
        for argv in ([b'/usr/bin/python3', api.SELF.encode(), b'serve'], [b'/bin/sleep', b'30'],
                     [b'/usr/bin/python3', b'-I', api.SELF.encode(), b'ensure']):
            self.fake_process(77, argv)
            api.stop_owned(self.proc, lambda *a: self.fail('signalled'), owner=os.getuid())
        sleeper = subprocess.Popen(['sleep', '30'])
        try:
            Path(api.PIDFILE).write_text(str(sleeper.pid))
            api.stop_owned(kill=lambda *a: self.fail('signalled'), owner=os.getuid())
            self.assertIsNone(sleeper.poll())
        finally:
            sleeper.kill()
            sleeper.wait()

    def test_stale_server_that_will_not_exit_is_refused(self):
        self.fake_process(55, api.SERVE_CMDLINE)
        with self.assertRaises(api.Refusal):
            api.stop_owned(self.proc, lambda *a: None, wait=0.2, owner=os.getuid())

    def test_second_serve_never_touches_the_active_socket(self):
        os.mkdir(api.SOCKET_DIR)
        with patch.object(api, 'SOCKET', os.path.join(api.SOCKET_DIR, 'keys.sock')), socket.socket(socket.AF_UNIX) as live:
            live.bind(api.SOCKET)
            inode = os.lstat(api.SOCKET).st_ino
            held = api.lock_file(api.SERVER_LOCK)
            try:
                with self.assertRaises(api.Refusal):
                    api.serve()
            finally:
                os.close(held)
            self.assertEqual(os.lstat(api.SOCKET).st_ino, inode)

    def test_repeated_ensure_keeps_a_current_server_and_refuses_a_live_holder(self):
        spawned = []
        with patch.object(api.subprocess, 'Popen', side_effect=lambda *a, **k: spawned.append(a)), \
                patch.object(api, 'running', return_value=True):
            api.ensure()
            api.ensure()
        held = api.lock_file(api.SERVER_LOCK)
        lock_inode = os.stat(api.SERVER_LOCK).st_ino
        try:
            with patch.object(api.subprocess, 'Popen', side_effect=lambda *a, **k: spawned.append(a)), \
                    patch.object(api, 'running', return_value=False), patch.object(api, 'stop_owned'):
                with self.assertRaises(api.Refusal):
                    api.ensure()
        finally:
            os.close(held)
        self.assertEqual(spawned, [])
        self.assertEqual(os.stat(api.SERVER_LOCK).st_ino, lock_inode)


class Hold(unittest.TestCase):
    def test_existing_holder_still_ensures_and_exits(self):
        with tempfile.TemporaryDirectory() as root, patch.object(api, 'HOLD_LOCK', os.path.join(root, 'hold.lock')):
            held = api.lock_file(api.HOLD_LOCK)
            try:
                with patch.object(api, 'ensure') as ensure, patch.object(api.time, 'sleep', side_effect=AssertionError):
                    self.assertEqual(api.hold(), 0)  # recovers the server, then exits: one hold process remains
                    ensure.assert_called_once_with()
            finally:
                os.close(held)


class Enrollment(unittest.TestCase):
    def test_staged_token_keeps_the_previous_one_until_commit(self):
        clients = {'reign': {'sha256': hashlib.sha256(TOKEN.encode()).hexdigest(), 'providers': ['openrouter']}}
        with patch.object(api, 'save_clients'), patch.object(api, 'load_clients', return_value=clients):
            first = api.manage('register', 'reign', 'openrouter,openai', clients)
            self.assertEqual(api.authenticate(TOKEN)['providers'], ['openrouter'])
            self.assertEqual(api.authenticate(first['token'])['providers'], ['openai', 'openrouter'])
            api.manage('discard', 'reign', '', clients, first['stage'])  # the consumer failed to store it
            self.assertIsNotNone(api.authenticate(TOKEN))
            self.assertIsNone(api.authenticate(first['token']))
            second = api.manage('register', 'reign', 'openrouter', clients)
            api.manage('commit', 'reign', '', clients, second['stage'])
            self.assertIsNone(api.authenticate(TOKEN))
            self.assertIsNotNone(api.authenticate(second['token']))
            with self.assertRaises(api.Refusal):
                api.manage('commit', 'reign', '', clients, second['stage'])

    def test_concurrent_register_and_wrong_stage_never_commit_another_token(self):
        clients = {'reign': {'sha256': hashlib.sha256(TOKEN.encode()).hexdigest(), 'providers': ['openrouter']}}
        with patch.object(api, 'save_clients'), patch.object(api, 'load_clients', return_value=clients):
            a = api.manage('register', 'reign', 'openrouter', clients, now=1000)
            with self.assertRaisesRegex(api.Refusal, 'stage_pending'):
                api.manage('register', 'reign', 'openrouter', clients, now=1001)  # B never overwrites A
            self.assertIsNotNone(api.authenticate(a['token']))
            for command in ('commit', 'discard'):
                for stage in (None, '0' * 16):
                    with self.assertRaisesRegex(api.Refusal, 'stage_mismatch'):
                        api.manage(command, 'reign', '', clients, stage)
            with self.assertRaisesRegex(api.Refusal, 'stage_active'):
                api.manage('discard', 'reign', '', clients, abandoned=True, now=1000 + api.STAGE_ABANDONED_AFTER - 1)
            self.assertIsNotNone(api.authenticate(TOKEN))  # the last valid enrollment survives every refusal
            api.manage('discard', 'reign', '', clients, abandoned=True, now=1000 + api.STAGE_ABANDONED_AFTER)
            self.assertIsNone(api.authenticate(a['token']))
            self.assertIsNotNone(api.authenticate(TOKEN))
            b = api.manage('register', 'reign', 'openrouter', clients)
            with self.assertRaisesRegex(api.Refusal, 'stage_mismatch'):
                api.manage('commit', 'reign', '', clients, a['stage'])  # a stale stage id cannot commit B
            api.manage('commit', 'reign', '', clients, b['stage'])
            self.assertIsNotNone(api.authenticate(b['token']))
            self.assertIsNone(api.authenticate(TOKEN))


if __name__ == '__main__':
    unittest.main()
