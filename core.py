from collections import namedtuple

Limit = namedtuple("Limit", "kind scope group remaining resets_at")
Account = namedtuple("Account", "id name snapshot")

# Once every account is below the thresholds, another one must be this much
# better off before it is worth a swap, or two low accounts would trade places
# on every message.
FALLBACK_MARGIN = 10

# Going back for a week that ends sooner needs this much above the threshold,
# or it is not worth two swaps. The default; a caller can pass its own
# `return_margin`. It never applies to the fallback above.
RETURN_MARGIN = 10

# Why the server refuses paid extra usage to an account. "org_spend_cap_reached"
# on a Team or Enterprise seat is the member's own spend cap ("You've hit your
# individual spend limit"). These belong to one member, so another account may
# still have extra usage. Where the reason is read from is the adapter's affair.
MEMBER_CAP_REASONS = frozenset({
    "org_spend_cap_reached",
    "member_level_disabled",
    "member_zero_credit_limit",
    "seat_tier_level_disabled",
    "seat_tier_zero_credit_limit",
    "group_zero_credit_limit",
})
# These hold for the whole organisation, so every account in it is capped too.
ORG_CAP_REASONS = frozenset({
    "out_of_credits",
    "org_level_disabled",
    "org_level_disabled_until",
    "org_service_level_disabled",
    "overage_not_provisioned",
    "no_limits_configured",
})

# A weekly window with at least this much left has just reset.
FRESH = 95

# How long before its reset an account's leftover is worth going back for.
LAST_DAY_SECONDS = 24 * 60 * 60

# A handover is only worth it when the other week ends at least this much
# sooner.
SOONER_BY_SECONDS = 60 * 60

# An account's own thresholds keep a reserve for its week, so they end just before
# the week does. Whatever is still held back then would expire unused.
RESERVE_ENDS_SECONDS = 60 * 60


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


def reserve_ended(limits, now):
    """Whether this account's week is in its last hour, so its reserve is free to
    spend. An account with no reading has no known week, and keeps its reserve."""
    for limit in limits if isinstance(limits, list) else []:
        if limit.kind == "weekly_all" and limit.resets_at and now < limit.resets_at:
            return (limit.resets_at - now).total_seconds() <= RESERVE_ENDS_SECONDS
    return False


def needs_rotation(limits, now, thresholds):
    return not _has_headroom(limits, now, thresholds)


def decide(limits, active, accounts, now, thresholds, per_account=None, return_margin=RETURN_MARGIN, capped=frozenset()):
    if not needs_rotation(limits, now, thresholds_of(active, thresholds, per_account)):
        sooner = expiring_sooner(limits, active, accounts, now, thresholds, per_account, return_margin)
        return ("rotate", sooner) if sooner else "stay"
    chosen = next_account(active, accounts, now, thresholds, per_account)
    if chosen:
        return "rotate", chosen
    # "Every account is spent" would be a claim about accounts that do not exist.
    others = [entry for entry in accounts if entry.id != active]
    if not others:
        return "unenrolled"
    # Thresholds an account sets for itself are a floor. Every other account is
    # below its thresholds by now, so one with a floor of its own is never
    # moved into, and an active one below its floor is left whatever the
    # margin: an account without a reserve is drained before one with it.
    drainable = [entry for entry in others if not _keeps_floor(entry.id, thresholds, per_account)]
    if not drainable:
        return "exhausted"
    best = max(drainable, key=lambda entry: _worst_window(entry.snapshot, now, thresholds))
    if _keeps_floor(active, thresholds, per_account):
        return "rotate", best.id
    if _worst_window(best.snapshot, now, thresholds) >= _worst_window(limits, now, thresholds) + FALLBACK_MARGIN:
        return "rotate", best.id
    rescue = extra_usage_fallback(active, drainable, now, thresholds, capped)
    if rescue:
        return "rotate", rescue
    return "exhausted"


def extra_usage_fallback(active, candidates, now, thresholds, capped):
    """When every account is below its thresholds, the active one keeps working
    on paid extra usage until its spend cap runs out, and then every request is
    refused. Moving to an account whose extra usage is not known to be spent
    keeps work going. Only fires once the active account is capped, and never
    moves into a capped account, so two capped accounts cannot trade places. An
    account never seen capped counts as uncapped: if it is, the next reading
    marks it and this moves on, at most once per account. The candidate with
    the most included usage left goes first, ties in preference order."""
    if active not in capped:
        return None
    free = [entry for entry in candidates if entry.id != active and entry.id not in capped]
    if not free:
        return None
    return max(free, key=lambda entry: _worst_window(entry.snapshot, now, thresholds, unknown=-1)).id


def _keeps_floor(account_id, thresholds, per_account):
    """Only a threshold above the global one holds something back. One below it
    asks for the account to be drained further."""
    own = (per_account or {}).get(account_id, {})
    return any(value > thresholds.get(group, 0) for group, value in own.items())


def _worst_window(limits, now, thresholds, unknown=None):
    """How close to the wall an account is: the least left on any live window
    that has a threshold. `unknown` stands in for an account with no reading,
    for callers that must rank one rather than fail on it."""
    if unknown is not None and not isinstance(limits, list):
        return unknown
    live = [
        limit.remaining
        for limit in limits
        if limit.group in thresholds and (limit.resets_at is None or limit.resets_at > now)
    ]
    if not live and unknown is not None:
        return unknown
    return min(live)


def expiring_sooner(limits, active, accounts, now, thresholds, per_account=None, return_margin=RETURN_MARGIN):
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
            worth_it = limit.remaining >= own.get("weekly", 0) + return_margin
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


def cap_expires(seen_at):
    """Spend caps are monthly. Without the reset time, which the adapter cannot
    see, a mark is held to the start of the next calendar month in UTC, and
    cleared sooner by a reading that shows extra usage available again."""
    year, month = (seen_at.year + 1, 1) if seen_at.month == 12 else (seen_at.year, seen_at.month + 1)
    return seen_at.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def capped(marks, now):
    """The account ids whose extra usage is spent, from `marks`: (id,
    organisation, reason, seen_at) per enrolled account, reason None when
    nothing is on record."""
    out, orgs = set(), set()
    for identifier, organisation, reason, seen_at in marks:
        if reason is None or seen_at is None or now >= cap_expires(seen_at):
            continue
        if reason in MEMBER_CAP_REASONS:
            out.add(identifier)
        elif reason in ORG_CAP_REASONS:
            out.add(identifier)
            if organisation:
                orgs.add(organisation)
    out.update(identifier for identifier, organisation, _, _ in marks if organisation in orgs)
    return frozenset(out)
