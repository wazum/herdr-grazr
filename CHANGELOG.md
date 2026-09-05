# Changelog

Notable changes to *grazr*, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- A sidebar tag. *grazr* publishes the active account to every Claude pane as
  a `$grazr` token, set when Claude starts in a pane and refreshed after every
  rotation. Herdr shows a token only where your own sidebar row names it, so
  the README has the row to add. A pane you have scrolled up in is skipped,
  because repainting a pane's metadata can jump it to the bottom.

- Fewer calls to the undocumented usage endpoint. *grazr* never asks when no
  other account could take over, and it weighs the age Claude stamped on a
  reading against the pace the last two imply, so a stale-looking number earns
  a call while a fresh one above the mark does not. The estimate decides only
  when to ask. A swap is still made against a reading.

### Changed

- `REMAINING_SESSION` now defaults to 30 and `LIVE_USAGE_BELOW` to 45. Claude's
  cached reading runs behind the window it describes, and 15 left no room for
  that lag plus the wait for the next check. The gap between the two marks is
  the band *grazr* watches in, so `LIVE_USAGE_BELOW` sits above the thresholds.
  Both are still yours to set.

### Fixed

- The two files *grazr* shares between panes are put in place in one step. A
  plain write empties a file before it fills it, and both are written on the
  common path of every turn end with no lock held.
- The panes are tagged after the rotation lock is let go rather than while it
  is held. Two Herdr calls per pane, capped at five seconds each, is long
  enough to stall every other pane's hook, and a tag is worth none of that.
- A swap no longer dies recording the account it just left when the reading
  was one *grazr* could not trust. A swap leaves Claude's cache naming the
  account you came from, so the manual swap was most likely to hit this on the
  second one in a row, after the credential had already moved.
- A reading *grazr* cannot trust is now asked about even when no account could
  take over. Claude stops writing its usage cache while `~/.claude.json` and a
  running session name different accounts, which is the state a swap leaves
  behind for a while, and staying silent through it meant *grazr* could neither
  warn nor measure the pace. It asks rarely there, since the call buys those
  two things rather than a swap.
- Once a reading says a swap is due, the confirming call comes on a one minute
  leash instead of five. At a heavy pace five minutes of drift is a lot of
  window.
- An endpoint that does not answer is reported instead of passing in silence.
  Falling back to Claude's cached reading is right, but doing it quietly let a
  window run out while *grazr* looked like it was working.
- The keychain is reached at `/usr/bin/security` rather than by name, so a
  `security` planted earlier on `PATH` is never handed a credential.
- When no account is above your thresholds, *grazr* says they are low rather
  than spent. They still serve requests, and the old wording read as though
  the server had cut you off.
- A swap no longer signs you out of your MCP servers. Claude keeps their
  logins in the same store as the account credential, and they are minted
  against each server rather than against an account, so they now stay put
  while the login around them changes.
- A rotation now says when the account it moved to had a token that expired
  while it was parked. Claude refreshes one on its next request, but a
  refresh token already spent elsewhere makes that fail and signs the account
  out. The line is the only warning before that happens.
- An `apiKeyHelper`, `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` in
  `settings.json` puts Claude on API-key auth, so swapping the saved
  claude.ai login under it changes nothing. grazr used to report those swaps
  as though they had worked. It now stops and names the setting to unset.
- Carrying the MCP logins can make a credential too long for the macOS
  keychain, which refuses one rather than truncating it. The swap now goes
  ahead without them instead of failing.
- A credential the keychain will not hold, and a line in `config.env` that
  will not parse, now print one line saying so instead of a stack trace.
- A swap no longer leaves the account you left behind in Claude's config. Its
  `/login` API key stayed there, and Claude reads that key back as a way to
  authenticate, so the account you moved to could spend it. Its plan and
  model caches stayed too, and described the wrong account until Claude asked
  again.

### Internal

Housekeeping, none of which changes what *grazr* does.

- Two modules split out of files that held more than they said. `accounts.py`
  keeps the accounts *grazr* enrolled, which are its own files rather than
  Claude's, and `atomic.py` keeps the single way a file is put in place. Four
  copies of that had drifted apart.
- An entry point is handed what it works on instead of building it from the
  environment. A `Runtime` carries the paths, the credential store, the state
  directory and the config, so a caller can pass a sandbox and a test cannot
  reach the real keychain by forgetting a variable.
- What *grazr* last reported and whether its toast appeared travel together,
  so one reader and one writer know how the two are stored.
- Two names that did nothing are gone: an identity writer no caller used, and
  a wrapper that read Claude's config only to drop part of the answer.
- A test pins the agreement between the keychain timeout and the lock stale
  ages. Raising either number alone would let a swap lose a lock mid-write,
  and only a comment said so.

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
