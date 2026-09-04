# grazr

Rotational grazing for Claude Code: moves the herd to a fresh account before
the pasture runs out. A [Herdr](https://herdr.dev) plugin.

A Claude pane goes dead when the 5-hour or weekly limit hits, and getting it
back means logging in as another account by hand. grazr does not wait for the
wall. When a usage window is nearly spent it swaps which stored credential the
official `claude` binary reads, at a turn boundary, so no pane is ever blocked
or prompted.

**Requires** macOS, `python3` (3.9+), Herdr 0.8.0+, and two or more Claude
subscriptions that are all yours.

## How it works

Every account is logged in once with Claude's own `claude auth login`. grazr
never sees a password and never mints a token; it copies the credential blob a
normal login already produced into its own keychain item, and copies it back
when it is that account's turn. The `claude` binary is unmodified and re-reads
its credential every 30 seconds, which is what makes the swap invisible.

Nothing secret is ever written to disk. Parked credentials stay in the macOS
keychain; the files under the state directory hold only names, identities and
the last known headroom.

## Install

```sh
herdr plugin install wazum/herdr-grazr
```

Then enrol each account:

```sh
herdr plugin pane open --plugin wazum.grazr --entrypoint enrol
```

Pick `s` for the account you are logged into now, and `l` for each additional
one. `l` logs in through a throwaway config directory, so your live session is
never logged out.

Finally, list them in preference order:

```sh
$EDITOR "$(herdr plugin config-dir wazum.grazr)/config.env"
```

```sh
REMAINING_SESSION=15     # rotate when the 5-hour window has less than this left
REMAINING_WEEKLY=20      # weekly windows deserve more margin: losing one costs days
ACCOUNTS="work personal" # preference order; first with headroom wins
ENABLED=1
DRY_RUN=0                # 1 = log the decision, do not swap
```

Start with `DRY_RUN=1` for a day. Decisions appear in `herdr plugin log`.

### Turn Herdr's toasts on

**Herdr toasts are off by default**, so grazr's notification will not appear
until you enable them. Worse, `herdr notification show` still exits 0 when it
shows nothing, so a plugin cannot tell the difference without reading the JSON.
Add this to `~/.config/herdr/config.toml`:

```toml
[ui.toast]
delivery = "herdr"
delay_seconds = 1

[ui.toast.herdr]
position = "bottom-right"
```

Then `herdr server reload-config`. Choose `terminal` instead of `herdr` if you
work over SSH, or `system` for macOS Notification Centre.

Two things to expect. Herdr suppresses a toast while another is already on
screen, so a swap during a busy moment can be dropped; grazr notices that and
announces again next time rather than assuming you saw it. And the visible
duration is fixed by Herdr at 5 or 8 seconds depending on the notification
kind, with no setting for it. grazr asks for the 8-second kind, which also
plays a sound. Set `HERDR_DISABLE_SOUND=1` to keep the longer read without it.

### Remote Control

Claude's Remote Control is bound to the signed-in account, so it disconnects on
every swap and has to be restarted with `/remote-control` inside each affected
pane. grazr says so in its notification. It deliberately does not type that
command into your panes: it would mean writing into sessions that may be
mid-turn, and grazr never touches the screen.

To see what is enrolled and what each account has left:

```sh
herdr plugin pane open --plugin wazum.grazr --entrypoint status
```

## What it will not do

- It will not rotate away from an account the server has restricted. Moving off
  a restricted account is how you circumvent a restriction, and the Usage
  Policy prohibits that. grazr stops and tells you instead.
- It will not act on usage data it cannot trust. If the cached usage describes a
  different account, or is over an hour old, grazr does nothing.
- It will not write a credential it cannot write whole. macOS `security`
  silently truncates an over-long input and destroys the item, so grazr measures
  first and refuses rather than risking your login.

## Policy

grazr rotates between subscriptions that all belong to, and are paid for by,
the one person running it. It never shares credentials, never routes requests
through a third-party client, and never modifies the `claude` binary.

Anthropic's public documents do not cap accounts per person and do not address
switching between two of your own plans. The Claude Code docs describe
`CLAUDE_CONFIG_DIR` as "Useful for running multiple accounts side by side" and
list switching accounts as a normal response to a usage limit.

Two clauses cut the other way and no document resolves them: advertised limits
"assume ordinary, individual usage of Claude Code", and the Consumer Terms
prohibit accessing the services "through automated or non-human means, whether
through a bot, script, or otherwise". grazr is a script on the credential path
rather than the request path, but Anthropic has not said whether chaining
personal plans counts as ordinary use, and it reserves the right to enforce
without notice. Anthropic's own published route past a limit is usage credits.
Read the current terms and decide for yourself.
