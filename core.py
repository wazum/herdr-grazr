from collections import namedtuple

Limit = namedtuple("Limit", "kind scope group remaining resets_at")
Account = namedtuple("Account", "id name snapshot")

# One cached usage reading: the millisecond stamp the agent wrote on it, and
# the headroom it reported per limit group.
Reading = namedtuple("Reading", "fetched_at remaining")

MS_PER_HOUR = 3_600_000


def burn_rate(previous, current):
    """Headroom points spent per hour, per group, between two readings.

    Measured against the stamps the readings carry rather than the wall clock:
    two looks a second apart usually find the same reading, and dividing a real
    drop by that second would invent a rate hundreds of times too high.
    """
    if previous is None or current is None:
        return {}
    hours = (current.fetched_at - previous.fetched_at) / MS_PER_HOUR
    if hours <= 0:
        return {}
    # Headroom that went up means that window reopened, and no rate spans a
    # reset. Only the group that rolled over is dropped: a 5-hour window
    # reopens while the weekly one it sits inside keeps falling.
    return {
        group: (previous.remaining[group] - left) / hours
        for group, left in current.remaining.items()
        if group in previous.remaining and left <= previous.remaining[group]
    }


def corrected(limits, rates, age_hours):
    """`limits` with each group's headroom reduced by what the pace implies has
    been spent since the reading was taken.

    An estimate, so it decides only whether the reading is worth a second
    opinion, never whether to rotate. A fresh reading corrects to itself, which
    is what keeps a healthy account off the network entirely.
    """
    if age_hours <= 0 or not rates:
        return limits
    return [
        entry._replace(
            remaining=max(0, entry.remaining - rates.get(entry.group, 0) * age_hours)
        )
        for entry in limits
    ]


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
    chosen = next_account(active, accounts, now, thresholds)
    if chosen:
        return "rotate", chosen
    # "Every account is spent" would be a claim about accounts that do not exist.
    return "exhausted" if any(_is_candidate(active, entry) for entry in accounts) else "unenrolled"


def next_account(active, accounts, now, thresholds):
    """The first account in preference order fit to take over, or None. Says
    nothing about whether the active one needs replacing."""
    for candidate in accounts:
        if not _is_candidate(active, candidate):
            continue
        if candidate.snapshot is None or _has_headroom(candidate.snapshot, now, thresholds):
            return candidate.id
    return None


def _is_candidate(active, entry):
    return entry.id != active and entry.snapshot != "locked"


def _has_headroom(limits, now, thresholds):
    return all(
        limit.remaining >= thresholds[limit.group]
        for limit in limits
        if limit.group in thresholds and (limit.resets_at is None or limit.resets_at > now)
    )
