# Changelog

Notable changes to *grazr*, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `MODEL_LIMITS=1` watches per-model weekly limits too. Some plans cap a model
  on its own, and that cap runs out while the all-models week still has room.
  Claude leaves it out of the status line, so *grazr* never saw it and a pane
  on that model hit the wall. With the setting on, the detached process that
  makes the swap also asks Claude's usage endpoint for the live account's
  per-model limits, at most every two minutes. Such a limit counts only while
  a pane is on that model. It is off by default, since the endpoint is
  undocumented.

## 0.3.7 - 2026-09-25

### Fixed

- Saving the login you are on no longer races a swap. It read the credential
  and the account it belongs to one after the other, and a swap landing in
  between filed one account's credential under the other's name. While a swap
  is running it now refuses and asks you to try again.
- A pane still showing an old usage window can no longer hide a low reading.
  Its figures could replace the current window's just before the decision ran,
  and that decision then let the account run on. The window on record now
  stays until a newer one arrives.
- A login you never enrolled says why *grazr* does not rotate off it. There is
  nowhere to keep its reading, so the decision never saw one, and it stayed
  put without a word. It now says so once in the log and as a notification.
  The status screen no longer claims it rotates away on its own, and points
  you to the swap key or to enrolling it.

## 0.3.6 - 2026-09-24

### Added

- Every swap says what it handed over: whether the arriving credential's token
  had lapsed while it sat parked, and when the refresh behind that token runs
  out. Claude renews a lapsed token on its next request, but a renewal that
  fails signs the account out, and these two times are the only record of what
  *grazr* installed.
- Claude signing you out goes in the log. A reading with no account behind it
  means Claude has signed out, and *grazr* saw that and dropped it.

### Fixed

- The log keeps every swap. A line the same as the one before it was dropped,
  and swaps run between the same two accounts, so a second swap read exactly
  like the first and never appeared at all. A standing situation, such as a
  Claude lock that is busy or a dry run, still takes one line, since those
  repeat on every message of every pane.

## 0.3.5 - 2026-09-24

### Fixed

- The wait *grazr* names is now the one that ends it. "Every account is below
  your thresholds" and "nothing to swap to" quoted the first window to reopen
  anywhere, even one that still had plenty left, so you were told to come back
  in an hour while the weekly window you were really behind had three days to
  go. Only windows below your thresholds count now, and an account behind two
  of them is named for the later one, since the first to reset still leaves it
  short.
- The swap key no longer names the account you are already on. It never swaps
  to that one, so its reset said nothing about when the key would work.

### Changed

- Both messages say which account frees up first and which window is holding
  it, as in "earliest reset Thu 09:00 (work, weekly window)". They gave a bare
  time before, and a five-hour wait and a weekly one read the same.

## 0.3.4 - 2026-09-13

### Fixed

- No more "cannot read Claude's usage" at a weekly reset. Right at a reset the
  usage block is missing for a turn, and with several sessions crossing the
  line together, 0.3.3 counted those single misses as one version going blind.
  The warning now follows a single session. It fires only when one session
  completes two turns and never once gets usage, which is what a dropped field
  looks like and a reset never does.

## 0.3.3 - 2026-09-10

### Fixed

- No more "cannot read Claude's usage" after a restart or a Claude update. A
  completed turn can come back with no limits block for a turn right after a
  session resumes, and grazr read that one gap as the field being gone. It now
  waits: the first gap arms the warning, a reading that does carry limits
  clears it, and only a version that keeps missing them trips it.

## 0.3.2 - 2026-09-09

### Fixed

- No more false "every account is low" right after a swap. A session keeps
  reporting the account it left until its next request, and grazr counted that
  leftover reading against the account it had just moved to when the last
  message before the swap ticked the old account a shade below the figure grazr
  parked. The reading's reset time already says whose it is, so grazr now
  trusts that over the headroom when the two accounts' windows reset at
  different times.

## 0.3.1 - 2026-09-08

### Fixed

- A rotation no longer brings back the toast that says a Claude session sends
  no usage. Each rotation wiped the record of having shown it, and a pane
  resumed days ago and idle since re-runs the status line every minute with
  no limits to report, so the toast was back within a minute of every swap.
- A swap on demand goes to `grazr.log` like an automatic one. Only Herdr's
  plugin log had it.
- A swap on demand that one of Claude's locks refuses says so on screen and in
  a toast, the way having nowhere to go does. The refusal reached only the
  plugin log.

### Changed

- Messages are written as sentences, with a capital letter first and no
  semicolons.

## 0.3.0 - 2026-09-07

### Added

- *grazr* reads usage from Claude's status line. After every message Claude
  hands that command the session's five-hour and weekly usage. *grazr* sits in
  the command, shows your own status line unchanged, keeps the reading, and
  swaps within one message when a threshold is crossed. The swap runs in a
  separate process, so Claude cancelling the status line for the next update
  cannot leave it half done. Before this, *grazr* read a cache that could sit
  still for an hour while a window drained, which is how panes kept hitting
  the wall.
- Enrolling connects the status line. Two actions connect and disconnect it by
  hand. When a Claude pane starts and the status line is no longer *grazr*'s,
  a toast says so once.
- A toast, once per Claude version, when a session that has talked to the API
  sends no usage in its status line. A Claude release that renames the field
  is noticed the same day.
- Every decision goes to `grazr.log` in the state directory, with a timestamp.
- A sidebar tag. *grazr* publishes the active account to every Claude pane as
  a `$grazr` token, set when Claude starts in a pane and refreshed after every
  swap. Herdr shows a token only where your own sidebar row names it, so the
  README has the row to add. A pane you have scrolled up in is skipped, since
  repainting a pane's metadata can jump it to the bottom.

### Removed

- `LIVE_USAGE_BELOW` and the call to the undocumented usage endpoint. The
  status line makes both unnecessary. A `config.env` that still has the
  setting is refused with a line naming it.
- The refusal to rotate away from a restricted account. The restriction came
  from the endpoint, which is gone, and Claude shows the restriction itself.

### Fixed

- A swap no longer signs you out of your MCP servers. Their logins live in the
  same store as the account credential and belong to no account, so they now
  stay put while the login around them changes. When carrying them makes the
  credential too long for the macOS keychain, the swap goes ahead without them.
- A swap interrupted half way is finished by the next attempt. It no longer
  parks the arriving credential over the outgoing account's.
- A swap no longer leaves the account you left behind in Claude's config. Its
  `/login` API key stayed there and could be spent by the account you moved
  to, and its plan and model caches described the wrong account until Claude
  asked again.
- An `apiKeyHelper`, `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in
  `settings.json` puts Claude on API-key auth, so a swap under it changes
  nothing. *grazr* used to report those swaps as done. It now stops and names
  the setting to unset.
- A swap says when the account it moved to had a token that expired while it
  was parked. Claude refreshes one on its next request, but a refresh token
  already spent elsewhere makes that fail and signs the account out.
- When no account is above your thresholds, *grazr* says they are low rather
  than spent. They still serve requests.
- The panes are tagged after the rotation lock is let go, not while it is held.
  Two Herdr calls per pane, capped at five seconds each, would stall every
  other pane's swap.
- The files *grazr* shares between panes are put in place in one step. A plain
  write empties a file before it fills it.
- The keychain is reached at `/usr/bin/security` rather than by name, so a
  `security` planted earlier on `PATH` is never handed a credential.
- A credential the keychain will not hold, and a line in `config.env` that
  will not parse, print one line saying so instead of a stack trace.
- An account file that is valid JSON but not shaped like an account is skipped
  instead of stopping the decision for every account.

### Internal

Housekeeping, none of which changes what *grazr* does.

- Two modules split out of files that held more than they said. `accounts.py`
  keeps the accounts *grazr* enrolled, which are its own files rather than
  Claude's, and `atomic.py` keeps the single way a file is put in place.
- An entry point is handed what it works on instead of building it from the
  environment. A `Runtime` carries the paths, the credential store, the state
  directory and the config, so a test cannot reach the real keychain by
  forgetting a variable.
- Every open situation *grazr* has reported is kept, with whether its toast
  appeared, so two situations cannot evict each other and toast in turns.
- A test pins the agreement between the keychain timeout and the lock stale
  ages. Raising either alone would let a swap lose a lock mid-write.

## 0.2.0 - 2026-09-04

### Added

- A `swap` action you can bind to a Herdr key. It moves you to the first
  account in `ACCOUNTS` that still has room, without waiting for the
  threshold. `DRY_RUN` still applies. `ENABLED` does not, because that flag
  only turns off the automatic switch. If no other account has room, the key
  shows a toast that says so and tells you when the first window opens again.

### Changed

- Reset times in toasts and in the log are shown as a local weekday and time,
  like "Fri 01:00", instead of a UTC timestamp.

## 0.1.1 - 2026-09-04

### Changed

- The manifest description now says what grazr does in the words people
  search for: automatic account switching at the usage limit.

### Fixed

- A live usage reply in an unexpected shape now reads as "unknown" instead of
  raising in the hook. The cached reading already did.

## 0.1.0 - 2026-09-04

First release.

### Added

- Rotation to a fresh account when the active one drops below the configured
  headroom, decided on every Claude pane's turn end.
- A credential swap that moves the identity with it, under the same locks
  Claude uses for its own config and token refresh, so a refresh in flight can
  never land on a half-finished swap.
- Parked credentials in the macOS keychain, or in owner-only files on Linux.
  No secret is ever written to a command line or a log.
- A live usage check for when Claude's cached reading is too old to trust,
  because that copy can sit unrefreshed long enough for a window to run dry
  unseen. `LIVE_USAGE_BELOW` sets the headroom it starts asking at, and `0`
  turns it off.
- A refusal to rotate away from an account the server has restricted, since
  leaving would be circumventing the restriction.
- Panes to enrol an account and to read what each one has left, plus a Herdr
  toast when a swap happens.
- `DRY_RUN`, which logs the decision and swaps nothing.
