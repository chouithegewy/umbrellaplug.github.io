"""Run inside Android Kodi: RunScript(path/to/check_apk_cache.py,path/to/base.apk).

Read-only audit of bundled assets. The report contains counts and APK asset names,
never account settings or file contents. Output: special://temp/kodi-apk-cache.json.
"""

import json
import os
from pathlib import Path
import sys
import zipfile
import zlib


def audit(apk_path, assets_path):
    assets_path = Path(assets_path).resolve()
    result = {
        'checked': 0, 'bytes_checked': 0, 'missing': 0, 'damaged': 0,
        'unreadable': 0, 'unsafe_entries': 0, 'examples': [],
    }

    def record(kind, name):
        result[kind] += 1
        if len(result['examples']) < 20:
            result['examples'].append({'kind': kind, 'asset': name})

    with zipfile.ZipFile(apk_path) as package:
        for entry in package.infolist():
            if not entry.filename.startswith('assets/') or entry.is_dir():
                continue
            name = entry.filename[len('assets/'):]
            path = (assets_path / name).resolve()
            if assets_path not in path.parents:
                record('unsafe_entries', name)
                continue
            result['checked'] += 1
            try:
                size = path.stat().st_size
                if size != entry.file_size:
                    record('damaged', name)
                    continue
                crc = 0
                with path.open('rb') as cached_file:
                    for chunk in iter(lambda: cached_file.read(65536), b''):
                        crc = zlib.crc32(chunk, crc)
                        result['bytes_checked'] += len(chunk)
                if (crc & 0xffffffff) != entry.CRC:
                    record('damaged', name)
            except FileNotFoundError:
                record('missing', name)
            except OSError:
                record('unreadable', name)

    # Splash.java uses the parent directory timestamp as its completeness test.
    result['splash_would_skip_extraction'] = (
        assets_path.parent.exists()
        and assets_path.parent.stat().st_mtime_ns // 1000000
        >= os.stat(apk_path).st_mtime_ns // 1000000
    )
    result['ok'] = result['checked'] > 0 and not any(
        result[key] for key in ('missing', 'damaged', 'unreadable', 'unsafe_entries')
    )
    return result


def main():
    import xbmc
    import xbmcvfs

    report_path = xbmcvfs.translatePath('special://temp/kodi-apk-cache.json')
    try:
        result = audit(sys.argv[1], xbmcvfs.translatePath('special://xbmc/'))
    except Exception as error:
        # Do not include paths or arbitrary exception text in the report.
        result = {'ok': False, 'error_type': type(error).__name__}
    with open(report_path + '.tmp', 'w', encoding='utf-8') as report:
        json.dump(result, report, indent=2)
        report.write('\n')
    os.replace(report_path + '.tmp', report_path)
    xbmc.log('Kodi APK cache audit: ok=%s' % result['ok'], xbmc.LOGINFO)


if __name__ == '__main__':
    main()
