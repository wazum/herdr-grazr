from collections import namedtuple

Limit = namedtuple("Limit", "kind scope group remaining resets_at")
Account = namedtuple("Account", "id name snapshot")

# Once every account is below the thresholds, another one must be this much
# better off before it is worth a swap, or two low accounts would trade places
# on every message. Going back for a week that ends sooner needs this much
# above the threshold, or it is not worth two swaps. The default; a caller can
# pass its own `margin`.
FALLBACK_MARGIN = 10

# A weekly window with at least this much left has just reset.
FRESH = 95

# How long before its reset an account's leftover is worth going back for.
LAST_DAY_SECONDS = 24 * 60 * 60

# A handover is only worth it when the other week ends at least this much
# sooner.
SOONER_BY_SECONDS = 60 * 60


def merged(previous, current, now):
    """A window already on record keeps the lowest headroom it has shown. Idle
    panes repeat old figures, and a repeat must not put headroom back. A newer
    window is taken as it comes. An older one comes from a pane that has not
    caught up, and the window on record stays. So does one the payload leaves
    out, since a window can be absent on its own. One that has run out is not
    taken at all, so a repeat cannot bring it back once it was dropped."""
    if not isinstance(previous, list):
        return _live(current, now)
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
    covered = {(entry.kind, entry.group) for entry in current}
    readings += [entry for entry in previous if (entry.kind, entry.group) not in covered]
    return _live(readings, now)


def moved(previous, current):
    """Whether `current` shows spending the record does not: a window lowered
    or a new one opened. A payload that merely lacks a window, or repeats one,
    shows none."""
    recorded = {
        (entry.kind, entry.group, entry.resets_at): entry.remaining
        for entry in (previous if isinstance(previous, list) else [])
    }
    return any(
        (entry.kind, entry.group, entry.resets_at) not in recorded
        or entry.remaining < recorded[(entry.kind, entry.group, entry.resets_at)]
        for entry in current
    )


def _live(limits, now):
    return [entry for entry in limits if entry.resets_at is None or entry.resets_at > now]


def replaced(previous, current, now):
    """The windows on record that have run out, by the clock or because
    `current` carries a newer one. Their last reading is what expired unused.
    The clock has to count, since a payload drops a window the moment its
    reset passes, and the newer window arrives only with the next request."""
    if not isinstance(previous, list):
        return []
    newest = {(entry.kind, entry.group): entry.resets_at for entry in current if entry.resets_at}
    return [
        entry
        for entry in previous
        if entry.resets_at
        and (entry.resets_at <= now or entry.resets_at < newest.get((entry.kind, entry.group), entry.resets_at))
    ]


def thresholds_of(account_id, thresholds, per_account=None):
    """The thresholds one account is held to: the global ones, with any that
    account sets for itself in `per_account`, which is keyed by account id.
    Every judgement about an account goes through here with that account's
    id, so a reserve kept on one account holds whichever account is active."""
    return dict(thresholds, **(per_account or {}).get(account_id, {}))


def needs_rotation(limits, now, thresholds):
    return not _has_headroom(limits, now, thresholds)


def decide(limits, active, accounts, now, thresholds, per_account=None, margin=FALLBACK_MARGIN):
    if not needs_rotation(limits, now, thresholds_of(active, thresholds, per_account)):
        sooner = expiring_sooner(limits, active, accounts, now, thresholds, per_account, margin)
        return ("rotate", sooner) if sooner else "stay"
    chosen = next_account(active, accounts, now, thresholds, per_account)
    if chosen:
        return "rotate", chosen
    # "Every account is spent" would be a claim about accounts that do not exist.
    others = [entry for entry in accounts if entry.id != active]
    if not others:
        return "unenrolled"

    def worst(account_id, snapshot):
        return _worst_window(snapshot, now, thresholds, thresholds_of(account_id, thresholds, per_account))

    best = max(others, key=lambda entry: worst(entry.id, entry.snapshot))
    ahead = worst(best.id, best.snapshot) - worst(active, limits)
    # A margin of nought must still not swap a tie, or two equally low
    # accounts would trade places on every message.
    if ahead >= margin and ahead > 0:
        return "rotate", best.id
    return "exhausted"


def _worst_window(limits, now, thresholds, own):
    """How close to the wall an account is: the least left on any live window
    that has a threshold. What an account holds back beyond the global
    threshold, `own` against `thresholds`, is a reserve and counts as spent,
    so the fallback does not pick an account for the reserve it keeps. An
    account that sets nothing of its own counts just what it has left."""
    return min(
        limit.remaining - own[limit.group] + thresholds.get(limit.group, own[limit.group])
        for limit in limits
        if limit.group in own and (limit.resets_at is None or limit.resets_at > now)
    )


def expiring_sooner(limits, active, accounts, now, thresholds, per_account=None, margin=FALLBACK_MARGIN):
    """The first account fit to take over whose week ends before the active
    account's, or None. What it has left is lost at its reset unless spent
    first. So that this does not undo every session swap five hours later, it
    fires only when the active week has just reset or the other account's
    week is in its last day, and only when enough is left there to be worth
    two swaps. A lightly used week reads as fresh for days, so the floor
    applies to both."""
    weeks = [limit for limit in limits if limit.group == "weekly" and limit.resets_at and limit.resets_at > now]
    if not weeks:
        return None
    ends = min(limit.resets_at for limit in weeks)
    fresh = any(limit.remaining >= FRESH for limit in weeks)
    for candidate in accounts:
        if candidate.id == active or not isinstance(candidate.snapshot, list):
            continue
        own = thresholds_of(candidate.id, thresholds, per_account)
        if not _has_headroom(candidate.snapshot, now, own):
            continue
        for limit in candidate.snapshot:
            if limit.group != "weekly" or not limit.resets_at or not now < limit.resets_at:
                continue
            if (ends - limit.resets_at).total_seconds() < SOONER_BY_SECONDS:
                continue
            worth_it = limit.remaining >= own.get("weekly", 0) + margin
            last_day = (limit.resets_at - now).total_seconds() <= LAST_DAY_SECONDS
            if worth_it and (fresh or last_day):
                return candidate.id
    return None


def next_account(active, accounts, now, thresholds, per_account=None):
    """The first account in preference order fit to take over, or None. Says
    nothing about whether the active one needs replacing. Each candidate is
    held to its own thresholds, never to the active account's."""
    for candidate in accounts:
        if candidate.id == active:
            continue
        if candidate.snapshot is None or _has_headroom(
            candidate.snapshot, now, thresholds_of(candidate.id, thresholds, per_account)
        ):
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
