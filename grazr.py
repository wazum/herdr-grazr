"""grazr — rotate to a fresh Claude account before the current one runs out.

Entry points: hook (a Herdr event), enrol and status (popup panes).
All Herdr and filesystem I/O lives here; Claude knowledge lives in claude.py;
the decision itself is core.decide.
"""

import contextlib
import fcntl
import getpass
import json
import os
import shlex
import subprocess
import sys
from collections import namedtuple
from datetime import datetime, timezone

import claude
import core

Config = namedtuple("Config", "thresholds accounts enabled dry_run")

DEFAULT_CONFIG = """\
# Rotate when a limit group has less than this percent left.
REMAINING_SESSION=15     # the 5-hour window
REMAINING_WEEKLY=20      # weekly windows, incl. per-model ones

# Preference order. First account with headroom wins.
ACCOUNTS=""

ENABLED=1
DRY_RUN=0                # 1 = log the decision, do not swap
"""

_THRESHOLD_KEYS = {"REMAINING_SESSION": ("session", 15), "REMAINING_WEEKLY": ("weekly", 20)}
_FLAG_KEYS = (("ENABLED", True), ("DRY_RUN", False))


TURN_ENDS = ("idle", "done")


def notify(title, body, spawn=subprocess.run):
    """Herdr's own toast. Returns whether it was actually shown.

    `herdr notification show` exits 0 even when nothing appears: toasts are off
    by default (`[ui.toast] delivery`), a toast already on screen returns busy,
    and there is a rate limit. Treating that as success would leave the user
    never told, so the caller gets the truth.

    An in-app toast is a 3 second blink whatever the sound: the API path pins
    the kind to UpdateInstalled and only the delivery mode changes how long it
    lasts. Users who want to read it set `delivery = "system"` or `"terminal"`
    in Herdr, which is why the body stays short enough for either.
    """
    herdr = os.environ.get("HERDR_BIN_PATH")
    if not herdr:
        return False
    completed = spawn(
        [herdr, "notification", "show", title, "--body", body, "--sound", "none"],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(completed.stdout)["result"]["shown"] is True
    except (ValueError, KeyError, TypeError):
        return False


def act_on(decision, paths, state_dir, active_id, limits, dry_run):
    """Carry out what core.decide concluded, and report it. Returns the line
    that goes to stdout, which Herdr keeps in `herdr plugin log`."""
    if decision == "stay":
        return None

    if decision == "locked":
        line = "account %s is restricted; not rotating" % active_id
        announced = _announce_once(
            state_dir, "locked:%s" % active_id, "Grazr: account restricted", line
        )
        return line if announced else None

    if decision == "exhausted":
        soonest = _soonest_reset(limits)
        line = "every account is spent; earliest reset %s" % (soonest or "unknown")
        announced = _announce_once(
            state_dir, "exhausted:%s" % soonest, "Grazr: no headroom left", line
        )
        return line if announced else None

    _, next_id = decision
    if dry_run:
        return "DRY_RUN: would rotate %s -> %s" % (active_id, next_id)

    claude.rotate(paths, active_id, next_id, limits)
    line = "rotated %s -> %s" % (active_id, next_id)
    notify("Grazr: now on %s" % next_id[:8], "Remote Control needs /remote-control per pane")
    return line


def _soonest_reset(limits):
    """When the first window reopens. `limits` may be a sentinel string rather
    than a list, and a limit may carry no reset time at all."""
    if not isinstance(limits, list):
        return None
    times = [entry.resets_at for entry in limits if entry.resets_at]
    return min(times).isoformat() if times else None


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
    with open(marker, "w") as handle:
        handle.write("%s\n%s" % (key, "1" if notified else "0"))


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
            key, separator, value = tokens[0].partition("=")
            if not separator:
                raise ValueError("%s line %d: expected KEY=value" % (path, number))
            settings[key] = value

    # An omitted setting takes the documented default; a misspelt one is
    # rejected below, so a typo can never quietly become a default.
    thresholds = {}
    for key, (group, default) in _THRESHOLD_KEYS.items():
        thresholds[group] = _percent(settings.pop(key, None), key, default)
    flags = [_flag(settings.pop(key, None), key, default) for key, default in _FLAG_KEYS]
    accounts = shlex.split(settings.pop("ACCOUNTS", "") or "")
    if len(set(accounts)) != len(accounts):
        raise ValueError("ACCOUNTS lists the same account twice: %s" % " ".join(accounts))
    if settings:
        raise ValueError("unknown setting(s): %s" % ", ".join(sorted(settings)))

    return Config(thresholds=thresholds, accounts=accounts, enabled=flags[0], dry_run=flags[1])


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
            keychain_account=getpass.getuser(),
        ),
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

    paths, state_dir = _paths()
    now = datetime.now(timezone.utc)
    active, limits = claude.inspect(paths, now)
    if active is None:
        return 0

    # The common path ends here: no account store, no lock, no subprocess.
    if not core.needs_rotation(limits, now, config.thresholds):
        return 0

    # Only the rotate and announce paths take the lock; the common path above
    # never touches it.
    with _rotation_lock(state_dir) as acquired:
        if not acquired:
            return 0
        active, limits = claude.inspect(paths, now)
        accounts = claude.load_accounts(paths, config.accounts)
        decision = core.decide(limits, active, accounts, now, config.thresholds)
        line = act_on(decision, paths, state_dir, active, limits, config.dry_run)

    if line:
        print(line)
    return 0


@contextlib.contextmanager
def _rotation_lock(state_dir):
    """One pane at a time past the threshold. Several panes go idle together
    when a long turn ends, and each would otherwise park over the others."""
    handle = open(os.path.join(state_dir, "rotate.lock"), "w")
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
    command = argv[1] if len(argv) > 1 else ""
    if command == "hook":
        return hook()
    if command == "status":
        return status()
    if command == "enrol":
        return enrol()
    print("usage: grazr.py hook|status|enrol", file=sys.stderr)
    return 2


def status():
    paths, _ = _paths()
    config = load_config(_config_path())
    now = datetime.now(timezone.utc)
    active, limits = claude.inspect(paths, now)

    print("grazr — enrolled accounts and headroom\n")
    print("config: %s" % _config_path())
    print("thresholds: session %d%% / weekly %d%% remaining%s\n"
          % (config.thresholds["session"], config.thresholds["weekly"],
             "   DRY_RUN" if config.dry_run else ""))

    for account in claude.load_accounts(paths, config.accounts):
        marker = "*" if account.id == active else " "
        print("%s %-16s %s" % (marker, account.name, _describe(account.snapshot)))

    print("\nactive account headroom: %s" % _describe(limits))
    print("\n(* = active)   press return to close")
    try:
        input()
    except EOFError:
        pass
    return 0


def _describe(snapshot):
    if snapshot is None:
        return "never parked"
    if snapshot in ("locked", "unknown"):
        return snapshot
    return ", ".join(
        "%s %d%% left" % (entry.kind, entry.remaining) for entry in snapshot
    ) or "no limits reported"


def enrol():
    paths, _ = _paths()
    print("grazr — enrol a Claude account\n")
    print("  (s) save the login this machine is using now")
    print("  (l) log in to another account, without touching the current one\n")
    choice = input("choice [s/l]: ").strip().lower()
    if choice not in ("s", "l"):
        print("nothing to do")
        return 1

    if choice == "s":
        return _enrol_from(paths, None)

    source = os.path.abspath(
        os.path.join(paths.accounts_dir, "..", "enrol-%d" % os.getpid())
    )
    os.makedirs(source, exist_ok=True)
    # Everything after the directory exists is inside the try: an abort at the
    # browser step or the name prompt would otherwise strand a real credential
    # in the keychain under a service name nothing tracks.
    try:
        print("\nlogging in with an isolated config dir; your current login is untouched.\n")
        login = subprocess.run(
            ["claude", "auth", "login"], env=dict(os.environ, CLAUDE_CONFIG_DIR=source)
        )
        if login.returncode != 0:
            print("login did not complete")
            return 1
        return _enrol_from(paths, source)
    finally:
        if not claude.discard_isolated_login(paths, source):
            print("warning: could not remove the throwaway login from the keychain")


def _enrol_from(paths, source):
    name = input("account name: ").strip()
    if not name:
        print("a name is required")
        return 1
    try:
        identifier = claude.enrol(paths, name, source)
    except RuntimeError as error:
        print("could not enrol: %s" % error)
        return 1
    print("\nenrolled %s as %s" % (name, identifier))
    print('add it to ACCOUNTS in %s' % _config_path())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
