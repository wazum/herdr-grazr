from collections import namedtuple

Limit = namedtuple("Limit", "kind scope group remaining resets_at")
Account = namedtuple("Account", "id name snapshot")

# Once every account is below the thresholds, another one must be this much
# better off before it is worth a swap, or two low accounts would trade places
# on every message.
FALLBACK_MARGIN = 10

# A weekly window with at least this much left has just reset.
FRESH = 95


def merged(previous, current):
    """A window already on record keeps the lowest headroom it has shown. Idle
    panes repeat old figures, and a repeat must not put headroom back. A newer
    window is taken as it comes. An older one comes from a pane that has not
    caught up, and the window on record stays."""
    if not isinstance(previous, list):
        return current
    lowest = {(entry.group, entry.resets_at): entry.remaining for entry in previous}
    newest = {
        (entry.kind, entry.group): entry
        for entry in sorted((entry for entry in previous if entry.resets_at), key=lambda entry: entry.resets_at)
    }
    readings = []
    for entry in current:
        recorded = newest.get((entry.kind, entry.group))
        if recorded and entry.resets_at and entry.resets_at < recorded.resets_at:
            readings.append(recorded)
            continue
        readings.append(
            entry._replace(
                remaining=min(entry.remaining, lowest.get((entry.group, entry.resets_at), entry.remaining))
            )
        )
    return readings


def replaced(previous, current):
    """The windows on record that `current` supersedes with a newer one. Their
    last reading is what expired unused."""
    if not isinstance(previous, list):
        return []
    newest = {(entry.kind, entry.group): entry.resets_at for entry in current if entry.resets_at}
    return [
        entry
        for entry in previous
        if entry.resets_at and entry.resets_at < newest.get((entry.kind, entry.group), entry.resets_at)
    ]


def needs_rotation(limits, now, thresholds):
    return not _has_headroom(limits, now, thresholds)


def decide(limits, active, accounts, now, thresholds):
    if not needs_rotation(limits, now, thresholds):
        sooner = expiring_sooner(limits, active, accounts, now, thresholds)
        return ("rotate", sooner) if sooner else "stay"
    chosen = next_account(active, accounts, now, thresholds)
    if chosen:
        return "rotate", chosen
    # "Every account is spent" would be a claim about accounts that do not exist.
    others = [entry for entry in accounts if entry.id != active]
    if not others:
        return "unenrolled"
    best = max(others, key=lambda entry: _worst_window(entry.snapshot, now, thresholds))
    if _worst_window(best.snapshot, now, thresholds) >= _worst_window(limits, now, thresholds) + FALLBACK_MARGIN:
        return "rotate", best.id
    return "exhausted"


def _worst_window(limits, now, thresholds):
    """How close to the wall an account is: the least left on any live window
    that has a threshold."""
    return min(
        limit.remaining
        for limit in limits
        if limit.group in thresholds and (limit.resets_at is None or limit.resets_at > now)
    )


def fresh_weeks(limits, now):
    """When the weekly windows that have just reset end. Cheap and account-free,
    like needs_rotation, since it is asked on every message."""
    return [
        limit.resets_at
        for limit in limits
        if limit.group == "weekly" and limit.remaining >= FRESH and limit.resets_at and limit.resets_at > now
    ]


def expiring_sooner(limits, active, accounts, now, thresholds):
    """Once the active account's week is fresh, the first account fit to take
    over whose week ends before it, or None. Spending that one first is what
    keeps its leftover from expiring unused. Only a fresh week hands over, so
    this fires at a reset and not on every message."""
    fresh = fresh_weeks(limits, now)
    if not fresh:
        return None
    ends = min(fresh)
    for candidate in accounts:
        if candidate.id == active or not isinstance(candidate.snapshot, list):
            continue
        if not _has_headroom(candidate.snapshot, now, thresholds):
            continue
        if any(
            limit.group == "weekly" and limit.resets_at and now < limit.resets_at < ends
            for limit in candidate.snapshot
        ):
            return candidate.id
    return None


def next_account(active, accounts, now, thresholds):
    """The first account in preference order fit to take over, or None. Says
    nothing about whether the active one needs replacing."""
    for candidate in accounts:
        if candidate.id == active:
            continue
        if candidate.snapshot is None or _has_headroom(candidate.snapshot, now, thresholds):
            return candidate.id
    return None


def shortfall(limits, now, thresholds):
    """The first live window below its threshold, or None."""
    return next(
        (
            limit
            for limit in limits
            if limit.group in thresholds
            and limit.remaining < thresholds[limit.group]
            and (limit.resets_at is None or limit.resets_at > now)
        ),
        None,
    )


def _has_headroom(limits, now, thresholds):
    return shortfall(limits, now, thresholds) is None
