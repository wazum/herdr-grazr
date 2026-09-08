# Changelog

Notable changes to *grazr*, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Fixed

- A rotation no longer brings back the warning that a Claude session sends no
  usage. Every rotation wiped the record of having shown it, and a pane
  resumed days ago and idle since re-runs the status line every minute with no
  limits to report, so the toast came back seconds after every swap.
- A swap on demand is written to `grazr.log` like an automatic one. Only
  Herdr's plugin log had it.
- A swap on demand that one of Claude's locks refuses says so on screen and
  in a toast, the way having nowhere to go does. The refusal reached only the
  plugin log.

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
