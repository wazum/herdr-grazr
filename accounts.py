"""The accounts grazr enrolled, as files under its own state directory.

Nothing here is Claude's. An entry holds an identity from a Claude login, but
the file, its name and the parked snapshot are grazr's own.
"""

import json
import os
import uuid
from datetime import datetime, timezone

import atomic
import core


def parse_time(value):
    """A bare timestamp would raise when compared against an aware `now`, deep
    inside the decision. It lives here rather than in claude.py so this module
    depends on nothing above it."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def account_id(value):
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


def has_parked_credential(store, identifier):
    """An account whose parked credential went missing cannot be rotated to,
    and the only way to find out is to look."""
    return store.read_parked(identifier) is not None


def load(paths, names):
    """Enrolled accounts in the configured preference order. An unknown name is
    skipped rather than fatal, so a typo cannot stop rotation entirely."""
    by_name = {}
    for filename in sorted(os.listdir(paths.accounts_dir)):
        if filename.startswith(".") or not filename.endswith(".json"):
            continue
        identifier = filename[: -len(".json")]
        try:
            stored = read(paths, identifier)
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


def read(paths, identifier):
    with open(os.path.join(paths.accounts_dir, identifier + ".json")) as handle:
        return json.load(handle)


def write(paths, identifier, account):
    """Metadata only -- never a secret. The credential lives in the store."""
    atomic.write(
        os.path.join(paths.accounts_dir, identifier + ".json"),
        json.dumps(account, indent=2),
    )


def record_snapshot(paths, identifier, snapshot):
    """Best-effort: the swap has already happened, so an account we never
    enrolled is nothing to record against rather than a reason to fail."""
    try:
        account = read(paths, identifier)
    except (OSError, ValueError):
        return
    account["snapshot"] = snapshot_to_json(snapshot)
    write(paths, identifier, account)


def snapshot_to_json(snapshot):
    if not isinstance(snapshot, list):
        return None
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
    if stored is None:
        return None
    try:
        return [
            core.Limit(
                kind=entry["kind"],
                scope=entry["scope"],
                group=entry["group"],
                remaining=entry["remaining"],
                resets_at=parse_time(entry["resets_at"]),
            )
            for entry in stored
        ]
    except (KeyError, TypeError, ValueError):
        return None
