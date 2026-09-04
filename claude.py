import contextlib
import json
import os
import shutil
import tempfile
import time
import urllib.request
import uuid
from collections import namedtuple
from datetime import datetime, timezone

import core

# Claude treats its cached usage as stale past this age (OZr in the 2.1.260 binary).
USAGE_STALE_AFTER_MS = 3_600_000

# proper-lockfile options Claude uses for .oauth_refresh.lock in the same binary.
OAUTH_LOCK_STALE_MS = 60_000

# Claude's saveConfigWithLock builds this path at runtime, so the name is not
# greppable. Its stale age is unpublished, so this matches the other lock.
CONFIG_LOCK_STALE_MS = 60_000

# What Claude's own fetchUtilization calls, with the headers and the 5s budget
# it uses. The body it gets back is what it caches verbatim as `utilization`.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_BETA = "oauth-2025-04-20"
USAGE_TIMEOUT_SECONDS = 5


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


def read_limits(config, now):
    """Claude's cached usage, or "unknown" when it cannot be trusted: it may
    describe an account we already left, or predate the last refresh."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware or the freshness guard fails open")
    usage = config.get("cachedUsageUtilization")
    if not isinstance(usage, dict):
        return "unknown"
    active = (config.get("oauthAccount") or {}).get("accountUuid")
    if not active or usage.get("accountUuid") != active:
        return "unknown"

    fetched_at = usage.get("fetchedAtMs")
    if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool):
        return "unknown"
    if now.timestamp() * 1000 - fetched_at > USAGE_STALE_AFTER_MS:
        return "unknown"
    return read_utilization(usage.get("utilization"))


def read_utilization(utilization):
    """The body Claude fetches and then caches verbatim, so the same reading
    serves whether it came off disk or off the wire."""
    if not isinstance(utilization, dict):
        return "unknown"
    if _is_locked(utilization):
        return "locked"

    entries = utilization.get("limits")
    if not isinstance(entries, list) or not entries:
        return "unknown"
    try:
        return [
            core.Limit(
                kind=entry["kind"],
                scope=entry.get("scope"),
                group=entry["group"],
                remaining=100 - entry["percent"],
                resets_at=_parse_time(entry.get("resets_at")),
            )
            for entry in entries
        ]
    except (AttributeError, KeyError, TypeError, ValueError):
        return "unknown"


def fetch_limits(store, opener=urllib.request.urlopen):
    """Ask the endpoint Claude asks, because its cache can sit unrefreshed for
    over an hour while a window is spent, and a reading that old is no reading
    at all. Returns None for "could not tell", which leaves the caller on
    whatever the cache said rather than stopping a turn end over it.

    Read-only: grazr spends the access token it already parks and never
    refreshes it. A 401 means Claude will refresh on its own next request.
    """
    blob = store.read_live()
    if blob is None:
        return None
    try:
        token = json.loads(blob)["claudeAiOauth"]["accessToken"]
    except (KeyError, TypeError, ValueError):
        return None

    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer %s" % token,
            "anthropic-beta": USAGE_BETA,
            "Content-Type": "application/json",
        },
    )
    try:
        with opener(request, timeout=USAGE_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        # No network, an expired token, a rate limit, a body that is not JSON.
        return None
    reading = read_utilization(payload)
    return None if reading == "unknown" else reading


def _is_locked(utilization):
    """A restriction the server placed on the account. The values are not
    documented, so any non-null one is read as "leave this account alone"."""
    return any(
        isinstance(bucket, dict) and bucket.get("locked_reason")
        for bucket in utilization.values()
    )


def _parse_time(value):
    """A bare timestamp would raise when compared against an aware `now`, deep
    inside the decision."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


Paths = namedtuple("Paths", "config_path config_dir accounts_dir")


def inspect(paths, now):
    """Who Claude is logged in as, and what that account has left."""
    try:
        with open(paths.config_path) as handle:
            config = json.load(handle)
    except (OSError, ValueError):
        # Not logged in, or read while Claude was rewriting the file.
        return None, "unknown"
    active = (config.get("oauthAccount") or {}).get("accountUuid")
    return active, read_limits(config, now)


def has_parked_credential(store, account_id):
    """An account whose parked credential went missing cannot be rotated to,
    and the only way to find out is to look."""
    return store.read_parked(account_id) is not None


def load_accounts(paths, names):
    """Enrolled accounts in the configured preference order. An unknown name is
    skipped rather than fatal, so a typo cannot stop rotation entirely."""
    by_name = {}
    for filename in sorted(os.listdir(paths.accounts_dir)):
        if filename.startswith(".") or not filename.endswith(".json"):
            continue
        identifier = filename[: -len(".json")]
        try:
            stored = _load_account(paths, identifier)
        except (OSError, ValueError):
            continue
        by_name[stored.get("name", identifier)] = core.Account(
            id=identifier,
            name=stored.get("name", identifier),
            snapshot=snapshot_from_json(stored.get("snapshot")),
        )
    if not names:
        return [by_name[name] for name in sorted(by_name)]
    return [by_name[name] for name in names if name in by_name]


def snapshot_to_json(snapshot):
    if snapshot is None or snapshot == "locked":
        return snapshot
    return [
        {
            "kind": entry.kind,
            "scope": entry.scope,
            "group": entry.group,
            "remaining": entry.remaining,
            "resets_at": entry.resets_at.isoformat() if entry.resets_at else None,
        }
        for entry in snapshot
    ]


def snapshot_from_json(stored):
    """Anything unrecognised reads as never parked. A hand-edited account file
    must not take rotation out for every account."""
    if stored is None or stored == "locked":
        return stored
    try:
        return [
            core.Limit(
                kind=entry["kind"],
                scope=entry["scope"],
                group=entry["group"],
                remaining=entry["remaining"],
                resets_at=_parse_time(entry["resets_at"]),
            )
            for entry in stored
        ]
    except (KeyError, TypeError, ValueError):
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
    account_id = _account_id(identity.get("accountUuid"))

    # Accounts are looked up by name, so a duplicate hides one of them for good.
    for existing in load_accounts(paths, []):
        if existing.name == name and existing.id != account_id:
            raise RuntimeError("the name %r is already used by another account" % name)

    store.write_parked(account_id, blob)
    _save_account(
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
    first write; a failure part-way leaves identity and credential disagreeing,
    which reads as "unknown" and stops every hook rather than swapping wrongly."""
    arriving = store.read_parked(next_id)
    if arriving is None:
        raise RuntimeError("no parked credential for %s; enrol it again" % next_id)
    identity = _load_account(paths, next_id)["oauthAccount"]

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
            store.write_live(arriving)
            renew()
        _merge_oauth_account(paths.config_path, identity)

    _record_snapshot(paths, active_id, snapshot)


def _account_id(value):
    """The server's account id becomes a filename and a keychain service, so it
    has to be a plain uuid and nothing else."""
    try:
        # Compared case-insensitively: some providers return uppercase, and it
        # is the same account. The canonical lowercase form is what gets used.
        canonical = str(uuid.UUID(str(value)))
        if canonical == value.lower():
            return canonical
    except (AttributeError, TypeError, ValueError):
        pass
    raise RuntimeError("that login has no usable account identity: %r" % (value,))


def _load_account(paths, account_id):
    with open(os.path.join(paths.accounts_dir, account_id + ".json")) as handle:
        return json.load(handle)


def _record_snapshot(paths, account_id, snapshot):
    """Best-effort: the swap has already happened, so an account we never
    enrolled is nothing to record against rather than a reason to fail."""
    try:
        account = _load_account(paths, account_id)
    except (OSError, ValueError):
        return
    account["snapshot"] = snapshot_to_json(snapshot)
    _save_account(paths, account_id, account)


def _save_account(paths, account_id, account):
    """Metadata only -- never a secret. The credential lives in the store."""
    handle, temporary = tempfile.mkstemp(dir=paths.accounts_dir, prefix=".grazr-")
    try:
        with os.fdopen(handle, "w") as writer:
            json.dump(account, writer, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, os.path.join(paths.accounts_dir, account_id + ".json"))
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _write_oauth_account(config_path, oauth_account):
    """Point ~/.claude.json at another identity. Read-modify-write preserving
    every other key, then one atomic replace, because Claude watches this file
    rather than locking it."""
    with _config_lock(config_path):
        _merge_oauth_account(config_path, oauth_account)


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

    directory = os.path.dirname(config_path) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".grazr-", suffix=".json")
    try:
        with os.fdopen(handle, "w") as writer:
            json.dump(config, writer)
        os.chmod(temporary, 0o600)
        os.replace(temporary, config_path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


