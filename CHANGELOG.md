# Changelog

Notable changes to *grazr*, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Fixed

- A swap no longer signs you out of your MCP servers. Claude keeps their
  logins in the same store as the account credential, and they are minted
  against each server rather than against an account, so they now stay put
  while the login around them changes.
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
