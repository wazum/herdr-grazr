from collections import namedtuple

Limit = namedtuple("Limit", "kind scope group remaining resets_at")
Account = namedtuple("Account", "id name snapshot")


def needs_rotation(limits, now, thresholds):
    return not _has_headroom(limits, now, thresholds)


def decide(limits, active, accounts, now, thresholds):
    if not needs_rotation(limits, now, thresholds):
        return "stay"
    chosen = next_account(active, accounts, now, thresholds)
    if chosen:
        return "rotate", chosen
    # "Every account is spent" would be a claim about accounts that do not exist.
    return "exhausted" if any(entry.id != active for entry in accounts) else "unenrolled"


def next_account(active, accounts, now, thresholds):
    """The first account in preference order fit to take over, or None. Says
    nothing about whether the active one needs replacing."""
    for candidate in accounts:
        if candidate.id == active:
            continue
        if candidate.snapshot is None or _has_headroom(candidate.snapshot, now, thresholds):
            return candidate.id
    return None


def _has_headroom(limits, now, thresholds):
    return all(
        limit.remaining >= thresholds[limit.group]
        for limit in limits
        if limit.group in thresholds and (limit.resets_at is None or limit.resets_at > now)
    )
