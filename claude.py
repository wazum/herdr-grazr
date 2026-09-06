import contextlib
import json
import os
import shutil
import time
from collections import namedtuple
from datetime import datetime, timezone

import accounts
import atomic
import core

# proper-lockfile options Claude uses for .oauth_refresh.lock in the same binary.
OAUTH_LOCK_STALE_MS = 60_000

# Claude's saveConfigWithLock builds this path at runtime, so the name is not
# greppable. Its stale age is unpublished, so this matches the other lock.
CONFIG_LOCK_STALE_MS = 60_000

# `mcpOAuth` holds a login per MCP server, minted against that server and not
# against a Claude account, so it survives a swap. An allowlist, not "everything
# except the login": a key Claude adds later must not start crossing accounts.
CARRIED_CREDENTIAL_KEYS = ("mcpOAuth",)

# `primaryApiKey` is a raw key Claude reads back as an auth source, so a
# leftover spends the outgoing account's key on the incoming one. The caches
# describe its plan and model access. Claude re-fetches each on demand, so
# dropping them costs a request. `cachedUsageUtilization` is left in place:
# read_limits already discards it when it names another account.
ACCOUNT_SCOPED_CONFIG_KEYS = (
    "primaryApiKey",
    "customApiKeyResponses",
    "overageCreditGrantCache",
    "passesEligibilityCache",
    "passesLastSeenRemaining",
    "cachedExtraUsageDisabledReason",
    "orgModelDefaultCache",
    "modelAccessCache",
    "additionalModelCostsCache",
    "additionalModelOptionsCache",
)


@contextlib.contextmanager
def _proper_lock(path, stale_ms, busy_message):
    """Mirrors proper-lockfile, so grazr and Claude exclude each other: acquire
    by mkdir, treat a holder past its stale age as abandoned, and on release
    leave behind any lock whose mtime changed, since that one is Claude's.
    """
    try:
        os.mkdir(path)
    except FileExistsError:
        try:
            age_ms = (time.time() - os.path.getmtime(path)) * 1000
        except FileNotFoundError:
            # Released between the two calls. There is no holder to respect,
            # so fall through to the retake below rather than raising.
            age_ms = stale_ms
        if age_ms < stale_ms:
            raise RuntimeError(busy_message)
        # Another process may be breaking the same lock. The loser must back
        # off rather than assume it holds it too.
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass
        try:
            os.mkdir(path)
        except FileExistsError:
            raise RuntimeError(busy_message)
    held = [os.path.getmtime(path)]

    def renew():
        """Claude refreshes its own lock every 5s. Slow work must do the same,
        or the lock looks abandoned and Claude takes it."""
        os.utime(path, None)
        held[0] = os.path.getmtime(path)

    try:
        yield renew
    finally:
        held_since = held[0]
        try:
            if os.path.getmtime(path) == held_since:
                os.rmdir(path)
        except OSError:
            pass


def _oauth_refresh_lock(config_dir):
    """Held across a swap so a token refresh cannot rewrite the live item."""
    return _proper_lock(
        os.path.join(config_dir, ".oauth_refresh.lock"),
        OAUTH_LOCK_STALE_MS,
        "Claude is refreshing its token; not swapping now",
    )


Paths = namedtuple("Paths", "config_path config_dir accounts_dir")


STATUSLINE_WINDOWS = (("five_hour", "session", "session"), ("seven_day", "weekly_all", "weekly"))


def statusline_limits(payload):
    """The limits Claude hands its status-line command after every message, for
    the token the session is using. None when the payload has none."""
    try:
        windows = json.loads(payload)["rate_limits"]
        return [
            core.Limit(
                kind=kind,
                scope=None,
                group=group,
                remaining=max(0, 100 - windows[window]["used_percentage"]),
                resets_at=_from_unix(windows[window].get("resets_at")),
            )
            for window, kind, group in STATUSLINE_WINDOWS
            if window in windows
        ]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _from_unix(seconds):
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    return datetime.fromtimestamp(seconds, timezone.utc)


def active_account(paths):
    try:
        with open(paths.config_path) as handle:
            return (json.load(handle).get("oauthAccount") or {}).get("accountUuid") or None
    except (OSError, ValueError, AttributeError):
        return None


def enrol(paths, store, name, source_config_dir=None):
    """Copy an existing login into grazr's keeping. Authentication is always
    Claude's own `claude auth login`; grazr never sees a password. `source_config_dir`
    is an isolated CLAUDE_CONFIG_DIR, so enrolling a second account never
    disturbs the live one."""
    config_path = (
        os.path.join(source_config_dir, ".claude.json")
        if source_config_dir
        else paths.config_path
    )
    blob = (
        store.read_isolated(source_config_dir) if source_config_dir else store.read_live()
    )
    if blob is None:
        raise RuntimeError("no login found; run `claude auth login` first")

    with open(config_path) as handle:
        identity = (json.load(handle).get("oauthAccount") or {})
    account_id = accounts.account_id(identity.get("accountUuid"))

    # Accounts are looked up by name, so a duplicate hides one of them for good.
    for existing in accounts.load(paths, []):
        if existing.name == name and existing.id != account_id:
            raise RuntimeError("the name %r is already used by another account" % name)

    store.write_parked(account_id, blob)
    accounts.write(
        paths,
        account_id,
        {
            "name": name,
            "email": identity.get("emailAddress"),
            "organization": identity.get("organizationName"),
            "oauthAccount": identity,
            "snapshot": None,
        },
    )
    return account_id


def discard_isolated_login(store, config_dir):
    """Remove the throwaway login enrolment used, stored credential and all.
    The copy grazr keeps is the parked one; this one is an untracked duplicate.

    Returns whether the credential is gone. Never raises: it is called from a
    finally, and masking the failure it is cleaning up after would hide the
    real problem.
    """
    removed = store.discard_isolated(config_dir)
    shutil.rmtree(config_dir, ignore_errors=True)
    return removed


def rotate(paths, store, active_id, next_id, snapshot):
    """Park the live credential under the account leaving, install the one
    arriving, and move the identity with it. Every refusal happens before the
    first write, and a failure part-way is resumed by the next attempt rather
    than parked over."""
    override = settings_auth_override(paths.config_dir)
    if override:
        raise RuntimeError(
            "%s in settings.json puts Claude on API-key auth, so swapping the "
            "saved claude.ai login would change nothing; unset it to let grazr "
            "rotate" % override
        )

    arriving = store.read_parked(next_id)
    if arriving is None:
        raise RuntimeError("no parked credential for %s; enrol it again" % next_id)
    identity = accounts.read(paths, next_id)["oauthAccount"]

    # Both locks before the first write. The identity write comes last, but its
    # lock can be busy too, and refusing after the credential moved is the whole
    # thing this order prevents.
    with _config_lock(paths.config_path) as renew_config, _oauth_refresh_lock(
        paths.config_dir
    ) as renew_oauth:

        def renew():
            renew_config()
            renew_oauth()

        leaving = store.read_live()
        if leaving is None:
            raise RuntimeError("no live credential to park; refusing to swap")
        renew()
        # The live credential already holding the arriving blob means an earlier
        # try swapped the store and died before the identity write. Parking
        # again would overwrite the outgoing account's own credential.
        if leaving != arriving:
            store.write_parked(active_id, leaving)
            renew()
            try:
                store.write_live(_carry_shared_keys(arriving, leaving))
            except ValueError:
                # The store refuses a blob past its line length, and the carry
                # is what pushes it over. Losing the MCP logins beats leaving
                # the pane on a spent account.
                store.write_live(arriving)
            renew()
        _merge_oauth_account(paths.config_path, identity)

    accounts.record_snapshot(paths, active_id, snapshot)
    return _expired_note(arriving)


def _expired_note(blob):
    """What to say about a parked token that expired while it sat, or None.

    Claude refreshes an expired token on its next request, so this is not a
    refusal. But a refresh token spent somewhere else -- another machine, a
    `claude auth login` for the same account -- fails that refresh and Claude
    signs the account out, so the note is the warning.
    """
    try:
        expires_at = json.loads(blob)["claudeAiOauth"]["expiresAt"]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
        return None
    # Claude writes this as epoch milliseconds.
    if expires_at > time.time() * 1000:
        return None
    return "; its token had expired, so Claude has to refresh it"


def settings_auth_override(config_dir):
    """The setting in settings.json that puts Claude on API-key auth, or None.

    Claude reaches every one of these before the saved claude.ai login, so a
    swap underneath one changes nothing while the log says it worked. Its own
    words in the 2.1.261 binary: "ANTHROPIC_API_KEY is set, so this session is
    using API-key auth", and the same for the other two. `env` there is
    "Environment variables to set for Claude Code sessions", so a key parked in
    that block counts as set.
    """
    try:
        with open(os.path.join(config_dir, "settings.json")) as handle:
            settings = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    if str(settings.get("apiKeyHelper") or "").strip():
        return "apiKeyHelper"
    env = settings.get("env")
    if isinstance(env, dict):
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if str(env.get(key) or "").strip():
                return "env.%s" % key
    return None


def install_statusline(config_dir, record_path, command):
    """Put grazr's shim in as Claude's status line and keep what was there.

    The record keeps the exact shim too, so a plugin checked out under a new
    path re-installs instead of passing as connected, and a status line the
    user replaced by hand is recognised as theirs.
    """
    settings = _read_settings(config_dir)
    current = settings.get("statusLine")
    recorded = _read_record(record_path)
    if isinstance(current, dict) and current.get("command") == command:
        return "status line already connected"
    ours_before = recorded and isinstance(current, dict) and current.get("command") == recorded["shim"]
    previous = recorded["previous"] if ours_before else current
    atomic.write(record_path, json.dumps({"previous": previous, "shim": command}))
    settings["statusLine"] = {
        "type": "command",
        "command": command,
        "refreshInterval": (previous or {}).get("refreshInterval", 60),
    }
    _write_settings(config_dir, settings)
    return "status line connected"


def uninstall_statusline(config_dir, record_path):
    recorded = _read_record(record_path)
    if recorded is None:
        return "status line was not connected"
    settings = _read_settings(config_dir)
    if (settings.get("statusLine") or {}).get("command") != recorded["shim"]:
        return "status line was not grazr's, left alone"
    if recorded["previous"]:
        settings["statusLine"] = recorded["previous"]
    else:
        del settings["statusLine"]
    _write_settings(config_dir, settings)
    os.remove(record_path)
    return "status line disconnected"


def statusline_installed(config_dir, record_path):
    recorded = _read_record(record_path)
    if recorded is None:
        return False
    try:
        return (_read_settings(config_dir).get("statusLine") or {}).get("command") == recorded["shim"]
    except RuntimeError:
        return False


def _read_record(record_path):
    try:
        with open(record_path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _read_settings(config_dir):
    try:
        with open(os.path.join(config_dir, "settings.json")) as handle:
            settings = json.load(handle)
    except OSError:
        return {}
    except ValueError:
        raise RuntimeError("%s is not valid JSON, not touching it" % os.path.join(config_dir, "settings.json"))
    return settings if isinstance(settings, dict) else {}


def _write_settings(config_dir, settings):
    os.makedirs(config_dir, exist_ok=True)
    atomic.write(os.path.join(config_dir, "settings.json"), json.dumps(settings, indent=2) + "\n")


def _carry_shared_keys(arriving, leaving):
    """Move what belongs to no account onto the credential coming in, so a swap
    does not sign the user out of every MCP server.

    Best-effort: a blob that will not parse is installed as it stands. Refusing
    here would strand the pane on an account that has already run out.
    """
    try:
        outgoing, incoming = json.loads(leaving), json.loads(arriving)
    except ValueError:
        return arriving
    if not isinstance(outgoing, dict) or not isinstance(incoming, dict):
        return arriving
    carried = {key: outgoing[key] for key in CARRIED_CREDENTIAL_KEYS if key in outgoing}
    if not carried:
        # Byte for byte, rather than a re-serialisation that only moves whitespace.
        return arriving
    incoming.update(carried)
    return json.dumps(incoming)


def _config_lock(config_path):
    return _proper_lock(
        config_path + ".lock",
        CONFIG_LOCK_STALE_MS,
        "Claude is writing its config; not swapping now",
    )


def _merge_oauth_account(config_path, oauth_account):
    """Caller holds the config lock. Claude re-reads and merges under it too,
    so a copy taken before acquiring it could already be out of date."""
    with open(config_path) as handle:
        config = json.load(handle)
    config["oauthAccount"] = oauth_account
    for key in ACCOUNT_SCOPED_CONFIG_KEYS:
        config.pop(key, None)

    atomic.write(config_path, json.dumps(config))


