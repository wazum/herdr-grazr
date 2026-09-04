# grazr

Rotational grazing for Claude Code: moves the herd to a fresh account before
the pasture runs out. A [Herdr](https://herdr.dev) plugin.

A Claude pane goes dead when the 5-hour or weekly limit hits. Getting it back
means logging in as another account by hand. grazr does not wait for the wall.
When a window is nearly spent, it swaps which stored credential the official
`claude` binary reads. It does that between turns, so no pane is ever blocked
or prompted.

**Requires** macOS, `python3` (3.9+), Herdr 0.8.0+, and two or more Claude
subscriptions that are all yours.

## How it works

<img width="880" src="docs/how-grazr-works.svg"
     alt="Flowchart of grazr's response when a Claude pane finishes a turn: it reads Claude's cached usage, stays put unless that usage is both trustworthy and nearly spent, and otherwise swaps the stored keychain credential to a fresh account, which Claude picks up within its thirty-second cache.">

You log in to each account once, with Claude's own `claude auth login`. grazr
never sees a password and never mints a token. It copies the credential that a
normal login already produced into its own keychain item, then copies it back
when that account's turn comes. The `claude` binary is unmodified, and it
re-reads its credential every 30 seconds. That is what makes the swap
invisible.

grazr writes no secrets to disk. Parked credentials stay in the macOS keychain,
and the files in the state directory hold only names, identities and the last
known headroom.

## Install

```sh
herdr plugin install wazum/herdr-grazr
```

Then enrol each account:

```sh
herdr plugin pane open --plugin wazum.grazr --entrypoint enrol
```

Pick `s` for the account you are logged into now. Pick `l` for each extra one.
`l` logs in through a throwaway config directory, so your live session is never
logged out.

Then list them in the order you want them used:

```sh
$EDITOR "$(herdr plugin config-dir wazum.grazr)/config.env"
```

```sh
REMAINING_SESSION=15     # rotate when the 5-hour window has less than this left
REMAINING_WEEKLY=20      # weekly windows get more margin, because losing one costs days
ACCOUNTS="work personal" # preference order, first with headroom wins
ENABLED=1
DRY_RUN=0                # 1 = log the decision, do not swap
```

Start with `DRY_RUN=1` for a day. Decisions show up in `herdr plugin log`.

### Turn Herdr's toasts on

**Herdr toasts are off by default**, so grazr's notification will not show
until you turn them on. `herdr notification show` also exits 0 when it shows
nothing, so a plugin has to read the JSON to tell. Add this to
`~/.config/herdr/config.toml`:

```toml
[ui.toast]
delivery = "herdr"
delay_seconds = 1

[ui.toast.herdr]
position = "bottom-right"
```

Then run `herdr server reload-config`. Pick `terminal` instead of `herdr` if
you work over SSH, or `system` for macOS Notification Centre.

A plugin toast lasts about three seconds and there is no setting for it, so
grazr keeps the text to one short line and plays a sound. Pick `system` or
`terminal` delivery to read it at your own pace, or set
`HERDR_DISABLE_SOUND=1` for silence.

Herdr drops a toast while another one is on screen. For the "nothing left" and
"account restricted" messages, grazr notices and says it again next time
instead of assuming you read it. A rotation is announced once, so a dropped
toast still leaves the swap in `herdr plugin log`.

### Remote Control

Claude's Remote Control belongs to the signed-in account. It disconnects on
every swap, and you restart it with `/remote-control` in each pane you care
about. grazr says so in its notification. It will not type that command for
you, because that would mean writing into panes that may be mid-turn, and grazr
never touches the screen.

To see what is enrolled and what each account has left:

```sh
herdr plugin pane open --plugin wazum.grazr --entrypoint status
```

## What it will not do

- Rotate away from an account the server has restricted. Moving off a
  restricted account is how you get around the restriction, and the Usage
  Policy forbids that. grazr stops and tells you.
- Act on usage data it cannot trust. If the cached usage belongs to another
  account, or is more than an hour old, grazr does nothing.
- Write a credential it cannot write whole. macOS `security` quietly truncates
  an over-long input and destroys the item, so grazr measures first and
  refuses.

## Policy

grazr rotates between subscriptions that belong to one person: the person
running it, who pays for them. It never shares credentials, never routes
requests through another client, and never changes the `claude` binary.

Anthropic's public documents do not limit how many accounts one person may
have, and they do not cover switching between two of your own plans. The Claude
Code docs call `CLAUDE_CONFIG_DIR` "Useful for running multiple accounts side
by side", and they list switching accounts as a normal answer to a usage limit.

Two clauses point the other way, and no document settles them. Advertised
limits "assume ordinary, individual usage of Claude Code". The Consumer Terms
forbid reaching the services "through automated or non-human means, whether
through a bot, script, or otherwise". grazr is a script on the credential path,
not on the request path. But Anthropic has not said whether chaining personal
plans counts as ordinary use, and it can enforce without notice. Anthropic's
own answer to a limit is usage credits. Read the current terms and decide for
yourself.
