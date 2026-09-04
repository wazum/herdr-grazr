from collections import namedtuple

Limit = namedtuple("Limit", "kind scope group remaining resets_at")
Account = namedtuple("Account", "id name snapshot")


def needs_rotation(limits, now, thresholds):
    """Whether the active account is in any state worth acting on. Cheap and
    account-free, so the caller can skip loading the account store on the
    common path, which runs on every turn end of every pane."""
    if limits == "unknown":
        return False
    if limits == "locked":
        return True
    return not _has_headroom(limits, now, thresholds)


def decide(limits, active, accounts, now, thresholds):
    if not needs_rotation(limits, now, thresholds):
        return "stay"
    if limits == "locked":
        # Moving off a restricted account is circumventing the restriction.
        return "locked"
    for candidate in accounts:
        if candidate.id == active or candidate.snapshot == "locked":
            continue
        if candidate.snapshot is None or _has_headroom(candidate.snapshot, now, thresholds):
            return "rotate", candidate.id
    return "exhausted"


def _has_headroom(limits, now, thresholds):
    return all(
        limit.remaining >= thresholds[limit.group]
        for limit in limits
        if limit.group in thresholds and (limit.resets_at is None or limit.resets_at > now)
    )
