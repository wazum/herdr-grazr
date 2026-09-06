"""grazr — rotate to a fresh Claude account before the current one runs out.

Entry points: statusline (Claude's status-line command, after every message),
decide (detached from it), tag (a Herdr event), install, uninstall and swap
(Herdr actions), enrol and status (popup panes). All Herdr I/O lives here.
Claude's own files live in claude.py, the accounts grazr enrolled in
accounts.py, where credentials are kept in stores.py, and the decision itself
is core.decide.
"""

import contextlib
import fcntl
import getpass
import json
import os
import shlex
import subprocess
import sys
import termios
import tty
from collections import namedtuple
from datetime import datetime, timezone

import accounts
import atomic
import claude
import core
import stores

Config = namedtuple("Config", "thresholds accounts enabled dry_run")

Runtime = namedtuple("Runtime", "paths store state_dir config")

DEFAULT_CONFIG = """\
# Rotate when a limit group has less than this percent left.
REMAINING_SESSION=30     # the 5-hour window
REMAINING_WEEKLY=20      # weekly windows, incl. per-model ones

# Preference order. First account with headroom wins.
ACCOUNTS=""

ENABLED=1
DRY_RUN=0                # 1 = log the decision, do not swap
"""

_THRESHOLD_KEYS = {"REMAINING_SESSION": ("session", 30), "REMAINING_WEEKLY": ("weekly", 20)}
_FLAG_KEYS = (("ENABLED", True), ("DRY_RUN", False))

# Telling the user is worth a moment, but not a stuck status line.
NOTIFY_TIMEOUT_SECONDS = 5


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


def act_on(decision, runtime, active_id, limits, accounts=(), now=None):
    """Carry out what core.decide concluded, and report it. Returns the line
    that goes to stdout, which Herdr keeps in `herdr plugin log`."""
    paths, store, state_dir, config = runtime
    dry_run = config.dry_run
    if decision == "stay":
        return None

    def name_of(identifier):
        return _name_of(accounts, identifier)

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
    # A rotation resolves every open situation. Leaving them on record would
    # silence each of them the next time it is real.
    _write_notices(state_dir, {line: shown})
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
    branches that call it are reached on every message of every pane, and two
    panes reaching a new situation together would both call it new, so the
    store is read and written under a lock. Every open situation is kept, or
    two of them would evict each other and toast in turns.
    """
    with _file_lock(os.path.join(state_dir, "notices.lock")) as held:
        if not held:
            return False
        notices = _read_notices(state_dir)
        if key in notices:
            if not notices[key] and notify(title, body):
                notices[key] = True
                _write_notices(state_dir, notices)
            return False
        notices[key] = notify(title, body)
        _write_notices(state_dir, notices)
        return True


def _read_notices(state_dir):
    try:
        with open(os.path.join(state_dir, "notices.json")) as handle:
            notices = json.load(handle)
    except (OSError, ValueError):
        return {}
    return notices if isinstance(notices, dict) else {}


def _write_notices(state_dir, notices):
    atomic.write(os.path.join(state_dir, "notices.json"), json.dumps(notices))




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


def _runtime():
    """Everything an entry point works on, read off the environment once.

    Entry points take one rather than building their own, so a caller can hand
    over a sandbox instead of reaching the real keychain and account files.
    """
    paths, store, state_dir = _paths()
    return Runtime(
        paths=paths, store=store, state_dir=state_dir, config=load_config(_config_path())
    )


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


PREVIOUS_STATUSLINE = "statusline.previous.json"
STATUSLINE_TIMEOUT_SECONDS = 5
LOG = "grazr.log"


def statusline(runtime=None, payload=None, spawn=subprocess.run, detach=None):
    """Runs inside Claude after every message, with the status-line payload on
    stdin. The bar stays the one configured before grazr. Claude cancels this
    command when the next update arrives, so the swap runs detached, in
    `decide`, where a kill cannot leave it half done."""
    payload = sys.stdin.read() if payload is None else payload
    runtime = runtime or _runtime()
    paths, store, state_dir, config = runtime
    print(_previous_bar(state_dir, payload, spawn), end="", flush=True)

    if not config.enabled:
        return 0
    limits = claude.statusline_limits(payload)
    if limits is None:
        _warn_unreadable(state_dir, payload)
        return 0
    active = claude.active_account(paths)
    if active is None or _left_behind(limits, active, accounts.load(paths, [])):
        return 0
    accounts.record_snapshot(paths, active, limits)
    if core.needs_rotation(limits, datetime.now(timezone.utc), config.thresholds):
        (detach or _detach_decide)()
    return 0


def _warn_unreadable(state_dir, payload):
    """A Claude release that renames the field would leave grazr blind in
    silence. A session that has not reached the API yet has no limits to send,
    so only one that has counts."""
    try:
        sent = json.loads(payload)
        spoken = sent["context_window"]["total_input_tokens"] > 0
        version = sent.get("version", "unknown")
    except (ValueError, KeyError, TypeError, AttributeError):
        return
    if not spoken:
        return
    line = "Claude %s sends no rate limits in its status line, so grazr sees no usage" % version
    if _announce_once(state_dir, "unreadable:%s" % version, "grazr: cannot read Claude's usage", line):
        _log(state_dir, datetime.now(timezone.utc), line)


def _detach_decide():
    with open(os.path.join(_paths()[2], LOG), "a") as log:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "decide"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
            start_new_session=True,
        )


def decide(runtime=None):
    """Swap if the latest reading says so. Runs detached from the status line
    that recorded it, so Claude cancelling that cannot stop a swap half way."""
    runtime = runtime or _runtime()
    paths, store, state_dir, config = runtime
    now = datetime.now(timezone.utc)
    with _rotation_lock(state_dir) as acquired:
        if not acquired:
            return 0
        active = claude.active_account(paths)
        limits = _latest_reading(paths, active)
        if limits is None:
            return 0
        enrolled = accounts.load(paths, config.accounts)
        decision = core.decide(limits, active, enrolled, now, config.thresholds)
        # rotate refuses this too, but a raised error suits the person who just
        # pressed a key, not every message of every pane.
        if isinstance(decision, tuple):
            override = claude.settings_auth_override(paths.config_dir)
            if override:
                decision = ("override", override)
        try:
            line = act_on(decision, runtime, active, limits, enrolled, now)
        except RuntimeError as refusal:
            # A busy Claude lock. The next message brings the next try.
            line = "not rotating: %s" % refusal
            decision = "stay"

    # Two herdr calls per pane, capped at five seconds each, is far too long to
    # hold the lock every other pane is waiting on.
    if _moved(decision, config.dry_run):
        tag_all(_name_of(enrolled, decision[1]))
    if line:
        _log(state_dir, now, line)
    return 0


def _latest_reading(paths, active):
    return next((entry.snapshot for entry in accounts.load(paths, []) if entry.id == active), None)


def _previous_bar(state_dir, payload, spawn):
    try:
        with open(os.path.join(state_dir, PREVIOUS_STATUSLINE)) as handle:
            command = (json.load(handle)["previous"] or {}).get("command")
    except (OSError, ValueError, KeyError, AttributeError):
        return ""
    if not command:
        return ""
    try:
        return spawn(
            command, shell=True, input=payload, capture_output=True, text=True,
            timeout=STATUSLINE_TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _left_behind(limits, active, enrolled):
    """A session keeps reporting the account it left until its next request.
    That window's reset time is parked in the old account's snapshot, which is
    how such a payload is told apart."""
    resets = {
        entry.resets_at.replace(microsecond=0)
        for entry in limits
        if entry.group == "session" and entry.resets_at
    }
    return any(
        entry.id != active
        and isinstance(entry.snapshot, list)
        and any(
            parked.group == "session"
            and parked.resets_at
            and parked.resets_at.replace(microsecond=0) in resets
            for parked in entry.snapshot
        )
        for entry in enrolled
    )


def _log(state_dir, now, line):
    """One line per decision, not one per message it holds for."""
    path = os.path.join(state_dir, LOG)
    try:
        with open(path) as handle:
            last = handle.read().rstrip("\n").rsplit("\n", 1)[-1]
    except OSError:
        last = ""
    if last.endswith(" " + line):
        return
    with open(path, "a") as handle:
        handle.write("%s %s\n" % (now.astimezone().strftime("%Y-%m-%d %H:%M:%S"), line))


def _rotation_lock(state_dir):
    """One pane at a time past the threshold. Several panes go idle together
    when a long turn ends, and each would otherwise park over the others."""
    return _file_lock(os.path.join(state_dir, "rotate.lock"))


@contextlib.contextmanager
def _file_lock(path):
    # "w" would truncate before flock is even attempted, so a pane that goes on
    # to lose the race still writes to the file.
    handle = open(path, "a")
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
    entry_points = {
        "statusline": statusline, "decide": decide, "install": install, "uninstall": uninstall,
        "status": status, "enrol": enrol, "swap": swap, "tag": tag,
    }
    command = argv[1] if len(argv) > 1 else ""
    if command not in entry_points:
        print("usage: grazr.py %s" % "|".join(entry_points), file=sys.stderr)
        return 2
    return entry_points[command]()


def tag(runtime=None):
    """Name the account being spent. The pane event carries a pane id and tags
    that pane, so a new pane is not blank until the next rotation. The action
    carries none and tags every pane, which is what you press after adding the
    sidebar row. A pane start is also when to notice that the status line is no
    longer grazr's, since without it grazr sees nothing."""
    paths, _, state_dir, _ = runtime or _runtime()
    active = claude.active_account(paths)
    if active is None:
        return 0
    named = next(
        (entry.name for entry in accounts.load(paths, []) if entry.id == active),
        active,
    )
    pane_id = os.environ.get("HERDR_PANE_ID")
    if pane_id:
        tag_pane(pane_id, named)
    else:
        tag_all(named)
    if not claude.statusline_installed(paths.config_dir, _record_path(state_dir)):
        line = "the status line is not grazr's, so grazr sees no usage; run the connect action"
        if _announce_once(state_dir, "statusline-missing", "grazr: status line not connected", line):
            print(line)
    return 0


def install(runtime=None):
    paths, _, state_dir, _ = runtime or _runtime()
    print(claude.install_statusline(paths.config_dir, _record_path(state_dir), _shim_command(state_dir)))
    return 0


def uninstall(runtime=None):
    paths, _, state_dir, _ = runtime or _runtime()
    print(claude.uninstall_statusline(paths.config_dir, _record_path(state_dir)))
    return 0


def _record_path(state_dir):
    return os.path.join(state_dir, PREVIOUS_STATUSLINE)


def _shim_command(state_dir):
    """The status line runs in Claude's environment, not herdr's, so the
    command carries the directories herdr would have set."""
    return "HERDR_PLUGIN_STATE_DIR=%s HERDR_PLUGIN_CONFIG_DIR=%s python3 %s statusline" % (
        shlex.quote(state_dir),
        shlex.quote(os.path.dirname(_config_path())),
        shlex.quote(os.path.abspath(__file__)),
    )


def swap(runtime=None):
    """A swap the user asked for, from a Herdr key. The active account's
    headroom is not consulted, and ENABLED gates the status line only."""
    runtime = runtime or _runtime()
    paths, store, state_dir, config = runtime
    now = datetime.now(timezone.utc)
    with _rotation_lock(state_dir) as acquired:
        if not acquired:
            return _refuse_swap("busy rotating already, try again in a moment")
        active = claude.active_account(paths)
        if active is None:
            return _refuse_swap("not logged in, nothing to swap from")
        limits = _latest_reading(paths, active)
        enrolled = accounts.load(paths, config.accounts)
        next_id = core.next_account(active, enrolled, now, config.thresholds)
        if next_id is None:
            soonest = _soonest_reset(now, *(entry.snapshot for entry in enrolled))
            return _refuse_swap(
                "nothing to swap to, earliest reset %s" % (soonest or "unknown")
            )
        decision = ("rotate", next_id)
        print(
            act_on(decision, runtime, active, limits, enrolled, now)
        )

    if _moved(decision, config.dry_run):
        tag_all(_name_of(enrolled, next_id))
    return 0


def _refuse_swap(reason):
    """The user pressed a key and is watching, so a refusal goes to the screen
    as well as the log."""
    print("grazr: %s" % reason)
    notify("grazr: no swap", reason)
    return 1


def status(runtime=None):
    paths, store, state_dir, config = runtime or _runtime()
    active = claude.active_account(paths)

    print("config: %s" % _config_path())
    print("thresholds: session %d%% / weekly %d%% remaining%s\n"
          % (config.thresholds["session"], config.thresholds["weekly"],
             "   DRY_RUN" if config.dry_run else ""))

    enrolled = accounts.load(paths, config.accounts)
    for account in enrolled:
        marker = "*" if account.id == active else " "
        parked = "parked" if accounts.has_parked_credential(store, account.id) else "NO CREDENTIAL"
        print("%s %-16s %-14s %s" % (marker, account.name, parked, _describe(account.snapshot)))

    for name in config.accounts:
        if not any(account.name == name for account in enrolled):
            print("  %-16s not enrolled, so it is never used" % name)

    print("\nactive account headroom: %s" % _describe(_latest_reading(paths, active)))
    if not claude.statusline_installed(paths.config_dir, _record_path(state_dir)):
        print("  the status line is not grazr's, so grazr sees no usage; run the connect action")
    if active and not any(account.id == active for account in enrolled):
        # Enrolled but left out of ACCOUNTS is the likelier mistake, and calling
        # that "not enrolled" sends you off to enrol it a second time.
        enrolled = any(account.id == active for account in accounts.load(paths, []))
        print("  this login is %s; grazr can rotate away but not back"
              % ("enrolled but missing from ACCOUNTS" if enrolled else "not enrolled"))

    last = _last_decision(state_dir)
    if last:
        print("\nlast reported: %s" % last)
    print("\n(* = active)   press any key to close")
    read_key()
    return 0


def _last_decision(state_dir):
    notices = _read_notices(state_dir)
    if not notices:
        return None
    key, notified = list(notices.items())[-1]
    return "%s%s" % (key, "" if notified else "  (toast never appeared)")


def _describe(snapshot):
    if snapshot is None:
        return "no reading yet"
    return ", ".join(
        "%s %d%% left" % (entry.kind, entry.remaining) for entry in snapshot
    ) or "no limits reported"


def enrol(runtime=None):
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

    runtime = runtime or _runtime()
    paths = runtime.paths
    if choice == "s":
        return _enrol_from(runtime, None)

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
        return _enrol_from(runtime, source)
    finally:
        if not claude.discard_isolated_login(runtime.store, source):
            print("warning: could not remove the throwaway login from the keychain")


def _enrol_from(runtime, source):
    paths, store, state_dir, _ = runtime
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
    print(claude.install_statusline(paths.config_dir, _record_path(state_dir), _shim_command(state_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
