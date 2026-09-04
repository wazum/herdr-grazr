import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
from collections import namedtuple
from datetime import datetime, timezone

import core

SERVICE = "Claude Code-credentials"

# Measured 2026-09-04. At 4096 security truncates the line, writes the
# truncated prefix over the item, and only then exits 1.
MAX_SECURITY_LINE = 4095

# Claude treats its cached usage as stale past this age (OZr in the 2.1.260 binary).
USAGE_STALE_AFTER_MS = 3_600_000

# proper-lockfile options Claude uses for .oauth_refresh.lock in the same binary.
OAUTH_LOCK_STALE_MS = 60_000

# Claude's saveConfigWithLock builds this path at runtime, so the name is not
# greppable. Its stale age is unpublished, so this matches the other lock.
CONFIG_LOCK_STALE_MS = 60_000

# A swap makes three of these calls. All three at full timeout must still fit
# inside the lock stale ages.
SECURITY_TIMEOUT_SECONDS = 15


@contextlib.contextmanager
def _proper_lock(path, stale_ms, busy_message):
    """Mirrors proper-lockfile, so grazr and Claude exclude each other: acquire
    by mkdir, treat a holder past its stale age as abandoned, and on release
    leave behind any lock whose mtime changed, since that one is Claude's.
    """
    try:
        os.mkdir(path)
    except FileExistsError:
        if (time.time() - os.path.getmtime(path)) * 1000 < stale_ms:
            raise RuntimeError(busy_message)
        os.rmdir(path)
        os.mkdir(path)
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


def _run_security(line, spawn=subprocess.run):
    """Feed one command to `security -i`. The secret rides stdin, argv stays clean."""
    try:
        completed = spawn(
            ["security", "-i"],
            input=line + "\n",
            capture_output=True,
            text=True,
            timeout=SECURITY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("the keychain did not answer within %ds" % SECURITY_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        # security echoes the tail of the blob on a truncated line, and this
        # message reaches the plugin log.
        scrubbed = re.sub(r"[0-9a-f]{16,}", "<hex>", completed.stderr.strip()[:200])
        raise RuntimeError("security refused the command: %s" % scrubbed)
    return completed.stdout


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

    # One broad guard. Claude's shape can change in any release, and a traceback
    # on every turn end of every pane is worse than doing nothing.
    try:
        fetched_at = usage["fetchedAtMs"]
        if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool):
            return "unknown"
        if now.timestamp() * 1000 - fetched_at > USAGE_STALE_AFTER_MS:
            return "unknown"

        utilization = usage.get("utilization")
        if not isinstance(utilization, dict):
            return "unknown"
        if _is_locked(utilization):
            return "locked"

        entries = utilization.get("limits")
        if not isinstance(entries, list) or not entries:
            return "unknown"
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


Paths = namedtuple("Paths", "config_path config_dir accounts_dir keychain_account")


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


def has_parked_credential(paths, account_id):
    """An account whose parked credential went missing cannot be rotated to,
    and the only way to find out is to look."""
    return _read_credential(_parked_service(account_id), paths.keychain_account) is not None


def load_accounts(paths, names):
    """Enrolled accounts in the configured preference order. An unknown name is
    skipped rather than fatal, so a typo cannot stop rotation entirely."""
    by_name = {}
    for filename in sorted(os.listdir(paths.accounts_dir)):
        if filename.startswith(".") or not filename.endswith(".json"):
            continue
        identifier = filename[: -len(".json")]
        stored = _load_account(paths, identifier)
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


def enrol(paths, name, source_config_dir=None):
    """Copy an existing login into grazr's keeping. Authentication is always
    Claude's own `claude auth login`; grazr never sees a password. `source_config_dir`
    is an isolated CLAUDE_CONFIG_DIR, so enrolling a second account never
    disturbs the live one."""
    service = service_name(source_config_dir)
    config_path = (
        os.path.join(source_config_dir, ".claude.json")
        if source_config_dir
        else paths.config_path
    )
    blob = _read_credential(service, paths.keychain_account)
    if blob is None:
        raise RuntimeError("no login found for %s; run `claude auth login` first" % service)

    with open(config_path) as handle:
        identity = (json.load(handle).get("oauthAccount") or {})
    account_id = _account_id(identity.get("accountUuid"))

    # Accounts are looked up by name, so a duplicate hides one of them for good.
    for existing in load_accounts(paths, []):
        if existing.name == name and existing.id != account_id:
            raise RuntimeError("the name %r is already used by another account" % name)

    _install_credential(_parked_service(account_id), paths.keychain_account, blob)
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


def discard_isolated_login(paths, config_dir, spawn=subprocess.run):
    """Remove the throwaway login enrolment used, keychain item and all. The
    copy grazr keeps is the parked one; this one is an untracked duplicate.

    Returns whether the keychain item is gone. Never raises: it is called from
    a finally, and masking the failure it is cleaning up after would hide the
    real problem.
    """
    removed = True
    try:
        completed = spawn(
            [
                "security",
                "delete-generic-password",
                "-s",
                service_name(config_dir),
                "-a",
                paths.keychain_account,
            ],
            capture_output=True,
            timeout=SECURITY_TIMEOUT_SECONDS,
        )
        removed = getattr(completed, "returncode", 0) == 0
    except (OSError, subprocess.TimeoutExpired):
        removed = False
    shutil.rmtree(config_dir, ignore_errors=True)
    return removed


def rotate(paths, active_id, next_id, snapshot):
    """Park the live credential under the account leaving, install the one
    arriving, and move the identity with it. Every refusal happens before the
    first write; a failure part-way leaves identity and credential disagreeing,
    which reads as "unknown" and stops every hook rather than swapping wrongly."""
    arriving = _read_credential(_parked_service(next_id), paths.keychain_account)
    if arriving is None:
        raise RuntimeError("no parked credential for %s; enrol it again" % next_id)
    identity = _load_account(paths, next_id)["oauthAccount"]

    # Both locks before the first write. The identity write comes last, but its
    # lock can be busy too, and refusing after the keychain moved is the whole
    # thing this order prevents.
    with _config_lock(paths.config_path) as renew_config, _oauth_refresh_lock(
        paths.config_dir
    ) as renew_oauth:

        def renew():
            renew_config()
            renew_oauth()

        leaving = _read_credential(SERVICE, paths.keychain_account)
        if leaving is None:
            raise RuntimeError("no live credential to park; refusing to swap")
        renew()
        # The live item already holding the arriving blob means an earlier try
        # swapped the keychain and died before the identity write. Parking again
        # would overwrite the outgoing account's own credential.
        if leaving != arriving:
            _install_credential(_parked_service(active_id), paths.keychain_account, leaving)
            renew()
            _install_credential(SERVICE, paths.keychain_account, arriving)
            renew()
        _merge_oauth_account(paths.config_path, identity)

    _record_snapshot(paths, active_id, snapshot)


def _account_id(value):
    """The server's account id becomes a filename and a keychain service, so it
    has to be a plain uuid and nothing else."""
    try:
        if str(uuid.UUID(str(value))) == value:
            return value
    except (AttributeError, TypeError, ValueError):
        pass
    raise RuntimeError("that login has no usable account identity: %r" % (value,))


def _parked_service(account_id):
    return "grazr-%s" % account_id


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
    """Metadata only -- never a secret. The credential lives in the keychain."""
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


def _read_credential(service, account, spawn=subprocess.run):
    """The stored blob, or None when there is no such item. Service and account
    are not secrets, so argv is fine here; the blob comes back on stdout."""
    try:
        completed = spawn(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
            timeout=SECURITY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("the keychain did not answer within %ds" % SECURITY_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        return None
    stored = completed.stdout.strip()
    try:
        return bytes.fromhex(stored).decode()
    except (ValueError, UnicodeDecodeError):
        return stored


def _install_credential(service, account, blob, run=None):
    # `security -i` splits its line into tokens and the live service name has a
    # space, so the names need quoting. The hex payload never does.
    line = "add-generic-password -U -s %s -a %s -X %s" % (
        shlex.quote(service),
        shlex.quote(account),
        blob.encode().hex(),
    )
    size = len(line.encode())
    if size > MAX_SECURITY_LINE:
        raise ValueError(
            "credential for %s needs a %d byte security line, over the %d byte limit; "
            "installing it would truncate and destroy the item"
            % (service, size, MAX_SECURITY_LINE)
        )
    (run or _run_security)(line)


def service_name(config_dir=None):
    if config_dir is None:
        return SERVICE
    normalized = unicodedata.normalize("NFC", config_dir)
    return "%s-%s" % (SERVICE, hashlib.sha256(normalized.encode()).hexdigest()[:8])
