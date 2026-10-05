# Chromecast search and startup investigation

Investigated October 4, 2026: Chromecast (sabrina), Android 14, Kodi 21.2,
Umbrella 6.7.90. These are three separate failures with different remedies.

## Search results hidden by Kodi

The same saved movie search returned results through `Files.GetDirectory` but
showed zero items in Kodi's actual video window. The screen title was
`Movies / Umbrella: Watched`. In Estuary's left sidebar, changing **Watched** to
**All videos** immediately displayed `Coyote vs. Acme (2026)` without another
request or cache deletion. This is Kodi's video filter; cache repair cannot
make an unwatched movie pass a watched-only filter.

If search looks empty, open the left sidebar and check this filter first.
Validate the visible directory as well as an API listing; an API listing alone
does not validate Kodi's view filters.

## Umbrella cache parser failures

The collected logs also contain an AST parser `SystemError`, pagination errors
when `folderName` is absent, and a source error handler accessing an unset URL.
The installed add-on matched upstream 6.7.90 before repair, so the earlier local
patch was absent.

The source changes treat unreadable cached values as misses, return fetched
Python objects directly, rebuild unreadable shared progress results, preserve
cached 404s, and guard the missing folder name and source URL. The cache format
and existing databases remain compatible. Fourteen SQLite regression tests
exercise parser failures, coalescing, expiration, negative caching, stale-result
fallbacks, and database write failures. Search and pagination passed through
Kodi's API before and after restart; the view-filter fix was verified on screen.

This branch is based on upstream
[6.7.90](https://github.com/umbrellaplug/umbrellaplug.github.io/commit/3baead15cc75e6a17a708e1a4f884342d2e58295).
[6.7.88](https://github.com/umbrellaplug/umbrellaplug.github.io/commit/00dbaa168cb6a04f29731b00eefaa7fe4dd4226d)
already shortens cache write waits and coalesces progress builds; those changes
are retained. The upstream version does not contain this parser fallback.
Automatic updates were disabled only for Umbrella on the test Chromecast to
keep an update from replacing the installed local patch. Re-enable them when
the corresponding fixes are included upstream. No upstream submission was made.

## Kodi startup before add-ons run

Two captured failures happen inside Kodi before Umbrella can execute:

- September 13: `audioencoder.kodi.builtin.aac` was missing or disabled, followed
  by `CServiceManager::InitStageTwo: Unable to start CAddonMgr`.
- October 4, 21:25:32: Android captured SIGSEGV in
  `CSettingsManager::GetSetting`, called by `CAdvancedSettings::ParseSettingsFile`
  during settings loading. The Kodi log ends after reading the debug logging
  configuration, before add-on initialization.

Kodi 21.2 extracts required bundled files into its **Android application cache**.
Its launcher uses the extraction directory's modification time to decide whether
the files are complete. Creating that directory can make this test pass while
extraction is still running. A second startup can then launch native Kodi against
a partial tree; interruption leaves the same incomplete tree trusted on later
launches. Per-file extraction errors also do not abort extraction in this version.
See the official
[21.2 launcher](https://github.com/xbmc/xbmc/blob/21.2-Omega/tools/android/packaging/xbmc/src/Splash.java.in).

This mechanism explains why Android's **Clear cache** can restore startup: it
forces those bundled files to be extracted again. The missing built-in add-on
and early settings crash are consistent with incomplete assets, but the failed
tree was not preserved, so its corruption cannot be proved retrospectively.
At inspection, storage was 93% full (about 319 MiB free). That leaves little
headroom; the collected logs do not establish a disk-full or low-memory kill as
the trigger.

The current extracted tree was checked against the installed base APK: all
**3,984 assets**, totaling **64,236,174 bytes**, matched sizes and CRCs. There were
no missing, damaged, unreadable, or unsafe entries. The intermittent startup
failure was not reproduced during this investigation. Additional restart tests
were deferred once the user started video playback.

### Upstream fixes and permanent repair

Kodi upstream has fixes for this exact extraction race:

- [`0fbe551fdd6f`](https://github.com/xbmc/xbmc/commit/0fbe551fdd6f879a268e2702e4477fc59f0cd7a9),
  September 9: write a completion stamp only after extraction finishes, serialize
  extraction workers, and require the stamp before starting native Kodi.
- [`fef77f6611dc`](https://github.com/xbmc/xbmc/commit/fef77f6611dc83250e824cc667056bab8267adc6),
  September 10: abort on failed asset writes and send the extraction result
  directly to the startup state machine.

The published **Kodi 22 RC1** source contains these changes. **21.2 and 21.3** do
not. This was verified against each official release tag on October 4.
[22 RC1 is a prerelease](https://github.com/xbmc/xbmc/releases/tag/22.0rc1-Piers);
a permanent fix for this particular race requires a Kodi build containing these
changes or a backport. An Umbrella Python patch cannot fix its host application's
pre-Python launcher. Kodi was not upgraded during this repair.

For another failed startup, collect the current and old Kodi logs and Android
native crash history **before** relaunching or clearing cache. If the same
incomplete-asset failure recurs, stop Kodi, use Android's **Clear cache**, allow
the next extraction to finish, then relaunch. **Clear data** resets account
settings and add-ons and is not part of this procedure.

## Diagnostic tools

From the repository root:

```sh
bash tools/kodi/fetch-logs.sh <connected-Chromecast-serial>
python3 -B -W error::ResourceWarning -m unittest discover -s tests -v
```

The log helper verifies `ro.product.model=Chromecast`, bounds ADB calls, includes
storage/memory availability and native crash history, and saves files privately
under the ignored `logs/` directory. Set `KODI_LOG_ROOT` to use another location.
An omitted serial selects a connected Chromecast or discovers its wireless
**connection** service. Wireless pairing uses a separate port and temporary code.
Prerequisites: Bash, ADB, and GNU `timeout`/`sha256sum`.

`tools/kodi/check_apk_cache.py` runs **inside Kodi**, whose app identity can read
the protected extracted assets. Stage it in Kodi's external `temp` directory,
obtain `base.apk` with `adb shell pm path org.xbmc.kodi`, and invoke Kodi's
`RunScript(script-path,base-apk-path)` built-in. The report is written to
`special://temp/kodi-apk-cache.json`. The audit only reads bundled assets and
writes its report; it never repairs files or reads account settings. Six tests
cover interrupted extraction, truncation, checksum corruption, timestamp
precision, and path traversal. It checks the old directory timestamp logic;
it does not model newer Kodi versions' completion-stamp logic.

Raw logs, device databases, account XML, screenshots, and pairing credentials
are intentionally excluded from the commit. Logs may contain private media
URLs; review them locally before sharing.
