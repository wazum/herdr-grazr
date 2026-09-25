from collections import namedtuple

Limit = namedtuple("Limit", "kind scope group remaining resets_at")
Account = namedtuple("Account", "id name snapshot")


def merged(previous, current):
    """A window already on record keeps the lowest headroom it has shown. Idle
    panes repeat old figures, and a repeat must not put headroom back. A newer
    window is taken as it comes. An older one comes from a pane that has not
    caught up, and the window on record stays."""
    if not isinstance(previous, list):
        return current
    # Keyed by scope too: a per-model weekly limit shares its reset with the
    # all-models one, and must never lend it its lower headroom.
    lowest = {(entry.group, entry.scope, entry.resets_at): entry.remaining for entry in previous}
    newest = {
        (entry.kind, entry.group, entry.scope): entry
        for entry in sorted((entry for entry in previous if entry.resets_at), key=lambda entry: entry.resets_at)
    }
    readings = []
    for entry in current:
        recorded = newest.get((entry.kind, entry.group, entry.scope))
        if recorded and entry.resets_at and entry.resets_at < recorded.resets_at:
            readings.append(recorded)
            continue
        readings.append(
            entry._replace(
                remaining=min(
                    entry.remaining, lowest.get((entry.group, entry.scope, entry.resets_at), entry.remaining)
                )
            )
        )
    return readings


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
