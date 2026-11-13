"""Offline regression tests for cache persistence and the HTTP surface."""
import concurrent.futures
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'dashboard'))
import app
import render


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.old = dict(app.CACHE)
        app.CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'cache.json')
        self.settings = patch.multiple(app.config, DEMO=False, CACHE_FILE=self.path)
        self.settings.start()

    def tearDown(self):
        self.settings.stop()
        app.CACHE.clear()
        app.CACHE.update(self.old)
        self.tmp.cleanup()

    def test_failure_preserves_data_age(self):
        app.CACHE['fx'] = {'data': [1], 'at': 100, 'ttl': 60, 'err': None}
        def fail():
            raise RuntimeError('offline')
        app.fetch_into_cache('fx', fail, 60)
        entry = app.cache_get('fx')
        self.assertEqual(entry['at'], 100)
        self.assertEqual(entry['data'], [1])
        self.assertGreater(entry['attempted_at'], 100)
        self.assertFalse(app.cache_valid(entry))

    def test_parallel_writes_persist_complete_snapshot(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: app.cache_put(str(i), [i]), range(30)))
        saved = json.loads(Path(self.path).read_text())
        self.assertEqual(len(saved), 30)
        self.assertFalse(list(Path(self.tmp.name).glob('*.tmp')))

    def test_restore_ignores_invalid_metadata_and_unknown_keys(self):
        Path(self.path).write_text(json.dumps({
            'fx': {'data': [1], 'at': 'bad', 'ttl': 10},
            'wb_cpi': {'data': [2], 'at': time.time(), 'ttl': 60},
            'unknown': {'data': [3], 'at': time.time(), 'ttl': 60},
        }))
        app.load_disk_cache()
        self.assertEqual(set(app.CACHE), {'wb_cpi'})

    def test_corrupt_file_is_nonfatal(self):
        Path(self.path).write_text('{bad')
        app.load_disk_cache()
        self.assertEqual(app.CACHE, {})

    def test_invalid_cache_is_never_fresh(self):
        for at in (None, 'bad', float('nan'), float('inf'), time.time() + 500):
            self.assertFalse(app.cache_valid({'data': [1], 'at': at, 'ttl': 60}))


class HTTPTests(unittest.TestCase):
    def test_health_head_and_not_found(self):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for method, path, code in [('GET', '/healthz', 200),
                                       ('HEAD', '/healthz', 200),
                                       ('GET', '/missing', 404)]:
                conn = http.client.HTTPConnection(*server.server_address, timeout=5)
                conn.request(method, path)
                response = conn.getresponse()
                self.assertEqual(response.status, code)
                self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
                self.assertIn("base-uri 'none'", response.getheader('Content-Security-Policy'))
                body = response.read()
                if method == 'HEAD':
                    self.assertEqual(body, b'')
                    self.assertEqual(response.getheader('Content-Length'), '3')
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_shared_accessibility_and_refresh_controls(self):
        self.assertIn('id="main-content"', render.PAGE)
        self.assertIn('Pause auto-refresh', render.PAGE)
        self.assertIn('visibilitychange', render.PAGE)
        self.assertIn('aria-current="page"', render.tabs('stocks'))
        self.assertEqual(render.tabs('stocks').count('aria-current="page"'), 1)
