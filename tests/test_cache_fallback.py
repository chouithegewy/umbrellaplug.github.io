"""Cache failure regressions using real SQLite databases and Kodi import stubs.

Run with: python3 -B -m unittest discover -s tests -v
"""

import ast
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
from time import time
import types
import unittest
from unittest.mock import Mock, patch


ADDON = Path(__file__).resolve().parents[1] / 'omega/plugin.video.umbrella'


class CacheFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.control = types.ModuleType('resources.lib.modules.control')
        self.control.dataPath = self.temp.name
        self.control.cacheFile = str(Path(self.temp.name) / 'main.db')
        self.control.providercacheFile = str(Path(self.temp.name) / 'providers.db')
        self.control.existsPath = lambda path: Path(path).exists()
        self.control.makeFile = lambda path: Path(path).mkdir(parents=True, exist_ok=True)
        self.control.monitor = types.SimpleNamespace(waitForAbort=lambda seconds: False)
        self.control.log_refresh_diagnostic = Mock()
        self.logger = types.ModuleType('resources.lib.modules.log_utils')
        self.logger.error = Mock()
        modules = types.ModuleType('resources.lib.modules')
        modules.control = self.control
        modules.log_utils = self.logger
        imports = {
            'resources': types.ModuleType('resources'),
            'resources.lib': types.ModuleType('resources.lib'),
            'resources.lib.modules': modules,
            'resources.lib.modules.control': self.control,
            'resources.lib.modules.log_utils': self.logger,
            'xbmc': types.ModuleType('xbmc'),
            'xbmcvfs': types.ModuleType('xbmcvfs'),
        }
        self.imports = patch.dict(sys.modules, imports)
        self.imports.start()
        self.addCleanup(self.imports.stop)
        self.caches = []
        for name in ('cache', 'providerscache'):
            path = ADDON / 'resources/lib/database' / (name + '.py')
            spec = importlib.util.spec_from_file_location('test_' + name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.caches.append(module)

    @staticmethod
    def key(module, function, args=()):
        return module._hash_function(function, args[:9] if module.__name__ == 'test_providerscache' else args)

    def seed(self, module, function, value, age=0):
        key = self.key(module, function)
        module.cache_insert(key, value)
        with closing(module.get_connection()) as connection, connection:
            connection.execute('UPDATE cache SET date=? WHERE key=?', (int(time()) - age, key))
        return key

    def test_fresh_data_survives_ast_runtime_failure(self):
        for module in self.caches:
            with self.subTest(module=module.__name__):
                expected = [{'title': 'fresh result', 'nested': (1, b'bytes')}]
                fetch = Mock(return_value=expected)
                with patch.object(module, 'literal_eval', side_effect=SystemError('AST constructor recursion depth mismatch')):
                    self.assertIs(module.get(fetch, 1), expected)
                fetch.assert_called_once_with()
                self.assertEqual(ast.literal_eval(module.cache_get(self.key(module, fetch))['value']), expected)

    def test_cached_ast_failure_refetches_and_returns_fresh_data(self):
        for module in self.caches:
            with self.subTest(module=module.__name__):
                expected = [{'title': 'recovered'}]
                fetch = Mock(return_value=expected)
                key = self.seed(module, fetch, repr([{'title': 'old'}]))
                with patch.object(module, 'literal_eval', side_effect=SystemError('AST constructor recursion depth mismatch')):
                    self.assertIs(module.get(fetch, 1), expected)
                fetch.assert_called_once_with()
                self.assertEqual(module.cache_get(key)['value'], repr(expected))

    def test_malformed_entry_is_replaced_by_fresh_result(self):
        for module in self.caches:
            with self.subTest(module=module.__name__):
                expected = [{'title': 'recovered'}]
                fetch = Mock(return_value=expected)
                key = self.seed(module, fetch, '[malformed')
                self.assertEqual(module.get(fetch, 1), expected)
                fetch.assert_called_once_with()
                self.assertEqual(module.cache_get(key)['value'], repr(expected))

    def test_valid_cache_avoids_fetch(self):
        for module in self.caches:
            with self.subTest(module=module.__name__):
                expected = [{'title': 'cached'}]
                fetch = Mock(side_effect=AssertionError('cache hit must not fetch'))
                self.seed(module, fetch, repr(expected))
                self.assertEqual(module.get(fetch, 1), expected)
                fetch.assert_not_called()

    def test_expired_cache_fetches_once(self):
        for module in self.caches:
            with self.subTest(module=module.__name__):
                expected = [{'title': 'new'}]
                fetch = Mock(return_value=expected)
                self.seed(module, fetch, repr([{'title': 'old'}]), age=7200)
                self.assertEqual(module.get(fetch, 1), expected)
                fetch.assert_called_once_with()

    def test_empty_fresh_result_keeps_stale_cache(self):
        for module in self.caches:
            for empty in (None, [], {}):
                with self.subTest(module=module.__name__, empty=empty):
                    expected = [{'title': 'stale but usable'}]
                    fetch = Mock(return_value=empty)
                    key = self.seed(module, fetch, repr(expected), age=7200)
                    self.assertEqual(module.get(fetch, 1), expected)
                    self.assertEqual(module.cache_get(key)['value'], repr(expected))

    def test_empty_fresh_result_is_not_cached(self):
        for module in self.caches:
            for empty in (None, [], {}):
                with self.subTest(module=module.__name__, empty=empty):
                    fetch = Mock(return_value=empty)
                    self.assertIsNone(module.get(fetch, 1))
                    self.assertIsNone(module.cache_get(self.key(module, fetch)))

    def test_database_failure_does_not_discard_fetched_data(self):
        for module in self.caches:
            with self.subTest(module=module.__name__):
                expected = [{'title': 'available despite database failure'}]
                fetch = Mock(return_value=expected)
                with patch.object(module, 'get_connection', side_effect=sqlite3.OperationalError('database is locked')):
                    self.assertIs(module.get(fetch, 1), expected)
                fetch.assert_called_once_with()

    def test_cached_404_remains_a_negative_cache_hit(self):
        module = self.caches[0]
        fetch = Mock(return_value='404:NOT FOUND')
        self.assertIsNone(module.get(fetch, 1))
        self.assertIsNone(module.cache_get(self.key(module, fetch))['value'])
        self.assertIsNone(module.get(fetch, 1))
        fetch.assert_called_once_with()

    def test_last_unwatched_season_can_be_removed(self):
        module = self.caches[0]
        fetch = Mock(return_value=[])
        key = self.seed(module, fetch, repr(['1']), age=7200)
        self.assertEqual(module.get(fetch, 1), [])
        self.assertIsNone(module.cache_get(key))

    def test_provider_prescrape_arguments_share_cache(self):
        module = self.caches[1]
        calls = []
        expected = [{'url': 'https://example.invalid/stream'}]

        def fetch(*args):
            calls.append(args)
            return expected

        args = tuple(range(9))
        self.assertEqual(module.get(fetch, 1, *args, {'metadata': True}, True), expected)
        self.assertEqual(module.get(fetch, 1, *args), expected)
        self.assertEqual(len(calls), 1)

    def seed_coalesced(self, function, value):
        module = self.caches[0]
        key = self.key(module, function)
        path = self.control.cacheFile + '.build-' + module._generate_md5(key)
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute('CREATE TABLE result (id INTEGER PRIMARY KEY, completed REAL, value TEXT)')
            connection.execute('INSERT INTO result VALUES (1, ?, ?)', (time() + 60, value))
        return path

    def test_invalid_shared_progress_result_is_rebuilt(self):
        module = self.caches[0]
        expected = [{'title': 'rebuilt progress'}]
        fetch = Mock(return_value=expected)
        path = self.seed_coalesced(fetch, '[malformed')
        self.assertEqual(module.get_coalesced(fetch, 1), expected)
        fetch.assert_called_once_with()
        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(connection.execute('SELECT value FROM result WHERE id=1').fetchone()[0], repr(expected))

    def test_shared_ast_failure_returns_fresh_progress(self):
        module = self.caches[0]
        expected = [{'title': 'fresh progress'}]
        fetch = Mock(return_value=expected)
        self.seed_coalesced(fetch, repr([{'title': 'old progress'}]))
        with patch.object(module, 'literal_eval', side_effect=SystemError('AST constructor recursion depth mismatch')):
            self.assertIs(module.get_coalesced(fetch, 1), expected)
        fetch.assert_called_once_with()

    def test_valid_shared_progress_avoids_rebuild(self):
        module = self.caches[0]
        expected = [{'title': 'shared progress'}]
        fetch = Mock(side_effect=AssertionError('shared result must not rebuild'))
        self.seed_coalesced(fetch, repr(expected))
        self.assertEqual(module.get_coalesced(fetch, 1), expected)
        fetch.assert_not_called()
        self.control.log_refresh_diagnostic.assert_called_once_with('mdb-build-shared-result')


if __name__ == '__main__':
    unittest.main()
