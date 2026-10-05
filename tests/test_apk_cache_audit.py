"""Verify the read-only audit detects partial or corrupt startup asset trees."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'tools/kodi/check_apk_cache.py'
SPEC = importlib.util.spec_from_file_location('apk_cache_audit', SCRIPT)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class ApkCacheAuditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.apk = self.root / 'base.apk'
        self.assets = self.root / 'apk/assets'
        self.assets.mkdir(parents=True)
        with zipfile.ZipFile(self.apk, 'w') as package:
            package.writestr('assets/system/settings/settings.xml', b'valid settings')
            package.writestr('assets/addons/audioencoder.kodi.builtin.aac/addon.xml', b'valid addon')
            package.writestr('classes.dex', b'not an extracted asset')
        self.settings = self.assets / 'system/settings/settings.xml'
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(b'valid settings')
        self.addon = self.assets / 'addons/audioencoder.kodi.builtin.aac/addon.xml'
        self.addon.parent.mkdir(parents=True)
        self.addon.write_bytes(b'valid addon')

    def test_complete_cache_matches_checksums_and_is_not_modified(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in (self.apk, self.settings, self.addon)}
        result = AUDIT.audit(self.apk, self.assets)
        self.assertTrue(result['ok'])
        self.assertEqual(result['checked'], 2)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in before})

    def test_partial_tree_is_bad_even_when_legacy_timestamp_says_current(self):
        self.addon.unlink()
        stamp = self.apk.stat().st_mtime_ns + 1000000000
        os.utime(self.assets.parent, ns=(stamp, stamp))
        result = AUDIT.audit(self.apk, self.assets)
        self.assertFalse(result['ok'])
        self.assertTrue(result['splash_would_skip_extraction'])
        self.assertEqual(result['missing'], 1)

    def test_truncated_asset_is_detected(self):
        self.settings.write_bytes(b'valid')
        self.assertEqual(AUDIT.audit(self.apk, self.assets)['damaged'], 1)

    def test_same_size_corruption_is_detected_by_checksum(self):
        self.settings.write_bytes(b'broken content')
        self.assertEqual(self.settings.stat().st_size, len(b'valid settings'))
        self.assertEqual(AUDIT.audit(self.apk, self.assets)['damaged'], 1)

    def test_android_timestamp_comparison_uses_milliseconds(self):
        os.utime(self.apk, ns=(2000999999, 2000999999))
        os.utime(self.assets.parent, ns=(2000000000, 2000000000))
        self.assertTrue(AUDIT.audit(self.apk, self.assets)['splash_would_skip_extraction'])

    def test_path_traversal_is_rejected_without_reading_outside_cache(self):
        with zipfile.ZipFile(self.apk, 'a') as package:
            package.writestr('assets/../../private-settings.xml', b'private')
        result = AUDIT.audit(self.apk, self.assets)
        self.assertFalse(result['ok'])
        self.assertEqual(result['unsafe_entries'], 1)


if __name__ == '__main__':
    unittest.main()
