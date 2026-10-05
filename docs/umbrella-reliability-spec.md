# Umbrella reliability and performance implementation spec

Status: proposed implementation; this document does not change runtime behavior.
Date: October 4, 2026. Baseline: Umbrella 6.7.90 and this branch's existing cache
repairs. The primary deployment target is the Google Chromecast running Kodi
21.2 / Python 3.11.7. Background and upstream evidence are in
[the maintenance investigation](addon-maintenance-and-performance.md).

## Intended behavior

On every fresh Kodi launch, Umbrella opens when its existing autostart setting
is enabled. Update servers, account checks and optional synchronization cannot
hold up that launch. Browsing displays usable metadata promptly, preserves
ordering and watched filters, and avoids duplicate enrichment work. Provider
failures finish with a useful result instead of recursion, indefinite waiting,
or an empty list that conceals a network error. A damaged disposable cache can
recover independently of watched history, bookmarks, settings and credentials.

Implementation and device validation must preserve the current instruction:
do not stop, restart, reload profiles or navigate away from the movie now playing.
Cold-start and playback tests belong to a later idle-device validation session.

This scope covers Umbrella's service, RD client, TMDb indexer, disposable caches
and logging. It does not upgrade Kodi, repair native Android extraction, restore
removed provider endpoints, override Kodi's watched filter, or deploy a package
to the Chromecast. Other debrid clients can adopt the transport interface later;
their authentication and mutation semantics require separate adapters and tests.

## Delivery order

Each milestone is independently reviewable and retains the existing cache
regression tests. A later milestone depends on the earlier transport and
measurement contracts rather than introducing another retry layer.

| Milestone | Change | Completion evidence |
| --- | --- | --- |
| M0 | Add timing/counter instrumentation and sanitized logging | Repeatable baseline; synthetic credentials absent from every owned log sink |
| M1 | Separate local startup from optional work; bound update requests | Offline update/account services do not delay autostart; no playback navigation |
| M2 | Add HTTP policy and migrate all RD/TMDb request paths | Deadline, authentication, throttle, TLS and mutation failure tests pass |
| M3 | Replace metadata batches with a bounded queue; cache-first directory completion | Stable lists with a slow item; fewer duplicate calls; cancellation/profile tests |
| M4 | Add versioned disposable caches and bounded coordination | Codec, contention, migration, corruption and process-crash tests pass |
| M5 | Validate supported runtimes and package the fork | Device measurements, clean package, rollback and upstream reconciliation documented |

M0–M2 address hangs and failure handling first. M3–M4 are performance/storage
changes and require measurements before becoming defaults. The numbers below
are proposed initial configuration, not measured performance claims. Tuning
requires recording the changed value and rerunning the affected acceptance cases.

## R1: launch independently of optional network work

Modify [service.py](../omega/plugin.video.umbrella/service.py) into three stages:

1. **Essential local initialization:** initialize settings and monitoring;
   publish a fresh settings snapshot; establish the active profile and local
   cache schema/version requirements. Split version migrations into required
   schema preparation and deferred disposable-cache cleanup. Neither stage
   may clear watched history, bookmarks or accounts as a side effect.
2. **Launch:** read `umbrella.autostart` from the fresh settings snapshot and
   invoke `RunAddon(plugin.video.umbrella)` once after Kodi's GUI/profile is ready.
   Optional HTTP calls are not dependencies of this stage.
3. **Optional work:** schedule update discovery, premium-account notifications,
   library work and tracking sync using bounded jobs and their existing settings.
   Notifications and refreshes wait until browsing is idle and playback is absent.

Audit `SyncMyAccounts`, `VersionIsUpdateCheck`, and `ReuseLanguageInvokerCheck`
before moving them. They include settings writes, dialogs and, in the invoker
check, `LoadProfile`. Keep required local settings normalization before launch;
defer explanatory dialogs. An invoker-setting mismatch must be recorded for
explicit application when idle; startup must not automatically reload a profile.

Use a Kodi-session launch marker that survives a service restart and expires
when Kodi exits. Do not use a property that normal settings initialization
clears. A settings edit, interpreter reuse, profile change, or service restart
must not reopen Umbrella over another activity. A fresh Kodi session with
autostart disabled must not launch it. If startup finds playback already active,
skip navigation for that session and record the reason; do not interrupt the movie.

Wait for GUI readiness using the monitor's cancellable wait, initially at
100 ms intervals with a 30-second readiness budget. Expiry produces a diagnostic,
not a permanent worker or a delayed launch over later user navigation. Fetching
the first directory can still require the directory's own metadata request;
separating launch does not promise an offline cold-cache catalog.

Acceptance: with the GUI ready, valid local settings, and no migration pending,
the service requests launch within a proposed p95 target of 1 second on the
Chromecast. A never-answering update/account server adds no launch-path HTTP
calls. Native Kodi startup time is measured separately.

## R2: one HTTP policy with explicit outcomes

Introduce `resources/lib/modules/http_client.py` and a small operation context
shared by callers: operation ID, monotonic deadline, cancellation signal,
profile/settings generation and priority. Preserve existing public return types
through adapter functions until each caller is migrated and tested.

The internal result includes outcome, parsed payload, HTTP/provider error code,
attempt count and optional retry time. Outcomes are `ok`, `known_empty`,
`not_found`, `auth_required`, `rate_limited`, `unavailable`, `capability_disabled`,
`denied`, `invalid_response`, `timeout`, `cancelled`, and `outcome_unknown` for a
mutation whose remote completion cannot be established. Empty JSON containers,
HTTP 204, JSON null and a transport failure must not collapse into one value.
Validate response shape at the provider boundary, not with substring searches
through arbitrary response text. Do not cache a failure as a successful empty list.

| Operation | Initial overall wait budget | Initial per-attempt limits |
| --- | --- | --- |
| Update discovery | 5 s | Connect 3 s; read at most 3 s |
| Interactive metadata page | 15 s | Connect 3 s; read at most 8 s |
| Optional enrichment/account check | 10 s | Connect 3 s; read at most 5 s |
| RD source resolution | 45 s | Connect 3 s; read at most the remaining budget, up to 45 s |
| Manual device authorization | Provider expiry, bounded by user cancellation | Connect 3 s; read at most 5 s; respect provider polling interval |

Pass the remaining operation budget through pagination, polling, retries,
coordination waits and token refresh. Cap each attempt by that remaining budget.
Use monotonic time for in-process budgets. Requests' connect/read timeouts are
not a hard wall-clock deadline: DNS and a trickling response can outlast them.
The caller must stop waiting at its operation deadline without joining a worker
indefinitely. An in-flight Python request may finish later; bound worker count,
stop further dispatch, and reject late UI/settings/cache updates from cancelled
or obsolete contexts. A remote mutation may already have happened and requires
reconciliation, not a claim that cancellation undid it.

Own sessions per worker; reuse connections within that worker and close sessions
on shutdown. Apply the same policy to both configured RD hosts and all TMDb
hosts, including OAuth, download pagination, deletion, token refresh and error
fallbacks. Remove independent adapter retry loops (`max_retries=0`) so the
operation policy accounts for every attempt. Default to at most three wire
attempts for one logical request, including refresh/replay; a paginated or
polling operation also has its shared deadline and provider rate budget.
Keep the longer RD pack-read allowance initially because the existing client
explicitly records pack timeouts at shorter limits. Bound the whole resolution
instead of multiplying that allowance by independent adapter retries.

Retry safe reads on selected transient connection failures or HTTP 502/503/504
with capped exponential backoff and jitter, initially 250 ms–2 s. Honor
`Retry-After` if present; if the wait exceeds the remaining budget, return a
retry time instead of sleeping beyond it. Certificate errors, malformed payloads,
confirmed denials, disabled capabilities and ordinary client errors are terminal
for the current operation. POST/DELETE replay requires the operation's provider
adapter to establish that replay is safe; HTTP method alone is insufficient.

Retain certificate validation on every path; remove TMDb `verify=False`
fallbacks. Return a certificate/connectivity failure through the normal result
path. Limit buffered JSON responses to an initial 2 MiB; over-limit responses
are `invalid_response`. Media downloads are outside this JSON client contract.

User-facing failures should explain the action available: reconnect an account,
retry after a cooldown, or check connectivity. A successful zero-match search
continues to use the ordinary empty-search view. Do not expose token contents,
raw HTTP bodies or implementation detail in notifications.

## R3: Real-Debrid authentication, capabilities and side effects

Migrate [realdebrid.py](../omega/plugin.video.umbrella/resources/lib/debrid/realdebrid.py)
to the HTTP policy while retaining existing file matching and cloud preferences.
Introduce a separate coordination database in this milestone for refresh leases
and rate accounting. Use the short transactions, safe journal modes and bounded
waits in R5 from its creation; the v2 cache migration can follow independently.

- Use documented bearer authentication in request headers. Remove tokens from
  constructed request URLs and remove the refresh-credential debug message.
- Refresh credentials at most once for a logical request after an explicit
  expired/invalid-token response. Permit at most one authenticated replay.
  Repeated invalid-token responses return `auth_required`; `_post` must not
  recursively refresh. Do not refresh for every generic `Bad Request` response.
- Coordinate refreshes across Umbrella invocations using a short-lived account
  lease in a separate coordination database. A follower rereads native account
  settings after the owner finishes. The owner checks profile and credential
  generation before persisting, so logout/account switching wins over a late
  response. Do not store credentials in the coordination database or erase
  settings on a timeout, 429, maintenance response or malformed JSON.
- Maintain a shared rolling request budget across Umbrella invocations,
  initially 180 RD REST requests per minute with a ceiling of the documented
  250. Count unsuccessful attempts and authenticated replays. OAuth requests
  also pass through the scheduler; their polling follows the OAuth contract.
  This budget cannot account for other apps using the same account. A 429 sets
  a shared cooldown, initially 30 seconds when the response supplies no retry
  time; no retry storm during that cooldown. Coordination records use opaque
  local account keys and contain no tokens, playback URLs or raw request bodies.
- Preserve the three-job RD resolver limit per interpreter. Separate concurrency
  from rate accounting; neither replaces the other. Waiting for a slot is
  cancellable and charged to the operation budget.
- Represent cache availability as `unknown`, `available`, or `unavailable`.
  The adapter declares instant-availability capability disabled; ordinary scrape
  flows do not probe it. A disabled endpoint response suppresses further calls
  for that capability until the adapter's capability version changes. Unknown
  availability must not remove an otherwise valid source. Resolve only the
  selected source; denied files remain denied.
- Track resources created by the current resolver. Clean up only confirmed
  owned temporary resources and honor the user's cloud-storage preference.
  Never delete a pre-existing cloud item. An ambiguous add-magnet timeout must
  not cause a blind second add; reconcile through bounded reads when ownership
  can be established, otherwise return `outcome_unknown`. The same rule applies
  to file selection, unrestriction, deletion and token requests according to
  their individual contracts.

Acceptance: two invocations with an expired token perform one coordinated
refresh; repeated invalid-token responses terminate within the budget. A shared
429 stops both invocations from dispatching REST calls during cooldown. Timeout
after a mutation creates neither an automatic duplicate nor an unowned deletion.

## R4: metadata queues and safe directory completion

Replace the thread batches in
[tmdb.py](../omega/plugin.video.umbrella/resources/lib/indexers/tmdb.py) with
a reusable work queue, initially four workers, with at most one page of pending
enrichment per invocation. A completed job immediately frees a slot; one slow
item cannot leave other available workers idle. Bound configuration to 1–8
workers; retire the developer-only unlimited batch behavior explicitly. Preserve
existing connection reuse, `append_to_response` and tracking batch reuse.

Introduce `resources/lib/modules/background_jobs.py`; the persistent service
owns optional enrichment workers. Browsing invocations submit bounded jobs to
the coordination database rather than relying on threads surviving plugin exit.
Jobs contain metadata IDs and generation/scope fields, never credentials or
signed URLs. Bound queued work to two pages per profile, discard obsolete-route
jobs first, and drop optional jobs when that queue is full. If the service is
unavailable, complete the directory and defer enrichment until a later visit.
Do not spawn replacement worker pools on every navigation. Foreground jobs
retain their caller deadline and take priority over optional jobs.

Build list entries from the list response and valid cached metadata first.
Core title/IDs, playable route, media type and required sort/filter fields must
be correct before completing the directory. Apply current watched/resume state
separately; do not serve stale user state as optional descriptive metadata.
Missing credits, expanded art or fanart must not prevent a usable entry.

Kodi may not display entries until `endOfDirectory`, so merely adding entries
early is insufficient. Complete the basic directory, then enqueue eligible
enrichment. Worker threads may update cache records; GUI refresh must use the
Kodi invocation path, never mutate a completed list from arbitrary workers.
Associate each job with profile, settings generation, route and directory
generation. Ignore stale completions after navigation, profile change or abort.

Coalesce enrichment for the same metadata key. Cache keys include provider,
media identifiers, locale, applicable account/profile scope and schema version.
Optional stale descriptive metadata may be used only under an explicit bounded
policy; introduce no additional stale window in M3. Preserve the existing TV
airing/episode freshness rules. Move hidden
tracking-sync side effects out of `metacache.fetch` into scheduled jobs. A cache
read must not silently become an unbounded account-sync operation.

Refresh only the same visible directory when playback and modal dialogs are
absent and the user is idle. Permit at most one refresh for that directory
generation; cache hits on the refreshed invocation must not create a refresh
loop. Preserve selected media identity, sort order, pagination and context menus.
If a sort/filter requires missing enriched fields, obtain those fields within
the foreground page budget rather than later silently reordering the list.
If safe selection-preserving refresh cannot be demonstrated for a Kodi/skin
combination, use the enrichment on the next natural visit instead.

Retain Kodi's watched-filter choice. The earlier movie-search incident returned
items that Kodi's Watched view hid; a populated API response is insufficient
acceptance evidence. Validate actual visible results in All Videos and Watched
views, without changing the user's filter as an automatic repair.

Acceptance: a fast item behind a stalled item starts as soon as any queue slot
is free; pending jobs and workers remain bounded. Titles, routes, sort/filter
semantics and current watch state remain correct. Enrichment never refreshes
over playback and cannot update a different profile or directory.

## R5: disposable caches, migration and coordination

Inventory callers of
[cache.py](../omega/plugin.video.umbrella/resources/lib/database/cache.py) and
[metacache.py](../omega/plugin.video.umbrella/resources/lib/database/metacache.py)
before migration. Record which values are disposable metadata, successful
negative results, progress snapshots or durable user state. New stale-serving
policies apply only to explicitly registered descriptive-metadata operations.

Create separate `cache-v2.db` and `metacache-v2.db` files. Store schema version,
namespace/key, result kind, creation/expiry times and a versioned payload.
Use a JSON codec with explicit tagged nodes for tuples and bytes, and an
unambiguous encoding of dictionaries so ordinary user keys cannot impersonate
codec tags. Preserve nested values and return types required by existing callers.
Unsupported values bypass caching and still return the fresh function result.
Initial limits are 2 MiB per encoded value and 64 nesting levels; an over-limit
fresh value is returned without being cached. Maintain intentional negative-cache
semantics independently of transient errors, and retain the current fresh-result
and last-season/unwatched regression behavior.

On a v2 miss, eligible legacy records may be read once with bounded
`ast.literal_eval`, validated and rewritten to v2. **Never use `eval`**, including
the existing `metacache.fetch` path. Unreadable, over-limit or unsupported legacy
records become cache misses. Avoid an eager duplicate of the whole cache on
the nearly full device. Durable history/bookmark/account stores are not migrated
by this milestone; their access and contents must remain intact.

Use WAL with `synchronous=NORMAL` for the new disposable databases after checking
that the profile filesystem supports it. Verify the returned journal mode;
fall back to DELETE journaling with `synchronous=FULL` if WAL is unavailable.
Never use journal/synchronization OFF. Keep read transactions short and write
transactions limited to one bounded batch. Initial contention limits are 50 ms
for a read and 100 ms for a write attempt, with a total foreground database-wait
budget of 200 ms. A busy read becomes a cache miss; a busy write skips caching.
If coordination fails, skip optional jobs and fail closed for refresh ownership
and rate admission; do not bypass the limiter or start competing token refreshes.
Close every cursor/connection on success, failure and cancellation. Do not copy
the existing 30-billion-byte mmap hint into the ARM configuration; it describes
a mapping limit, not a guaranteed RAM allocation. Leave mmap disabled initially
and tune only with device evidence.

Recover an unreadable row by removing that row. On confirmed disposable database
corruption, close connections and rebuild only that cache under coordination;
keep at most one bounded diagnostic backup if disk space permits. Disk-full or
read-only failures bypass the cache without crashing browsing. Do not reset the
entire Kodi app, settings, watch databases or another add-on's profile.

Replace indefinite coalescing waits with a coordination lease containing owner,
generation and expiry. Acquisition/release uses short transactions in a separate
database; no main-cache write lock is held across HTTP. Followers wait only to
their operation deadline. An expired owner's late result cannot overwrite the
new owner's result: writes require the current generation. Detect obsolete
leases across process death and clock changes; use monotonic waits locally and
bounded persisted expiry validation across processes.

Rollback: switch readers back to the retained legacy files; v2 files are
disposable. Do not retire the legacy reader/files until one release has passed
device validation and rollback. This is a cache rollback, not a rollback of
credential changes or watched-state changes made by the user.

## R6: measurements, compatibility and release gates

Route Umbrella-owned logging through sanitization, including `control.log`,
`log_utils.log_force`, normal/error logs and logging-failure fallbacks. Remove
raw traceback printing on these paths. Never emit access/refresh tokens, pairing
codes, passwords, authorization headers, signed media URLs or response bodies.
Log provider and endpoint templates instead of full query strings. Test embedded
credentials in exceptions as well as structured fields. Third-party/native
Kodi logs are outside this boundary and must not be claimed as covered.

Collect operation duration, request/retry counts, cache hits/misses, coalesced
jobs, queue peak, database waits, timeout/cancel outcomes and startup stage
times. Use aggregate counters and bounded diagnostic summaries; do not log
titles/search strings or other account content for performance measurement.
Instrumentation must work with debug logging disabled and avoid unbounded files.

Add meaningful tests under the existing test layout, with fixture/local HTTP
responses and real temporary SQLite databases:

| Area | Required scenarios |
| --- | --- |
| Startup | Autostart true/false; update/account hang; settings refresh; service restart; GUI readiness expiry; playback active; invoker mismatch without `LoadProfile` |
| HTTP | Connect/read failure; trickling response; bounded caller wait; retry accounting; cancellation; profile change; TLS failure without insecure retry; empty/null/204/malformed/HTML/over-limit response |
| RD | One refresh across invocations; repeated auth error; account change during refresh; 429/cooldown; disabled capability; denial; ambiguous mutation and owned cleanup |
| Metadata | Slow first item; duplicates; route/profile changes; stable ordering and pagination; required sort fields; latest watched/resume state; no refresh during playback |
| Cache | Nested tuple/bytes round-trip; tag collision; legacy decode without execution; negative cache; fresh empty results; locked/read-only/full/corrupt DB; expired lease and stale owner |
| Durability | Concurrent processes; process killed during a cache write; restart/recovery; preserved history/bookmarks; rollback to legacy reader |
| Logging | Synthetic secrets in URLs, headers, messages, exceptions and logging-failure paths absent from every Umbrella-owned sink |

M2–M4 require concurrency tests in separate processes, not only threads. Do not
use a real account or mutate the user's cloud in automated tests. Live contract
checks, if later authorized, use a dedicated account and controlled owned data.

The required integration target is Kodi 21.2 on this Chromecast. Test pure Python
components on 3.11 and 3.14; Kodi 22/Python 3.14 GUI support remains a candidate
until tested in Kodi itself. Inventory other supported Kodi/dependency versions
before implementation and preserve the existing declared compatibility range;
any reduction requires an explicit release decision. Use capability checks at
Kodi metadata boundaries and avoid requiring a newer Requests/urllib3 than
Kodi's installed dependencies without a manifest and compatibility review.

For device performance validation, capture at least 20 repetitions of the same
startup and representative 20-item movie/TV lists for baseline and candidate,
separating warm and cold disposable caches. Record p50/p95, call counts, queue
peaks and memory. Never obtain a cold cache by clearing Kodi app data or removing
watched history. Before M3 becomes default, require at least a 20% p95 reduction
on a list previously blocked by optional enrichment, no more than a 10% p95
regression on other measured paths, and no increase in warm-list HTTP calls.
These are acceptance targets to validate, not promised measured improvements.

All failure tests must terminate within their configured caller budgets;
background completions must cause no late navigation, refresh, credential
overwrite or stale-generation cache write. Device playback validation must show
no automatic navigation, dialogs or enrichment refresh while video plays.

Package only after those gates pass: use an identifiable fork version, update
repository/package metadata together, record the upstream commit and reconciled
changes, and inspect the archive for accidental logs/settings/test credentials.
Document source and package rollback separately. Commit and push reviewable
milestones; device installation and cold-start validation are separate actions
that must respect the ongoing playback constraint.
