"""grazr — rotate to a fresh Claude account before the current one runs out.

Entry points: hook (a Herdr event), enrol and status (popup panes).
All Herdr and filesystem I/O lives here; Claude knowledge lives in claude.py;
where credentials are kept lives in stores.py; the decision itself is
core.decide.
"""

import contextlib
import fcntl
import getpass
import json
import os
import shlex
import subprocess
import sys
import tempfile
import termios
import time
import tty
from collections import namedtuple
from datetime import datetime, timezone

import claude
import core
import stores

Config = namedtuple("Config", "thresholds accounts enabled dry_run live_usage_below")

DEFAULT_CONFIG = """\
# Rotate when a limit group has less than this percent left. Claude's cached
# reading runs behind the window, so leave more room than you mean to lose.
REMAINING_SESSION=30     # the 5-hour window
REMAINING_WEEKLY=20      # weekly windows, incl. per-model ones

# Preference order. First account with headroom wins.
ACCOUNTS=""

ENABLED=1
DRY_RUN=0                # 1 = log the decision, do not swap

# Below this much headroom, grazr asks the same endpoint Claude asks. Keep it
# higher than the thresholds above. The gap between the two is the band grazr
# watches in, wide enough to catch a reading that runs behind before it crosses
# the line. 0 turns the call off and leaves grazr with the cached reading.
LIVE_USAGE_BELOW=45
"""

_THRESHOLD_KEYS = {"REMAINING_SESSION": ("session", 30), "REMAINING_WEEKLY": ("weekly", 20)}
_FLAG_KEYS = (("ENABLED", True), ("DRY_RUN", False))


TURN_ENDS = ("idle", "done")

# Telling the user is worth a moment, but not a stuck hook on every turn end.
NOTIFY_TIMEOUT_SECONDS = 5

# The floor between two live usage calls, however many panes are idling. A
# reading that says a rotation is due waits on the shorter one, because five
# minutes of drift is a lot of window at a heavy pace. A reading grazr cannot
# see at all, with nowhere to rotate to, waits on the longest: it buys a warning
# and a pace to measure from, not a swap, and the blind spell runs for hours.
FETCH_INTERVAL_SECONDS = 300
URGENT_FETCH_INTERVAL_SECONDS = 60
BLIND_FETCH_INTERVAL_SECONDS = 1800


def notify(title, body, spawn=subprocess.run):
    """Herdr's own toast. Returns whether it was actually shown.

    `herdr notification show` exits 0 even when nothing appears: toasts are off
    by default (`[ui.toast] delivery`), a toast already on screen returns busy,
    and there is a rate limit. Treating that as success would leave the user
    never told, so the caller gets the truth.

    An in-app toast is a 3 second blink whatever the sound: the API path pins
    the kind to UpdateInstalled and only the delivery mode changes how long it
    lasts. Users who want to read it set `delivery = "system"` or `"terminal"`
    in Herdr, which is why the body stays short enough for either and why it
    makes a sound: grazr only speaks up for a swap, a restriction, or having
    nowhere left to go.
    """
    herdr = os.environ.get("HERDR_BIN_PATH")
    if not herdr:
        return False
    try:
        completed = spawn(
            [herdr, "notification", "show", title, "--body", body, "--sound", "request"],
            capture_output=True,
            text=True,
            timeout=NOTIFY_TIMEOUT_SECONDS,
        )
        return json.loads(completed.stdout)["result"]["shown"] is True
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return False


TAG = "grazr"

# A rotation reports to every Claude pane, so none of them may hang.
TAG_TIMEOUT_SECONDS = 5


def tag_all(name, spawn=subprocess.run):
    """Publish the active account to every Claude pane as the `$grazr` token.

    Herdr shows it only where the user's own sidebar row names `$grazr`, so
    without that row nothing appears. Best-effort: a tag is never worth
    failing a rotation over.
    """
    for pane_id, tokens in _claude_panes(spawn):
        if tokens.get(TAG) != name:
            tag_pane(pane_id, name, spawn)


def tag_pane(pane_id, name, spawn=subprocess.run):
    """One pane, unless it is being read. Herdr can snap a scrolled viewport
    back to the bottom when it repaints metadata, and a pane nobody was
    watching must not jump because another pane rotated."""
    if not pane_id or _is_scrolled(pane_id, spawn):
        return
    _herdr(spawn, "pane", "report-metadata", pane_id, "--source", TAG,
           "--token", "%s=%s" % (TAG, name))


def _claude_panes(spawn):
    """(pane id, current tokens) for every pane running Claude, and nothing for
    the other agents: they spend no Claude account."""
    answer = _herdr(spawn, "agent", "list")
    try:
        agents = json.loads(answer)["result"]["agents"]
    except (KeyError, TypeError, ValueError):
        return []
    return [
        (entry.get("pane_id"), entry.get("tokens") or {})
        for entry in agents
        if isinstance(entry, dict) and entry.get("agent") == "claude" and entry.get("pane_id")
    ]


def _is_scrolled(pane_id, spawn):
    answer = _herdr(spawn, "pane", "get", pane_id)
    try:
        scroll = json.loads(answer)["result"]["pane"]["scroll"]
        return scroll["offset_from_bottom"] > 0
    except (KeyError, TypeError, ValueError):
        # Repainting a pane someone is reading is the worse mistake, so an
        # unreadable answer counts as scrolled.
        return True


def _herdr(spawn, *arguments):
    """Returns stdout, or "" for anything that went wrong."""
    herdr = os.environ.get("HERDR_BIN_PATH")
    if not herdr:
        return ""
    try:
        completed = spawn(
            [herdr, *arguments],
            capture_output=True,
            text=True,
            timeout=TAG_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def act_on(decision, paths, store, state_dir, active_id, limits, dry_run, accounts=(), now=None):
    """Carry out what core.decide concluded, and report it. Returns the line
    that goes to stdout, which Herdr keeps in `herdr plugin log`."""
    if decision == "stay":
        return None

    def name_of(identifier):
        return _name_of(accounts, identifier)

    if decision == "locked":
        line = "account %s is restricted, not rotating" % name_of(active_id)
        announced = _announce_once(
            state_dir, "locked:%s" % active_id, "grazr: account restricted", line
        )
        return line if announced else None

    if decision == "unenrolled":
        line = "nothing to rotate to; enrol a second account and list it in ACCOUNTS"
        announced = _announce_once(
            state_dir, "unenrolled", "grazr: no second account", line
        )
        return line if announced else None

    if decision == "exhausted":
        soonest = _soonest_reset(
            now or datetime.now(timezone.utc),
            limits,
            *(entry.snapshot for entry in accounts)
        )
        # Below your thresholds, not cut off by the server: these accounts still
        # serve requests, so saying they are spent would stop you working for
        # no reason.
        line = "every account is below your thresholds, earliest reset %s" % (
            soonest or "unknown"
        )
        announced = _announce_once(
            state_dir, "exhausted:%s" % soonest, "grazr: every account is low", line
        )
        return line if announced else None

    kind, payload = decision

    if kind == "override":
        line = (
            "%s in settings.json puts Claude on API-key auth, so a swap changes nothing"
            % payload
        )
        announced = _announce_once(
            state_dir, "override:%s" % payload, "grazr: not rotating", line
        )
        return line if announced else None

    next_id = payload
    if dry_run:
        return "DRY_RUN: would rotate %s -> %s" % (name_of(active_id), name_of(next_id))

    note = claude.rotate(paths, store, active_id, next_id, limits)
    line = "rotated %s -> %s%s" % (name_of(active_id), name_of(next_id), note or "")
    shown = notify(
        "grazr: now on %s" % name_of(next_id),
        "Remote Control needs /remote-control per pane",
    )
    # The marker is the dedupe key too. Leaving the situation this rotation
    # just resolved on it would silence that situation the next time it is real.
    _write_marker(os.path.join(state_dir, "last_notice"), line, shown)
    return line


def _name_of(accounts, identifier):
    """The name enrolment gave an account, or its id when grazr never saw it."""
    return {entry.id: entry.name for entry in accounts}.get(identifier, identifier)


def _moved(decision, dry_run):
    """Whether a credential actually changed hands, which is the only thing
    worth repainting the panes for."""
    return not dry_run and isinstance(decision, tuple) and decision[0] == "rotate"


def _soonest_reset(now, *snapshots):
    """When the first window anywhere reopens, as a local weekday and clock
    time, since it ends up in a toast. A snapshot can hold one that already
    has, and naming that would give a time in the past."""
    times = [
        entry.resets_at
        for snapshot in snapshots
        if isinstance(snapshot, list)
        for entry in snapshot
        if entry.resets_at and entry.resets_at > now
    ]
    return min(times).astimezone().strftime("%a %H:%M") if times else None


def _announce_once(state_dir, key, title, body):
    """Report a situation once, across two channels that fail differently.

    The plugin log is the durable record and must not depend on the toast:
    Herdr's toasts are off by default. The toast is ephemeral and Herdr drops it
    while another is on screen, so it is retried until it is really shown.

    Returns whether this situation is new, which is what earns a log line. The
    branches that call it are reached on every idle event of every pane.
    """
    marker = os.path.join(state_dir, "last_notice")
    seen, notified = None, False
    try:
        with open(marker) as handle:
            seen, _, flag = handle.read().partition("\n")
            notified = flag == "1"
    except IOError:
        pass

    if seen == key:
        if not notified and notify(title, body):
            _write_marker(marker, key, True)
        return False

    _write_marker(marker, key, notify(title, body))
    return True


def _write_marker(marker, key, notified):
    _atomic_write(marker, "%s\n%s" % (key, "1" if notified else "0"))


def _atomic_write(path, text):
    """Put a file in place in one step. Both shared files are written on the
    common path of every turn end, in every pane, with no lock held, and a
    plain open empties a file before it fills it."""
    directory = os.path.dirname(path) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".grazr-")
    try:
        with os.fdopen(handle, "w") as writer:
            writer.write(text)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def is_turn_end(event_json):
    """A Claude pane that just finished a turn. `done` is idle in a tab nobody
    has looked at, so it counts too."""
    try:
        event = json.loads(event_json or "null")
    except ValueError:
        return False
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict):
        return False
    return data.get("agent") == "claude" and data.get("agent_status") in TURN_ENDS


def load_config(path):
    """Parse config.env, seeding it on first run. Every problem names its key:
    a threshold typo that silently defaulted would rotate at the wrong moment."""
    if not os.path.exists(path):
        _seed_config(path)

    settings = {}
    with open(path) as handle:
        for number, line in enumerate(handle, start=1):
            tokens = shlex.split(line, comments=True)
            if not tokens:
                continue
            # A quoted value survives as one token, so leftovers mean it was
            # unquoted, and every name after the first would be lost.
            if len(tokens) >= 3 and tokens[1] == "=":
                key, value, leftover = tokens[0], tokens[2], tokens[3:]
            else:
                key, separator, value = tokens[0].partition("=")
                leftover = tokens[1:]
                if not separator or not key:
                    raise ValueError("%s line %d: expected KEY=value" % (path, number))
            if leftover:
                raise ValueError(
                    '%s line %d: quote the value, as in %s="%s"'
                    % (path, number, key, " ".join([value] + leftover))
                )
            if key in settings:
                raise ValueError("%s line %d: %s is set twice" % (path, number, key))
            settings[key] = value

    # An omitted setting takes the documented default. A misspelt one is
    # rejected below, so a typo can never quietly become a default.
    thresholds = {}
    for key, (group, default) in _THRESHOLD_KEYS.items():
        thresholds[group] = _percent(settings.pop(key, None), key, default)
    flags = [_flag(settings.pop(key, None), key, default) for key, default in _FLAG_KEYS]
    live_usage_below = _percent(
        settings.pop("LIVE_USAGE_BELOW", None), "LIVE_USAGE_BELOW", 45
    )
    accounts = shlex.split(settings.pop("ACCOUNTS", "") or "")
    if len(set(accounts)) != len(accounts):
        raise ValueError("ACCOUNTS lists the same account twice: %s" % " ".join(accounts))
    if settings:
        raise ValueError("unknown setting(s): %s" % ", ".join(sorted(settings)))

    return Config(
        thresholds=thresholds,
        accounts=accounts,
        enabled=flags[0],
        dry_run=flags[1],
        live_usage_below=live_usage_below,
    )


def _seed_config(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(path, "x") as handle:
            handle.write(DEFAULT_CONFIG)
    except FileExistsError:
        pass


def _percent(value, key, default):
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ValueError("%s must be a whole percent, got %r" % (key, value))
    if not 0 <= number <= 100:
        raise ValueError("%s must be between 0 and 100, got %d" % (key, number))
    return number


def _flag(value, key, default):
    if value is None:
        return default
    if value not in ("0", "1"):
        raise ValueError("%s must be 0 or 1, got %r" % (key, value))
    return value == "1"


ESCAPE = "\x1b"
CLOSE_KEYS = (ESCAPE, "q", "\r", "\n", "")


def read_key(stream=None):
    """One keypress, no Enter, so Esc can close a pane. Falls back to a line
    when stdin is not a terminal, which is how the tests drive it."""
    stream = stream or sys.stdin
    if not stream.isatty():
        line = stream.readline()
        return line.strip()[:1].lower() if line.strip() else ESCAPE

    descriptor = stream.fileno()
    saved = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        key = stream.read(1)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
    if key == "\x03":
        # Raw mode swallows the signal, so Ctrl+C has to be re-raised by hand.
        raise KeyboardInterrupt
    return key.lower()


def _paths():
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser("~/.grazr")
    accounts_dir = os.path.join(state_dir, "accounts")
    os.makedirs(accounts_dir, exist_ok=True)
    # With CLAUDE_CONFIG_DIR set, Claude keeps its .claude.json inside that
    # directory rather than in the home directory.
    isolated = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = isolated or os.path.expanduser("~/.claude")
    config_path = (
        os.path.join(isolated, ".claude.json") if isolated else os.path.expanduser("~/.claude.json")
    )
    return (
        claude.Paths(
            config_path=config_path,
            config_dir=config_dir,
            accounts_dir=accounts_dir,
        ),
        stores.default_store(isolated, config_dir, state_dir, getpass.getuser()),
        state_dir,
    )


def _config_path():
    directory = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.expanduser("~/.grazr")
    return os.path.join(directory, "config.env")


def hook():
    """Runs on every pane.agent_status_changed across every pane, so the common
    path is two file reads and no subprocess."""
    if not is_turn_end(os.environ.get("HERDR_PLUGIN_EVENT_JSON")):
        return 0

    config = load_config(_config_path())
    if not config.enabled:
        return 0

    paths, store, state_dir = _paths()
    now = datetime.now(timezone.utc)
    active, limits, fetched_at = claude.inspect_reading(paths, now)
    if active is None:
        return 0

    # _fetch_due stamps the interval as a side effect, so it goes last: a call
    # refused for any other reason must not spend the next five minutes.
    estimate = _estimate(state_dir, active, limits, fetched_at, now)
    if (
        _worth_asking(estimate, config.live_usage_below)
        # Ordered by what each costs to answer, cheapest first. An auth setting
        # makes a swap change nothing, so the usage behind it is not worth a call.
        and not claude.settings_auth_override(paths.config_dir)
        and _fetch_due(state_dir, _interval(paths, config, active, estimate, now))
    ):
        answer = claude.fetch_limits(store)
        if answer is None:
            # Going on the cached reading is right, but doing it in silence
            # lets a window run out while grazr looks like it is working.
            line = "the usage endpoint did not answer, going on the cached reading"
            if _announce_once(state_dir, "unconfirmed", "grazr: usage check failed", line):
                print(line)
        else:
            limits = answer

    # The common path ends here: no account store, no lock, no subprocess.
    if not core.needs_rotation(limits, now, config.thresholds):
        return 0

    # Only the rotate and announce paths take the lock. The common path above
    # never touches it.
    with _rotation_lock(state_dir) as acquired:
        if not acquired:
            return 0
        # Another pane may have swapped while this one waited for the lock, and
        # then the reading above describes an account we already left. Only
        # that case is worth re-reading for: otherwise the live reading taken
        # above is the better of the two.
        swapped_to, its_limits = claude.inspect(paths, now)
        if swapped_to != active:
            active, limits = swapped_to, its_limits
        accounts = claude.load_accounts(paths, config.accounts)
        decision = core.decide(limits, active, accounts, now, config.thresholds)
        # Only a decision to move pays for the settings.json read, so the common
        # path is unchanged. rotate refuses this too, but a raised error suits
        # the person who just pressed a key, not every turn end of every pane.
        if isinstance(decision, tuple):
            override = claude.settings_auth_override(paths.config_dir)
            if override:
                decision = ("override", override)
        line = act_on(
            decision, paths, store, state_dir, active, limits, config.dry_run, accounts, now
        )

    # Outside the lock. Two herdr calls per pane, capped at five seconds each,
    # is far too long to hold the thing every other pane's hook is waiting on,
    # and a tag is worth none of that.
    if _moved(decision, config.dry_run):
        tag_all(_name_of(accounts, decision[1]))

    if line:
        print(line)
    return 0


def _worth_asking(limits, below):
    """Plenty of headroom needs no second opinion, and a restricted account has
    nothing to learn. What is left is the band where a swap may be due, plus
    "unknown", which is the stale cache that hid a spent window in the first
    place."""
    if not below or limits == "locked":
        return False
    if limits == "unknown":
        return True
    return any(entry.remaining < below for entry in limits)


def _estimate(state_dir, active, limits, fetched_at, now):
    """What the cached reading is probably worth by now: its own figures, less
    what the pace behind it implies has been spent since Claude took it.

    Only ever decides whether the reading is worth confirming. A rotation is
    always taken on a reading, never on this.
    """
    rates = _record_reading(state_dir, active, limits, fetched_at)
    if not isinstance(limits, list) or fetched_at is None:
        return limits
    age_hours = (now.timestamp() * 1000 - fetched_at) / core.MS_PER_HOUR
    return core.corrected(limits, rates, age_hours)


def _record_reading(state_dir, active, limits, fetched_at):
    """Keep the newest reading so the next turn end can tell the pace, and
    return the pace since the one before it.

    Kept per account: a rate measured across a swap would describe two windows
    at once. A reading Claude has not refreshed is stored again as itself, so
    the pair either side of it still spans the real interval.
    """
    path = os.path.join(state_dir, "reading")
    previous = None
    try:
        with open(path) as handle:
            stored = json.load(handle)
        if stored["account"] == active:
            previous = core.Reading(
                fetched_at=stored["fetched_at"], remaining=stored["remaining"]
            )
    except (OSError, ValueError, KeyError, TypeError):
        pass

    if not isinstance(limits, list) or fetched_at is None:
        return {}
    current = core.Reading(
        fetched_at=fetched_at, remaining={entry.group: entry.remaining for entry in limits}
    )
    if previous is None or current.fetched_at != previous.fetched_at:
        _atomic_write(path, json.dumps(
            {"account": active, "fetched_at": fetched_at, "remaining": current.remaining}
        ))
    return core.burn_rate(previous, current)


def _interval(paths, config, active, estimate, now):
    """How long the endpoint gets left alone before the next call, or None to
    leave it alone entirely.

    With an account able to take over, the call decides a swap and waits the
    ordinary floor, or the short one when the reading already says to move.
    With nowhere to go the call decides nothing, so it is only worth making
    when grazr cannot see the active account at all.
    """
    if _somewhere_to_go(paths, config, active, now):
        if core.needs_rotation(estimate, now, config.thresholds):
            return URGENT_FETCH_INTERVAL_SECONDS
        return FETCH_INTERVAL_SECONDS
    return BLIND_FETCH_INTERVAL_SECONDS if estimate == "unknown" else None


def _somewhere_to_go(paths, config, active, now):
    """Whether any enrolled account could take over. The live reading exists to
    decide a rotation, so with nowhere to rotate to it decides nothing, and the
    endpoint is undocumented and rate-limited enough to be worth not asking."""
    accounts = claude.load_accounts(paths, config.accounts)
    return core.next_account(active, accounts, now, config.thresholds) is not None


def _fetch_due(state_dir, interval):
    """Several panes go idle at once and the endpoint rate-limits, so the
    interval is shared through a file rather than kept per process. A None
    interval means the call is not worth making at all."""
    if interval is None:
        return False
    marker = os.path.join(state_dir, "last_usage_fetch")
    try:
        if time.time() - os.path.getmtime(marker) < interval:
            return False
    except OSError:
        pass
    # Stamped before the call, not after: a slow endpoint must not release
    # every other pane to pile on behind it.
    with open(marker, "a"):
        os.utime(marker, None)
    return True


@contextlib.contextmanager
def _rotation_lock(state_dir):
    """One pane at a time past the threshold. Several panes go idle together
    when a long turn ends, and each would otherwise park over the others."""
    # "w" would truncate before flock is even attempted, so a pane that goes on
    # to lose the race still writes to the file.
    handle = open(os.path.join(state_dir, "rotate.lock"), "a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except IOError:
            yield False
            return
        yield True
    finally:
        handle.close()


def main(argv):
    try:
        return _dispatch(argv)
    except (KeyboardInterrupt, EOFError):
        # A pane owns the terminal, so a traceback is the last thing to show.
        print("\ncancelled")
        return 130
    except (RuntimeError, ValueError) as error:
        # Every one of these is a refusal grazr raised on purpose -- a busy
        # lock, an unreadable keychain, a credential too long to store, a
        # config line it cannot parse -- and its message is the whole point.
        print("grazr: %s" % error)
        return 1


def _dispatch(argv):
    command = argv[1] if len(argv) > 1 else ""
    if command == "hook":
        return hook()
    if command == "status":
        return status()
    if command == "enrol":
        return enrol()
    if command == "swap":
        return swap()
    if command == "tag":
        return tag()
    print("usage: grazr.py hook|status|enrol|swap|tag", file=sys.stderr)
    return 2


def tag():
    """Name the account being spent. The pane event carries a pane id and tags
    that pane, so a new pane is not blank until the next rotation. The action
    carries none and tags every pane, which is what you press after adding the
    sidebar row."""
    paths, _, _ = _paths()
    active, _ = claude.inspect(paths, datetime.now(timezone.utc))
    if active is None:
        return 0
    named = next(
        (entry.name for entry in claude.load_accounts(paths, []) if entry.id == active),
        active,
    )
    pane_id = os.environ.get("HERDR_PANE_ID")
    if pane_id:
        tag_pane(pane_id, named)
    else:
        tag_all(named)
    return 0


def swap():
    """A swap the user asked for, from a Herdr key. The active account's
    headroom is not consulted, and ENABLED gates the hook only."""
    config = load_config(_config_path())
    paths, store, state_dir = _paths()
    now = datetime.now(timezone.utc)
    with _rotation_lock(state_dir) as acquired:
        if not acquired:
            return _refuse_swap("busy rotating already, try again in a moment")
        active, limits = claude.inspect(paths, now)
        if active is None:
            return _refuse_swap("not logged in, nothing to swap from")
        accounts = claude.load_accounts(paths, config.accounts)
        next_id = core.next_account(active, accounts, now, config.thresholds)
        if next_id is None:
            soonest = _soonest_reset(now, *(entry.snapshot for entry in accounts))
            return _refuse_swap(
                "nothing to swap to, earliest reset %s" % (soonest or "unknown")
            )
        decision = ("rotate", next_id)
        print(
            act_on(
                decision, paths, store, state_dir, active, limits,
                config.dry_run, accounts, now,
            )
        )

    if _moved(decision, config.dry_run):
        tag_all(_name_of(accounts, next_id))
    return 0


def _refuse_swap(reason):
    """The user pressed a key and is watching, so a refusal goes to the screen
    as well as the log."""
    print("grazr: %s" % reason)
    notify("grazr: no swap", reason)
    return 1


def status():
    paths, store, state_dir = _paths()
    config = load_config(_config_path())
    now = datetime.now(timezone.utc)
    active, limits = claude.inspect(paths, now)

    print("config: %s" % _config_path())
    print("thresholds: session %d%% / weekly %d%% remaining%s\n"
          % (config.thresholds["session"], config.thresholds["weekly"],
             "   DRY_RUN" if config.dry_run else ""))

    accounts = claude.load_accounts(paths, config.accounts)
    for account in accounts:
        marker = "*" if account.id == active else " "
        parked = "parked" if claude.has_parked_credential(store, account.id) else "NO CREDENTIAL"
        print("%s %-16s %-14s %s" % (marker, account.name, parked, _describe(account.snapshot)))

    for name in config.accounts:
        if not any(account.name == name for account in accounts):
            print("  %-16s not enrolled, so it is never used" % name)

    print("\nactive account headroom: %s" % _describe(limits))
    if limits == "unknown":
        print("  (usage describes another account or is over an hour old)")
    if active and not any(account.id == active for account in accounts):
        # Enrolled but left out of ACCOUNTS is the likelier mistake, and calling
        # that "not enrolled" sends you off to enrol it a second time.
        enrolled = any(account.id == active for account in claude.load_accounts(paths, []))
        print("  this login is %s; grazr can rotate away but not back"
              % ("enrolled but missing from ACCOUNTS" if enrolled else "not enrolled"))

    last = _last_decision(state_dir)
    if last:
        print("\nlast reported: %s" % last)
    print("\n(* = active)   press any key to close")
    read_key()
    return 0


def _last_decision(state_dir):
    try:
        with open(os.path.join(state_dir, "last_notice")) as handle:
            key, _, notified = handle.read().partition("\n")
    except IOError:
        return None
    return "%s%s" % (key, "" if notified == "1" else "  (toast never appeared)")


def _describe(snapshot):
    if snapshot is None:
        return "never parked"
    if snapshot in ("locked", "unknown"):
        return snapshot
    return ", ".join(
        "%s %d%% left" % (entry.kind, entry.remaining) for entry in snapshot
    ) or "no limits reported"


def enrol():
    # No title here: the pane border already carries it.
    print("  (s) save the login this machine is using now")
    print("  (l) log in to another account, without touching the current one")
    print("  (q) close, or press Esc\n")
    print("choice: ", end="", flush=True)
    choice = read_key()
    print(choice if choice in ("s", "l") else "")
    if choice not in ("s", "l"):
        print("closed, nothing changed")
        return 0

    paths, store, _ = _paths()
    if choice == "s":
        return _enrol_from(paths, store, None)

    source = os.path.abspath(
        os.path.join(paths.accounts_dir, "..", "enrol-%d" % os.getpid())
    )
    os.makedirs(source, mode=0o700, exist_ok=True)
    # Everything after the directory exists is inside the try: an abort at the
    # browser step or the name prompt would otherwise strand a real credential
    # in the keychain under a service name nothing tracks.
    try:
        print("\nlogging in with an isolated config dir; your current login is untouched.\n")
        try:
            login = subprocess.run(
                ["claude", "auth", "login"], env=dict(os.environ, CLAUDE_CONFIG_DIR=source)
            )
        except FileNotFoundError:
            print("claude is not on this pane's PATH, so it cannot log you in")
            return 1
        if login.returncode != 0:
            print("login did not complete")
            return 1
        return _enrol_from(paths, store, source)
    finally:
        if not claude.discard_isolated_login(store, source):
            print("warning: could not remove the throwaway login from the keychain")


def _enrol_from(paths, store, source):
    name = input("account name: ").strip()
    if not name:
        print("a name is required")
        return 1
    try:
        identifier = claude.enrol(paths, store, name, source)
    except RuntimeError as error:
        print("could not enrol: %s" % error)
        return 1
    print("\nenrolled %s as %s" % (name, identifier))
    print('add it to ACCOUNTS in %s' % _config_path())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
