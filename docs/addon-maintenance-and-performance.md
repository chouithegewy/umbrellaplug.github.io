# Debrid add-on maintenance and performance

Research date: October 4, 2026. Umbrella source reviewed at upstream
`3baead15cc75e6a17a708e1a4f884342d2e58295` (6.7.90), with this fork's existing
cache fixes. Sources include official service documentation, add-on maintainer
repositories, and Kodi documentation. This is a representative history, not a
complete survey or a benchmark comparison. Kodi and the movie were left untouched.

## Recurring maintenance patterns

| Area | What changes over time | Examples from the reviewed history |
| --- | --- | --- |
| Debrid contracts | Removed capabilities, response shapes, authentication, limits and file states | Umbrella's November 2024 RD adaptations; later resolver concurrency limits |
| Metadata and tracking | List limits, pagination, watched/resume semantics, sync APIs | Umbrella's Trakt limit handling and SIMKL batch reuse |
| Providers and matching | Scraper endpoints, cloud filenames, episode packs, thumbnails | Fen's Offcloud cloud-scraper and Easynews thumbnail fixes |
| Kodi compatibility | Python runtime/API changes, info tags, skins, database versions | Umbrella's Kodi 20 metadata and resume changes |
| Local state and concurrency | Invalid caches, overlapping refreshes, settings snapshots, interpreter reuse | Cache fallback repairs and upstream progress-build coalescing |
| Distribution | Update logic, dependency versions, fork lineage and delayed integration | Fen updater revisions and Seren's still-open RD/AD repair PR |

The [Umbrella changelog at the reviewed revision](https://github.com/umbrellaplug/umbrellaplug.github.io/blob/3baead15cc75e6a17a708e1a4f884342d2e58295/omega/plugin.video.umbrella/fullchangelog.txt)
records RD adjustments in November 2024, Kodi 20 metadata adaptations in 2023,
and a three-worker RD semaphore in May 2026. Its September 2026 changes reduce
duplicate progress work, background refreshes, and per-show SIMKL requests.
The recurring performance direction is to remove redundant work and calls.

[Tikipeter's current release notes](https://github.com/Tikipeter/Tikipeter.github.io/blob/main/README.md)
show repeated downloader and sync-service changes, a shared download queue,
concurrency controls, cloud-scraper repairs and revised update discovery.
The official package inspected is **Fen 4.0.46**, ID `plugin.video.fen`, rather
than the old Fen Light 2.x distribution. Historical Fen Light records were not
independently recovered from the current repository; the Seren proposal below
explicitly credits a similar Tikipeter implementation. These lineages should not
be conflated or assigned one universal version number.

[Seren PR #952](https://github.com/nixgates/plugin.video.seren/pull/952), opened
November 25, 2024, proposes moving RD availability checks to source selection,
reducing calls during scraping, followed by pack-selection and cleanup fixes.
GitHub's API reports it **open and unmerged** at this research date. This is
evidence of maintenance work, not evidence that an official Seren release ships
it. Repository, branch and installed version matter as much as the add-on name.

## Real-Debrid has changed despite the same API URL version

A July 2024 [client-maintainer issue](https://github.com/rogerfar/rdt-client/issues/545)
reports that uncached availability responses changed shape, breaking
deserialization. Treat this as a reported observation, not an official API
changelog. Umbrella's November 6 notes independently record an empty-hash fix.

The major November 2024 change was loss of `torrents/instantAvailability`.
Umbrella records resolver changes on November 23 and 25; Seren's November 25
proposal documents adaptation. A [current debrid client design record](https://github.com/debridmediamanager/debrid-media-manager/blob/main/cast-alldebrid.md)
identifies the endpoint as disabled since November 22, 2024. The current official
documentation no longer lists it. Do not assume every search result can be
cheaply labeled cached before selection.

The [official RD API documentation](https://api.real-debrid.com/) still uses
`/rest/1.0/`, documents a 250-request/minute ceiling and HTTP 429, and says refused
requests count toward the limit. Numeric errors include 35 (infringing file),
36 (fair usage), and 37 (disabled endpoint). Authentication still uses device
authorization and refresh tokens. URL version stability does not guarantee
capability or response stability.

Design recommendation: model provider capabilities explicitly. Distinguish
unknown availability, known absence, authentication failure, throttling,
temporary outage and a disabled endpoint. Refresh credentials once when
appropriate; do not repeatedly retry a denial or a removed capability. Honor
`Retry-After` when supplied and otherwise use a bounded cooldown. A concurrency
semaphore bounds simultaneous work, but does not enforce requests per minute;
multiple clients sharing an account also consume the service's budget.

## Kodi API, runtime and binary ABI are separate

- **Kodi 19:** the Python 2 to Python 3 migration broke older Python add-ons.
  See [Kodi's migration announcement](https://kodi.tv/article/kodi-19-python-3-goes-live/).
- **Kodi 20:** metadata moved toward typed info-tag methods. `ListItem.setInfo`
  became partly deprecated, and `xbmc.translatePath` was removed; the supported
  replacement is in `xbmcvfs`. See [the Python API v20 changes](https://xbmc.github.io/docs.kodi.tv/master/kodi-dev-kit/python_v20.html).
- **Bundled Python:** the reviewed [Kodi 21.2 build pins 3.11.7](https://github.com/xbmc/xbmc/blob/21.2-Omega/tools/depends/target/python3/PYTHON3-VERSION);
  [22 RC1 pins 3.14.6](https://github.com/xbmc/xbmc/blob/22.0rc1-Piers/tools/depends/target/python3/PYTHON3-VERSION).
  These are build definitions, not a claim about every distribution's system
  Python. The `xbmc.python` dependency version in an add-on manifest denotes
  Kodi's add-on interface, not the interpreter's exact version.
- **Binary ABI:** compiled input-stream, PVR and other native modules must match
  the host interfaces and platform/architecture. Umbrella's Python source has no
  direct C++ binary ABI to rebuild, although its native dependencies can have one.
  Kodi explains this in its [binary add-on distribution design](https://kodi.tv/article/kodi-v18-binary-add-ons-repository/).
  Its [2025 DevCon report](https://kodi.tv/article/devcon-2025-tirana-part-i/)
  also describes 21.2 ecosystem changes affecting older installations.

Test the supported Kodi/Python combinations and the real ARM device. A desktop
mock test verifies isolated behavior; it cannot validate Kodi GUI lifecycle,
native crashes, binary module compatibility or Android memory pressure.

## Concrete opportunities in this Umbrella fork

The following are source findings and proposed changes, not measured speedups.

| Priority | Observed code | Proposed change and intended benefit |
| --- | --- | --- |
| 1 | `service.py`: `AddonCheckUpdate.run()` uses `requests.get` without a timeout; `main()` calls it before autostart | Open the requested initial menu after essential local setup; schedule optional update/account work afterward. Give every network operation a deadline. An offline update server should not block the first menu. |
| 1 | `realdebrid.py`: `_post()` recursively calls itself after an authentication error without a retry cap | One refresh attempt and one retry, then a typed failure. Test repeated invalid-token responses and refresh failures. |
| 1 | `realdebrid.py`: authorization, downloads and token requests omit timeouts; `_get()` allows five configured retries with a 45-second timeout | Use connect/read limits plus an overall operation deadline and cancellation. A Requests timeout is not a total wall-clock budget. |
| 1 | `realdebrid.py`: `refresh_token()` logs a refresh credential; tokens are placed in request URLs | Redact credentials at the logging boundary, remove credential-bearing debug messages, and prefer documented bearer-header authentication. |
| 1 | `tmdb.py`: SSL-error fallbacks use `verify=False` and omit the original timeout | Preserve certificate validation and report the certificate problem; retain deadlines on every path. |
| 2 | `tmdb.py`: start one batch of threads, join all of them, then start the next | Use a bounded work queue so one slow item does not keep the next batch idle. Maintain cancellation and safe session ownership. |
| 2 | Full metadata and optional fanart processing happen before a directory completes | Serve usable cached/base metadata first. Fetch optional enrichment later and refresh only when the relevant browsing window is idle. Avoid refreshes during playback. |
| 2 | RD has a three-worker semaphore; provider code is spread across several methods | Centralize bounded request scheduling, refresh coordination and normalized provider results. Fetch or resolve only what the current operation needs. |
| 3 | `cache.py`: journaling and synchronization are disabled; reads may wait up to 60 seconds; cache values use Python literal parsing | Profile short transactions and WAL where supported; bound waits and recover disposable cache entries independently. Introduce a versioned codec with an explicit migration. Preserve durable watched/resume state separately. |

The relevant files are [startup service](../omega/plugin.video.umbrella/service.py),
[RD client](../omega/plugin.video.umbrella/resources/lib/debrid/realdebrid.py),
[TMDb indexer](../omega/plugin.video.umbrella/resources/lib/indexers/tmdb.py),
and [cache database](../omega/plugin.video.umbrella/resources/lib/database/cache.py).
[Requests documents pooling, TLS verification and timeout semantics](https://requests.readthedocs.io/en/latest/user/advanced/);
[SQLite documents the tradeoffs of disabled journaling](https://www.sqlite.org/pragma.html#pragma_journal_mode).
Changing storage settings should follow crash/concurrency tests rather than a
global switch applied equally to cache and user history.

Keep the efficiencies already present: persistent HTTP sessions, TMDb
`append_to_response`, metadata caches, progress-build coalescing and SIMKL batch
reuse. The next improvement should be validated against those existing paths.
More threads can increase throttling, SQLite contention and memory use on the
Chromecast. No cross-add-on performance ranking is supported by this research.

## Measure distinct stages and make failures reviewable

Record timings for native Kodi startup, service-to-menu startup, cold/warm list
loading, source discovery, resolution and player start separately. For each,
record request count, cache hit rate, duplicate jobs, database waits, timeouts,
and median/tail latency. Do not log account tokens or signed playback URLs.
Once the user is finished watching, compare the same searches and lists on the
same device with warm and cold caches and controlled network failures.

Useful regression cases include a removed endpoint, empty array/object/null,
malformed JSON, expired token, repeated refresh failure, 429, HTML maintenance
responses, a slow backend, locked/corrupt cache, cancellation and profile switch.
Fixture responses must be sanitized; service-contract tests should be optional
and use an authorized test account, rather than mutate the user's cloud.

Keep compatibility helpers at the boundaries for Kodi metadata, provider
responses and cache serialization. Use versioned fork packages and tracked
upstream merges so fixes survive updates. The existing source branch includes
20 cache/audit regression tests and the earlier repair; the larger network,
scheduling and serialization changes above remain recommendations.

The previously investigated Android extraction race occurs before Umbrella
Python runs. Its upstream Kodi fixes are recorded in
[the Chromecast investigation](chromecast-troubleshooting.md). It requires a
host Kodi change; faster Python menu loading cannot repair that native launcher.
