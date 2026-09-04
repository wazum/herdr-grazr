import contextlib
import fcntl
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import claude
import grazr
import stores
from core import Account, Limit, decide

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 4, 17, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc)
SOONER = datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)
THRESHOLDS = {"session": 15, "weekly": 20}


def limit(group="session", remaining=50, kind=None, scope=None, resets_at=LATER):
    return Limit(kind=kind or group, scope=scope, group=group, remaining=remaining, resets_at=resets_at)


def account(identifier, snapshot=None):
    return Account(id=identifier, name=identifier, snapshot=snapshot)


def account_named(name, identifier, snapshot=None):
    return Account(id=identifier, name=name, snapshot=snapshot)


class FakeStore:
    def __init__(self, live=None, parked=None, isolated=None):
        self.live = live
        self.parked = dict(parked or {})
        self.isolated = dict(isolated or {})
        self.events = []
        self.discard_result = True

    def read_live(self):
        return self.live

    def write_live(self, blob):
        self.events.append(("write_live", blob))
        self.live = blob

    def read_parked(self, account_id):
        return self.parked.get(account_id)

    def write_parked(self, account_id, blob):
        self.events.append(("write_parked", account_id, blob))
        self.parked[account_id] = blob

    def read_isolated(self, config_dir):
        return self.isolated.get(config_dir)

    def discard_isolated(self, config_dir):
        self.isolated.pop(config_dir, None)
        return self.discard_result


def spent_account():
    """Exists but has nothing left, so "exhausted" is the honest answer."""
    return account("spent", snapshot=[limit(group="session", remaining=0, resets_at=LATER)])


class DecideTest(unittest.TestCase):
    def test_unknown_limits_stay(self):
        self.assertEqual(
            decide("unknown", active="work", accounts=[], now=NOW, thresholds=THRESHOLDS),
            "stay",
        )

    def test_every_limit_above_its_threshold_stays(self):
        limits = [limit(group="session", remaining=40), limit(group="weekly", remaining=60)]

        self.assertEqual(
            decide(limits, active="work", accounts=[], now=NOW, thresholds=THRESHOLDS),
            "stay",
        )

    def test_limit_below_threshold_with_a_spent_alternative_is_exhausted(self):
        limits = [limit(group="session", remaining=14)]

        self.assertEqual(
            decide(
                limits,
                active="work",
                accounts=[spent_account()],
                now=NOW,
                thresholds=THRESHOLDS,
            ),
            "exhausted",
        )

    def test_rotates_to_a_never_used_account(self):
        limits = [limit(group="session", remaining=14)]
        accounts = [account("work"), account("personal")]

        self.assertEqual(
            decide(limits, active="work", accounts=accounts, now=NOW, thresholds=THRESHOLDS),
            ("rotate", "personal"),
        )

    def test_the_first_fit_in_preference_order_wins(self):
        limits = [limit(group="session", remaining=1)]
        accounts = [account("work"), account("second"), account("third")]

        self.assertEqual(
            decide(limits, active="work", accounts=accounts, now=NOW, thresholds=THRESHOLDS),
            ("rotate", "second"),
        )

    def test_skips_an_account_still_spent_inside_its_window(self):
        limits = [limit(group="session", remaining=14)]
        spent = account("spent", snapshot=[limit(group="session", remaining=2, resets_at=LATER)])
        accounts = [account("work"), spent, account("fresh")]

        self.assertEqual(
            decide(limits, active="work", accounts=accounts, now=NOW, thresholds=THRESHOLDS),
            ("rotate", "fresh"),
        )

    def test_ignores_a_limit_group_with_no_threshold(self):
        limits = [limit(group="session", remaining=40), limit(group="lunar_cycle", remaining=1)]

        self.assertEqual(
            decide(limits, active="work", accounts=[], now=NOW, thresholds=THRESHOLDS),
            "stay",
        )

    def test_uses_an_account_whose_spent_window_has_reset(self):
        limits = [limit(group="session", remaining=14)]
        recovered = account(
            "recovered", snapshot=[limit(group="session", remaining=2, resets_at=EARLIER)]
        )
        accounts = [account("work"), recovered]

        self.assertEqual(
            decide(limits, active="work", accounts=accounts, now=NOW, thresholds=THRESHOLDS),
            ("rotate", "recovered"),
        )


class LockedAccountTest(unittest.TestCase):
    """Rotating away from a restricted account is how you circumvent a
    suspension, which the Usage Policy prohibits. grazr stops instead."""

    def test_a_locked_active_account_never_rotates_even_with_somewhere_to_go(self):
        accounts = [account("work"), account("personal")]

        self.assertEqual(
            decide("locked", active="work", accounts=accounts, now=NOW, thresholds=THRESHOLDS),
            "locked",
        )

    def test_an_account_parked_while_locked_is_not_a_target(self):
        limits = [limit(group="session", remaining=1)]
        accounts = [account("work"), account("suspended", snapshot="locked"), account("fresh")]

        self.assertEqual(
            decide(limits, active="work", accounts=accounts, now=NOW, thresholds=THRESHOLDS),
            ("rotate", "fresh"),
        )


class NothingToRotateToTest(unittest.TestCase):
    """Claiming every account is spent is false when none is enrolled."""

    def test_no_enrolled_accounts_is_not_the_same_as_all_spent(self):
        limits = [limit(group="session", remaining=1)]

        self.assertEqual(
            decide(limits, active="work", accounts=[], now=NOW, thresholds=THRESHOLDS),
            "unenrolled",
        )

    def test_only_the_active_account_enrolled_is_also_unenrolled(self):
        limits = [limit(group="session", remaining=1)]

        self.assertEqual(
            decide(
                limits,
                active="work",
                accounts=[account("work")],
                now=NOW,
                thresholds=THRESHOLDS,
            ),
            "unenrolled",
        )

    def test_spent_candidates_are_still_exhausted(self):
        limits = [limit(group="session", remaining=1)]
        spent = account("spent", snapshot=[limit(group="session", remaining=0, resets_at=LATER)])

        self.assertEqual(
            decide(
                limits,
                active="work",
                accounts=[account("work"), spent],
                now=NOW,
                thresholds=THRESHOLDS,
            ),
            "exhausted",
        )


class ThresholdBoundaryTest(unittest.TestCase):
    def test_fires_only_below_the_threshold(self):
        for remaining, expected in ((16, "stay"), (15, "stay"), (14, "exhausted")):
            with self.subTest(remaining=remaining):
                self.assertEqual(
                    decide(
                        [limit(group="session", remaining=remaining)],
                        active="work",
                        accounts=[spent_account()],
                        now=NOW,
                        thresholds=THRESHOLDS,
                    ),
                    expected,
                )

    def test_a_fully_used_window_is_exhausted(self):
        self.assertEqual(
            decide(
                [limit(group="session", remaining=0)],
                active="work",
                accounts=[spent_account()],
                now=NOW,
                thresholds=THRESHOLDS,
            ),
            "exhausted",
        )


class TwoWeeklyLimitsTest(unittest.TestCase):
    """The per-model weekly limit must not be collapsed into the overall one."""

    def test_the_lower_weekly_limit_decides_whatever_its_position(self):
        overall = limit(group="weekly", kind="weekly_all", remaining=60)
        per_model = limit(group="weekly", kind="weekly_scoped", scope="Fable", remaining=5)

        for order in ([overall, per_model], [per_model, overall]):
            with self.subTest(first=order[0].kind):
                self.assertEqual(
                    decide(
                        order,
                        active="work",
                        accounts=[spent_account()],
                        now=NOW,
                        thresholds=THRESHOLDS,
                    ),
                    "exhausted",
                )


class ServiceNameTest(unittest.TestCase):
    def test_the_live_login_has_no_directory_suffix(self):
        self.assertEqual(stores.service_name(), "Claude Code-credentials")

    def test_an_isolated_directory_appends_its_hash_last(self):
        directory = "/tmp/grazr-enrol-personal"
        expected = hashlib.sha256(directory.encode()).hexdigest()[:8]

        self.assertEqual(
            stores.service_name(directory), "Claude Code-credentials-" + expected
        )

    def test_a_decomposed_path_hashes_the_same_as_its_composed_form(self):
        """Claude normalises to NFC before hashing; a path picked up from the
        filesystem can arrive decomposed."""
        composed = "/tmp/grazr-über"
        decomposed = "/tmp/grazr-über"

        self.assertEqual(stores.service_name(decomposed), stores.service_name(composed))


class KeychainStoreTest(unittest.TestCase):
    """Where a secret physically lives is the store's business alone; the rest
    of grazr speaks read/write, live/parked."""

    def store(self, spawn):
        return stores.KeychainStore("Claude Code-credentials", "tester", spawn=spawn)

    def test_read_live_asks_for_the_live_item_and_returns_the_blob(self):
        recorded = {}

        def fake_subprocess(argv, capture_output, text, **kwargs):
            recorded["argv"] = argv
            return SimpleNamespace(returncode=0, stdout='{"token":"abc"}\n', stderr="")

        blob = self.store(fake_subprocess).read_live()

        self.assertEqual(blob, '{"token":"abc"}')
        self.assertEqual(
            recorded["argv"],
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-a",
                "tester",
                "-w",
            ],
        )

    def test_write_live_feeds_security_i_a_quoted_hex_line(self):
        recorded = {}

        def fake_subprocess(argv, input, capture_output, text, **kwargs):
            recorded["argv"] = argv
            recorded["line"] = input
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.store(fake_subprocess).write_live('{"token":"abc"}')

        self.assertEqual(recorded["argv"], ["security", "-i"])
        self.assertEqual(
            recorded["line"],
            "add-generic-password -U -s 'Claude Code-credentials' -a tester -X %s\n"
            % '{"token":"abc"}'.encode().hex(),
        )

    def test_a_parked_credential_lives_under_the_account_s_own_service(self):
        """The parked naming scheme is the store's business: another adapter
        may park under a filename instead, and no caller may care."""
        keychain = {}

        def fake_subprocess(argv, input=None, capture_output=True, text=True, **kwargs):
            if argv[:2] == ["security", "-i"]:
                service = shlex.split(input)[3]
                keychain[service] = shlex.split(input)[7]
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            service = argv[argv.index("-s") + 1]
            if service not in keychain:
                return SimpleNamespace(returncode=44, stdout="", stderr="not found")
            return SimpleNamespace(returncode=0, stdout=keychain[service] + "\n", stderr="")

        store = self.store(fake_subprocess)
        store.write_parked("uuid-work", "BLOB-WORK")

        self.assertEqual(list(keychain), ["grazr-uuid-work"])
        self.assertEqual(store.read_parked("uuid-work"), "BLOB-WORK")
        self.assertIsNone(store.read_parked("uuid-personal"))

    def test_a_keychain_that_refuses_is_an_error_not_an_absent_item(self):
        """A locked keychain, or one asked from a session with no UI, exits
        non-zero without saying "not found". Reading that as "never parked"
        sends the user off to enrol an account that is already enrolled."""

        def fake_subprocess(argv, capture_output, text, **kwargs):
            return SimpleNamespace(
                returncode=51, stdout="", stderr="security: interaction not allowed"
            )

        with self.assertRaises(RuntimeError) as raised:
            self.store(fake_subprocess).read_parked("uuid-work")

        self.assertIn("interaction not allowed", str(raised.exception))

    def test_read_isolated_asks_for_the_directory_s_own_item(self):
        directory = "/tmp/grazr-enrol-personal"
        suffix = hashlib.sha256(directory.encode()).hexdigest()[:8]
        recorded = {}

        def fake_subprocess(argv, capture_output, text, **kwargs):
            recorded["argv"] = argv
            return SimpleNamespace(returncode=0, stdout="ISOLATED\n", stderr="")

        blob = self.store(fake_subprocess).read_isolated(directory)

        self.assertEqual(blob, "ISOLATED")
        self.assertEqual(recorded["argv"][3], "Claude Code-credentials-" + suffix)

    def test_discard_isolated_reports_whether_the_item_is_gone(self):
        """Missing counts as gone; a keychain error does not, and never raises:
        the caller runs in a finally and must not mask the real failure."""
        for returncode, expected in ((0, True), (44, True), (1, False)):
            with self.subTest(returncode=returncode):
                recorded = {}

                def fake_subprocess(argv, capture_output, timeout, _rc=returncode):
                    recorded["argv"] = argv
                    return SimpleNamespace(returncode=_rc)

                removed = self.store(fake_subprocess).discard_isolated("/tmp/grazr-x")

                self.assertIs(removed, expected)
                suffix = hashlib.sha256(b"/tmp/grazr-x").hexdigest()[:8]
                self.assertEqual(
                    recorded["argv"],
                    [
                        "security",
                        "delete-generic-password",
                        "-s",
                        "Claude Code-credentials-" + suffix,
                        "-a",
                        "tester",
                    ],
                )

    def test_discard_isolated_swallows_a_keychain_that_will_not_answer(self):
        def exploding_spawn(argv, capture_output, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        self.assertIs(self.store(exploding_spawn).discard_isolated("/tmp/grazr-x"), False)


class FileStoreTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.live_path = os.path.join(self.directory, ".credentials.json")
        self.parked_dir = os.path.join(self.directory, "credentials")
        self.store = stores.FileStore(self.live_path, self.parked_dir)

    def test_read_live_returns_the_file_content(self):
        with open(self.live_path, "w") as handle:
            handle.write('{"claudeAiOauth":{}}')

        self.assertEqual(self.store.read_live(), '{"claudeAiOauth":{}}')

    def test_a_missing_live_file_is_none_rather_than_an_error(self):
        self.assertIsNone(self.store.read_live())

    def test_write_live_replaces_the_file_owner_readable_only(self):
        with open(self.live_path, "w") as handle:
            handle.write("OLD")

        self.store.write_live("NEW")

        self.assertEqual(self.store.read_live(), "NEW")
        self.assertEqual(os.stat(self.live_path).st_mode & 0o777, 0o600)

    def test_a_write_leaves_no_debris_beside_the_credentials(self):
        """Claude re-reads its credentials file on every request, so the swap
        has to be one atomic replace and nothing else in the directory."""
        before = set(os.listdir(self.directory))
        with open(self.live_path, "w"):
            before.add(".credentials.json")

        self.store.write_live("NEW")

        self.assertEqual(set(os.listdir(self.directory)), before)

    def test_a_parked_credential_survives_a_round_trip_owner_readable_only(self):
        self.store.write_parked("uuid-work", "BLOB-WORK")

        self.assertEqual(self.store.read_parked("uuid-work"), "BLOB-WORK")
        self.assertIsNone(self.store.read_parked("uuid-personal"))
        stored = os.path.join(self.parked_dir, "uuid-work.json")
        self.assertEqual(os.stat(stored).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.parked_dir).st_mode & 0o777, 0o700)

    def test_read_isolated_reads_the_directory_s_own_credentials_file(self):
        isolated = os.path.join(self.directory, "enrol-tmp")
        os.mkdir(isolated)
        with open(os.path.join(isolated, ".credentials.json"), "w") as handle:
            handle.write("ISOLATED")

        self.assertEqual(self.store.read_isolated(isolated), "ISOLATED")

    def test_discard_isolated_reports_whether_the_credential_is_gone(self):
        """Missing counts as gone; an unremovable file does not, and never
        raises: the caller runs in a finally."""
        isolated = os.path.join(self.directory, "enrol-tmp")
        os.mkdir(isolated)

        self.assertTrue(self.store.discard_isolated(isolated), "nothing to delete is gone")

        with open(os.path.join(isolated, ".credentials.json"), "w") as handle:
            handle.write("ISOLATED")
        self.assertTrue(self.store.discard_isolated(isolated))
        self.assertEqual(os.listdir(isolated), [])

        with mock.patch.object(os, "unlink", side_effect=OSError("read-only fs")):
            with open(os.path.join(isolated, ".credentials.json"), "w") as handle:
                handle.write("ISOLATED")
            self.assertFalse(self.store.discard_isolated(isolated))


class HasParkedCredentialTest(unittest.TestCase):
    """An account whose parked credential went missing cannot be rotated to,
    and the only way to find out is to ask the store."""

    def test_a_parked_account_answers_true_a_missing_one_false(self):
        store = FakeStore(parked={"uuid-work": "BLOB"})

        self.assertTrue(claude.has_parked_credential(store, "uuid-work"))
        self.assertFalse(claude.has_parked_credential(store, "uuid-personal"))


class CredentialInstallTest(unittest.TestCase):
    """`security -i` truncates past 4095 bytes and writes the truncated prefix
    over the item before it fails, so the refusal has to happen before the call."""

    def install(self, service, account, blob, attempted):
        def recording_spawn(argv, input, capture_output, text, **kwargs):
            attempted.append(input.rstrip("\n"))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        stores.KeychainStore(service, account, spawn=recording_spawn).write_live(blob)

    def test_refuses_an_oversized_blob_without_invoking_security(self):
        attempted = []

        with self.assertRaises(ValueError):
            self.install("svc", "acct", "x" * 2100, attempted)

        self.assertEqual(attempted, [])

    def test_a_blob_that_fits_is_installed_as_hex_never_as_plaintext(self):
        blob = '{"claudeAiOauth":{"accessToken":"secret-value"}}'
        attempted = []

        self.install("svc", "acct", blob, attempted)

        self.assertEqual(
            attempted,
            ["add-generic-password -U -s svc -a acct -X " + blob.encode().hex()],
        )
        self.assertNotIn("secret-value", attempted[0])

    def test_the_ceiling_is_the_measured_4095_bytes(self):
        """Measured against the real binary: a 4095 byte line installs, 4096
        truncates the item destructively. The newline is not counted."""
        self.assertEqual(stores.MAX_SECURITY_LINE, 4095)

    def test_a_line_of_exactly_the_ceiling_installs_and_one_byte_more_refuses(self):
        """4095 installs; 4096 truncates the item destructively, verified
        against the real binary. Each blob byte adds two hex characters, so the
        account name carries the parity that makes both values reachable."""
        service = "svcx"
        for account, expected in (("acct", 4095), ("acctx", 4096)):
            prefix = len("add-generic-password -U -s %s -a %s -X " % (service, account))
            blob = "x" * ((stores.MAX_SECURITY_LINE + 1 - prefix) // 2)
            line = "add-generic-password -U -s %s -a %s -X %s" % (
                service,
                account,
                blob.encode().hex(),
            )
            self.assertEqual(len(line), expected, "fixture must land on %d" % expected)

            attempted = []
            if expected <= stores.MAX_SECURITY_LINE:
                self.install(service, account, blob, attempted)
                self.assertEqual(len(attempted[0]), stores.MAX_SECURITY_LINE)
            else:
                with self.assertRaises(ValueError):
                    self.install(service, account, blob, attempted)
                self.assertEqual(attempted, [])

    def test_the_real_service_name_survives_the_interactive_parser(self):
        """`security -i` splits its line into tokens, and the live service name
        contains a space. Unquoted, `-s Claude Code-credentials` parses as
        `-s Claude` plus a stray argument."""
        attempted = []

        self.install(stores.SERVICE, "some user", "x", attempted)

        self.assertEqual(
            shlex.split(attempted[0])[:6],
            ["add-generic-password", "-U", "-s", "Claude Code-credentials", "-a", "some user"],
        )

    def test_the_budget_is_measured_in_bytes_not_characters(self):
        """`security` counts bytes. A multi-byte account name fits the character
        budget while overflowing the real one, and overflow destroys the item."""
        account = "ü" * 20
        prefix = len("add-generic-password -U -s svc -a %s -X " % shlex.quote(account))
        # One character under, so only a byte-aware gate refuses it.
        blob = "x" * ((stores.MAX_SECURITY_LINE - prefix) // 2 - 1)
        line = "add-generic-password -U -s svc -a %s -X %s" % (account, blob.encode().hex())
        self.assertLessEqual(len(line), stores.MAX_SECURITY_LINE, "fixture must fit in characters")
        self.assertGreater(len(line.encode()), stores.MAX_SECURITY_LINE, "but not in bytes")
        attempted = []

        with self.assertRaises(ValueError):
            self.install("svc", account, blob, attempted)

        self.assertEqual(attempted, [])


class SecurityRunnerTest(unittest.TestCase):
    def store(self, spawn):
        return stores.KeychainStore("svc", "acct", spawn=spawn)

    def test_the_secret_travels_on_stdin_and_never_on_argv(self):
        recorded = {}

        def fake_subprocess(argv, input, capture_output, text, **kwargs):
            recorded["argv"] = argv
            recorded["input"] = input
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.store(fake_subprocess).write_live("secret-blob")

        self.assertEqual(recorded["argv"], ["security", "-i"])
        self.assertIn("secret-blob".encode().hex(), recorded["input"])
        self.assertNotIn("secret-blob".encode().hex(), " ".join(recorded["argv"]))
        # Without the newline `security -i` exits 0 having written nothing.
        self.assertTrue(recorded["input"].endswith("\n"))

    def test_a_failing_security_call_raises_rather_than_passing_silently(self):
        def fake_subprocess(argv, input, capture_output, text, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="security: unknown command")

        with self.assertRaises(RuntimeError):
            self.store(fake_subprocess).write_live("blob")

    def test_every_keychain_call_is_bounded(self):
        """A blocked keychain would hang the hook, and holding past the stale
        age lets Claude break grazr's lock underneath it."""
        seen = {}

        def fake_subprocess(argv, **kwargs):
            seen[argv[1]] = kwargs.get("timeout")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self.store(fake_subprocess).write_live("blob")
        self.store(fake_subprocess).read_live()

        self.assertTrue(all(seen.values()), "every security call needs a timeout: %s" % seen)
        self.assertLessEqual(max(seen.values()) * 1000, claude.OAUTH_LOCK_STALE_MS)

    def test_a_hung_keychain_raises_rather_than_blocking(self):
        def fake_subprocess(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))

        with self.assertRaises(RuntimeError):
            self.store(fake_subprocess).write_live("blob")
        with self.assertRaises(RuntimeError):
            self.store(fake_subprocess).read_live()

    def test_hex_cut_short_by_the_length_cap_is_still_scrubbed(self):
        """A hex tail left too short to match puts part of a credential in the
        log."""
        # Positioned so the cap cuts the hex run below the match length.
        prefix = "security: unknown command "
        leaked = prefix + "?" * (192 - len(prefix)) + "8" * 40

        def fake_subprocess(argv, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr=leaked)

        with self.assertRaises(RuntimeError) as raised:
            self.store(fake_subprocess).write_live("blob")

        self.assertNotIn("88888888", str(raised.exception))

    def test_the_failure_message_carries_no_credential_hex(self):
        """On the truncation path security echoes the tail of the blob as its
        error. That message reaches the plugin log, so it must not be repeated."""
        leaked = "security: unknown command \"%s\"" % ("87" * 40)

        def fake_subprocess(argv, input, capture_output, text, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr=leaked)

        with self.assertRaises(RuntimeError) as raised:
            self.store(fake_subprocess).write_live("blob")

        self.assertNotIn("87" * 40, str(raised.exception))


def config_json(percent=20, fetched_at=None, account_uuid="uuid-work", limits=None, buckets=None):
    if limits is None:
        limits = [
            {
                "kind": "session",
                "group": "session",
                "percent": percent,
                "resets_at": "2026-09-04T17:00:00.000000+00:00",
                "scope": None,
            }
        ]
    utilization = {"limits": limits}
    utilization.update(buckets or {})
    return {
        "oauthAccount": {"accountUuid": "uuid-work"},
        "cachedUsageUtilization": {
            "accountUuid": account_uuid,
            "fetchedAtMs": int((fetched_at or NOW).timestamp() * 1000),
            "utilization": utilization,
        },
    }


class ReadLimitsTest(unittest.TestCase):
    def test_percent_used_becomes_remaining_headroom(self):
        limits = claude.read_limits(config_json(percent=80), now=NOW)

        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0].group, "session")
        self.assertEqual(limits[0].remaining, 20)
        self.assertEqual(limits[0].resets_at, LATER)

    def test_usage_describing_another_account_is_unknown(self):
        stale_identity = config_json(account_uuid="uuid-someone-else")

        self.assertEqual(claude.read_limits(stale_identity, now=NOW), "unknown")

    def test_usage_with_no_identity_at_all_is_unknown(self):
        """A half-written config has neither uuid, and None == None must not
        read as a match -- that guard is the whole defence against a mid-swap read."""
        anonymous = config_json()
        del anonymous["oauthAccount"]
        del anonymous["cachedUsageUtilization"]["accountUuid"]

        self.assertEqual(claude.read_limits(anonymous, now=NOW), "unknown")

    def test_usage_older_than_an_hour_is_unknown(self):
        long_ago = datetime(2026, 9, 4, 10, 30, tzinfo=timezone.utc)

        self.assertEqual(claude.read_limits(config_json(fetched_at=long_ago), now=NOW), "unknown")

    def test_a_z_suffixed_reset_time_parses(self):
        """Python 3.9's fromisoformat rejects a bare Z."""
        zulu = config_json(
            limits=[
                {
                    "kind": "session",
                    "group": "session",
                    "percent": 10,
                    "resets_at": "2026-09-04T17:00:00.000000Z",
                    "scope": None,
                }
            ]
        )

        self.assertEqual(claude.read_limits(zulu, now=NOW)[0].resets_at, LATER)

    def test_a_naive_now_is_refused_rather_than_silently_misjudged(self):
        """A naive datetime is read as local time, which can make hours-old usage
        look fresh. The freshness guard must never fail open."""
        with self.assertRaises(ValueError):
            claude.read_limits(config_json(), now=datetime(2026, 9, 4, 12, 0))

    def test_a_locked_bucket_reports_locked_rather_than_a_low_reading(self):
        """locked_reason is server-set and its values are not documented, so any
        non-null one is read as "do not touch this account"."""
        restricted = config_json(
            percent=10,
            buckets={"five_hour": {"utilization": 10, "resets_at": None, "locked_reason": "suspended"}},
        )

        self.assertEqual(claude.read_limits(restricted, now=NOW), "locked")

    def test_an_unlocked_bucket_reads_normally(self):
        healthy = config_json(
            buckets={
                "five_hour": {"utilization": 10, "resets_at": None, "locked_reason": None},
                "seven_day_opus": None,
            }
        )

        self.assertNotEqual(claude.read_limits(healthy, now=NOW), "locked")

    def test_unexpected_types_anywhere_in_the_usage_block_are_unknown(self):
        """This runs on every turn end of every pane, so an unfamiliar shape has
        to degrade rather than raise."""
        cases = {
            "fetchedAtMs is a string": {"fetchedAtMs": "soon"},
            "fetchedAtMs is null": {"fetchedAtMs": None},
            "utilization is a string": {"utilization": "nope"},
            "utilization is a list": {"utilization": []},
            "limits is a string": {"utilization": {"limits": "nope"}},
            "a limit entry is a string": {"utilization": {"limits": ["nope"]}},
        }
        for label, override in cases.items():
            with self.subTest(label=label):
                config = config_json()
                config["cachedUsageUtilization"].update(override)
                self.assertEqual(claude.read_limits(config, now=NOW), "unknown")

    def test_a_reset_time_without_a_timezone_is_still_comparable(self):
        """Claude sends +00:00 today, so this is defensive rather than observed."""
        naive = config_json(
            limits=[
                {
                    "kind": "session",
                    "group": "session",
                    "percent": 90,
                    "resets_at": "2026-09-04T17:00:00.000000",
                    "scope": None,
                }
            ]
        )

        limits = claude.read_limits(naive, now=NOW)

        self.assertEqual(limits[0].resets_at, LATER)
        self.assertEqual(
            decide(
                limits, active="work", accounts=[spent_account()], now=NOW, thresholds=THRESHOLDS
            ),
            "exhausted",
        )

    def test_an_unrecognised_limit_shape_is_unknown_not_a_crash(self):
        """The hook runs on every idle event, so a Claude release that renames a
        field must degrade to "stay", not raise on every pane."""
        renamed = config_json(limits=[{"kind": "session", "pct": 90, "scope": None}])

        self.assertEqual(claude.read_limits(renamed, now=NOW), "unknown")

    def test_missing_or_empty_limits_are_unknown(self):
        for config in (config_json(limits=[]), {}, {"cachedUsageUtilization": None}):
            with self.subTest(config=config):
                self.assertEqual(claude.read_limits(config, now=NOW), "unknown")


class CredentialReadTest(unittest.TestCase):
    def read(self, spawn):
        return stores.KeychainStore("svc", "acct", spawn=spawn).read_live()

    def test_it_asks_for_the_named_item_and_returns_the_blob(self):
        recorded = {}

        def fake_subprocess(argv, capture_output, text, **kwargs):
            recorded["argv"] = argv
            return SimpleNamespace(returncode=0, stdout='{"token":"abc"}\n', stderr="")

        blob = self.read(fake_subprocess)

        self.assertEqual(blob, '{"token":"abc"}')
        self.assertEqual(
            recorded["argv"],
            ["security", "find-generic-password", "-s", "svc", "-a", "acct", "-w"],
        )

    def test_a_hex_encoded_item_is_decoded(self):
        def fake_subprocess(argv, capture_output, text, **kwargs):
            return SimpleNamespace(returncode=0, stdout='{"t":1}'.encode().hex() + "\n", stderr="")

        self.assertEqual(self.read(fake_subprocess), '{"t":1}')

    def test_a_missing_item_is_none_rather_than_an_error(self):
        """Not-found is ordinary: an account is parked for the first time."""

        def fake_subprocess(argv, capture_output, text, **kwargs):
            return SimpleNamespace(returncode=44, stdout="", stderr="could not be found")

        self.assertIsNone(self.read(fake_subprocess))


class OAuthAccountMergeTest(unittest.TestCase):
    """Claude takes no lock on ~/.claude.json -- it watches the file instead --
    so the identity swap has to be a single atomic replace."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = os.path.join(self.directory, ".claude.json")
        with open(self.path, "w") as handle:
            json.dump(
                {
                    "oauthAccount": {"accountUuid": "uuid-work", "emailAddress": "a@b.c"},
                    "projects": {"/some/project": {"history": [1, 2, 3]}},
                    "cachedUsageUtilization": {"accountUuid": "uuid-work"},
                },
                handle,
            )

    def read(self):
        with open(self.path) as handle:
            return json.load(handle)

    def test_a_config_lock_held_by_claude_blocks_the_merge(self):
        """Claude's saveConfigWithLock merges under this lock, so writing
        without it lets Claude's merge revert us."""
        os.mkdir(self.path + ".lock")

        with self.assertRaises(RuntimeError):
            claude._write_oauth_account(self.path, {"accountUuid": "uuid-personal"})

        self.assertEqual(self.read()["oauthAccount"]["accountUuid"], "uuid-work")

    def test_the_config_lock_is_taken_and_released(self):
        claude._write_oauth_account(self.path, {"accountUuid": "uuid-personal"})

        self.assertFalse(os.path.exists(self.path + ".lock"), "a leaked lock blocks Claude")
        self.assertEqual(self.read()["oauthAccount"]["accountUuid"], "uuid-personal")

    def test_a_stale_config_lock_does_not_block_forever(self):
        lock = self.path + ".lock"
        os.mkdir(lock)
        long_ago = time.time() - (claude.CONFIG_LOCK_STALE_MS / 1000) - 10
        os.utime(lock, (long_ago, long_ago))

        claude._write_oauth_account(self.path, {"accountUuid": "uuid-personal"})

        self.assertEqual(self.read()["oauthAccount"]["accountUuid"], "uuid-personal")

    def test_it_swaps_the_identity_and_keeps_every_other_key(self):
        claude._write_oauth_account(self.path, {"accountUuid": "uuid-personal"})

        written = self.read()
        self.assertEqual(written["oauthAccount"], {"accountUuid": "uuid-personal"})
        self.assertEqual(written["projects"], {"/some/project": {"history": [1, 2, 3]}})
        self.assertIn("cachedUsageUtilization", written)

    def test_it_leaves_no_temporary_file_behind(self):
        claude._write_oauth_account(self.path, {"accountUuid": "uuid-personal"})

        self.assertEqual(os.listdir(self.directory), [".claude.json"])

    def test_the_config_stays_owner_only(self):
        os.chmod(self.path, 0o600)

        claude._write_oauth_account(self.path, {"accountUuid": "uuid-personal"})

        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)


class OAuthRefreshLockTest(unittest.TestCase):
    """Claude takes ~/.claude/.oauth_refresh.lock (proper-lockfile, mkdir-based,
    stale after 60s) around a token refresh. grazr takes it around a swap so a
    refresh cannot rewrite the live item between parking and installing."""

    def setUp(self):
        self.config_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.config_dir, True)
        self.lock_path = os.path.join(self.config_dir, ".oauth_refresh.lock")

    def test_it_is_held_for_the_body_and_released_after(self):
        with claude._oauth_refresh_lock(self.config_dir):
            self.assertTrue(os.path.isdir(self.lock_path))

        self.assertFalse(os.path.exists(self.lock_path))

    def test_a_lock_another_process_holds_is_refused(self):
        os.mkdir(self.lock_path)

        with self.assertRaises(RuntimeError):
            with claude._oauth_refresh_lock(self.config_dir):
                self.fail("must not enter the body while Claude holds the lock")

        self.assertTrue(os.path.isdir(self.lock_path), "someone else's lock must survive")

    def test_a_stale_lock_is_broken_rather_than_deadlocking_forever(self):
        os.mkdir(self.lock_path)
        long_ago = time.time() - (claude.OAUTH_LOCK_STALE_MS / 1000) - 10
        os.utime(self.lock_path, (long_ago, long_ago))

        with claude._oauth_refresh_lock(self.config_dir):
            self.assertTrue(os.path.isdir(self.lock_path))

        self.assertFalse(os.path.exists(self.lock_path))

    def make_stale(self):
        os.mkdir(self.lock_path)
        long_ago = time.time() - (claude.OAUTH_LOCK_STALE_MS / 1000) - 10
        os.utime(self.lock_path, (long_ago, long_ago))

    def test_a_lock_released_between_the_two_calls_is_taken_not_a_crash(self):
        """The holder let go in the moment between mkdir failing and the age
        check. What is left is a free lock to take, not an error to raise on
        the hook that happened to look just then."""
        os.mkdir(self.lock_path)
        real_getmtime = os.path.getmtime
        vanished = []

        def getmtime_already_gone(path, *arguments):
            if path == self.lock_path and not vanished:
                vanished.append(path)
                os.rmdir(path)
                raise FileNotFoundError(path)
            return real_getmtime(path, *arguments)

        with mock.patch("os.path.getmtime", getmtime_already_gone):
            with claude._oauth_refresh_lock(self.config_dir):
                self.assertTrue(os.path.isdir(self.lock_path))

        self.assertFalse(os.path.exists(self.lock_path))

    def test_losing_the_race_to_break_a_stale_lock_backs_off(self):
        """Another process broke the same lock first and now holds it. That
        must read as busy, not as a traceback."""
        self.make_stale()
        real_mkdir = os.mkdir

        def mkdir_taken(path, *arguments):
            if path == self.lock_path and os.path.exists(path):
                raise FileExistsError(path)
            if path == self.lock_path:
                real_mkdir(path)
                raise FileExistsError(path)
            return real_mkdir(path, *arguments)

        with mock.patch("os.mkdir", mkdir_taken):
            with self.assertRaises(RuntimeError):
                with claude._oauth_refresh_lock(self.config_dir):
                    self.fail("the loser must not also hold the lock")

    def test_a_lock_removed_by_someone_else_mid_break_is_not_a_crash(self):
        """The stale lock vanished between the check and the removal."""
        self.make_stale()
        real_rmdir = os.rmdir

        def rmdir_already_gone(path, *arguments):
            if path == self.lock_path:
                real_rmdir(path)
                raise FileNotFoundError(path)
            return real_rmdir(path, *arguments)

        with mock.patch("os.rmdir", rmdir_already_gone):
            with claude._oauth_refresh_lock(self.config_dir):
                self.assertTrue(os.path.isdir(self.lock_path))

    def test_a_slow_swap_cannot_outlive_its_own_lock(self):
        """A lock is judged by its mtime, so three slow keychain calls must not
        add up past the stale age, or Claude takes the lock mid-swap."""
        self.assertLess(
            3 * stores.SECURITY_TIMEOUT_SECONDS * 1000,
            claude.OAUTH_LOCK_STALE_MS,
            "a worst-case swap must stay inside the stale age",
        )

    def test_holding_the_lock_keeps_it_fresh(self):
        with claude._oauth_refresh_lock(self.config_dir) as renew:
            aged = time.time() - 30
            os.utime(self.lock_path, (aged, aged))

            renew()

            self.assertGreater(os.path.getmtime(self.lock_path), aged + 20)

    def test_the_lock_is_released_even_when_the_swap_fails(self):
        with self.assertRaises(ZeroDivisionError):
            with claude._oauth_refresh_lock(self.config_dir):
                raise ZeroDivisionError("a swap blew up")

        self.assertFalse(os.path.exists(self.lock_path), "a leaked lock blocks Claude itself")


class RotateTest(unittest.TestCase):
    """The only function that mutates credentials. Every refusal has to happen
    before the first write, and the lock has to cover park-then-install."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.accounts_dir = os.path.join(self.directory, "accounts")
        os.mkdir(self.accounts_dir)
        self.config_path = os.path.join(self.directory, ".claude.json")
        with open(self.config_path, "w") as handle:
            json.dump({"oauthAccount": {"accountUuid": "uuid-work"}, "projects": {}}, handle)

        self.write_account("uuid-work", {"name": "work", "oauthAccount": {"accountUuid": "uuid-work"}})
        self.write_account(
            "uuid-personal", {"name": "personal", "oauthAccount": {"accountUuid": "uuid-personal"}}
        )

        self.paths = claude.Paths(
            config_path=self.config_path,
            config_dir=self.directory,
            accounts_dir=self.accounts_dir,
        )
        self.store = FakeStore(live="LIVE-WORK", parked={"uuid-personal": "PARKED-PERSONAL"})

    def write_account(self, identifier, data):
        with open(os.path.join(self.accounts_dir, identifier + ".json"), "w") as handle:
            json.dump(data, handle)

    def read_account(self, identifier):
        with open(os.path.join(self.accounts_dir, identifier + ".json")) as handle:
            return json.load(handle)

    def run_rotate(self, snapshot="locked"):
        claude.rotate(self.paths, self.store, "uuid-work", "uuid-personal", snapshot)

    def test_it_parks_the_live_blob_before_installing_the_next(self):
        self.run_rotate()

        self.assertEqual(
            self.store.events,
            [("write_parked", "uuid-work", "LIVE-WORK"), ("write_live", "PARKED-PERSONAL")],
        )
        self.assertEqual(self.store.parked["uuid-work"], "LIVE-WORK")
        self.assertEqual(self.store.live, "PARKED-PERSONAL")

    def test_it_moves_the_identity_with_the_credential(self):
        self.run_rotate()

        with open(self.config_path) as handle:
            self.assertEqual(json.load(handle)["oauthAccount"], {"accountUuid": "uuid-personal"})

    def test_a_failed_snapshot_write_leaves_no_debris(self):
        """The accounts directory is enumerated to find accounts, so litter here
        accumulates in the one place that must stay readable."""
        before = set(os.listdir(self.accounts_dir))

        with mock.patch.object(claude.json, "dump", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                claude._save_account(self.paths, "uuid-work", {"name": "work"})

        self.assertEqual(set(os.listdir(self.accounts_dir)), before)

    def test_it_records_what_the_outgoing_account_had_left(self):
        spent = [limit(group="session", remaining=3)]

        self.run_rotate(snapshot=spent)

        stored = self.read_account("uuid-work")["snapshot"]
        self.assertEqual(claude.snapshot_from_json(stored), spent)

    def test_an_account_that_was_never_parked_aborts_before_any_write(self):
        del self.store.parked["uuid-personal"]

        with self.assertRaises(RuntimeError):
            self.run_rotate()

        self.assertEqual(self.store.events, [])
        self.assertEqual(self.store.live, "LIVE-WORK")

    def test_a_retry_after_a_crash_mid_swap_does_not_park_the_wrong_blob(self):
        """If the process dies between the keychain swap and the identity write,
        ~/.claude.json is still self-consistent, so the next hook decides to
        rotate again. The retry must not park the arriving blob over the
        outgoing account's parked credential and lose it for good."""
        with mock.patch.object(claude, "_merge_oauth_account", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_rotate()

        self.assertEqual(self.store.live, "PARKED-PERSONAL")
        self.run_rotate()

        self.assertEqual(
            self.store.parked["uuid-work"],
            "LIVE-WORK",
            "the outgoing account's own credential must survive the retry",
        )

    def test_rotating_away_from_an_unenrolled_account_still_completes(self):
        """You can be logged into an account grazr never enrolled, and the swap
        still works, so it must not be lost to a traceback afterwards."""
        os.unlink(os.path.join(self.accounts_dir, "uuid-work.json"))

        self.run_rotate()

        self.assertEqual(self.store.live, "PARKED-PERSONAL")
        with open(self.config_path) as handle:
            self.assertEqual(json.load(handle)["oauthAccount"]["accountUuid"], "uuid-personal")

    def test_a_held_config_lock_aborts_before_any_credential_write(self):
        """The identity write is the last step but its lock is contended like
        any other, so it has to be held before the first mutation, not after."""
        os.mkdir(self.config_path + ".lock")

        with self.assertRaises(RuntimeError):
            self.run_rotate()

        self.assertEqual(self.store.events, [])
        self.assertEqual(self.store.live, "LIVE-WORK")

    def test_a_lock_held_by_claude_aborts_before_any_write(self):
        os.mkdir(os.path.join(self.directory, ".oauth_refresh.lock"))

        with self.assertRaises(RuntimeError):
            self.run_rotate()

        self.assertEqual(self.store.events, [])
        self.assertEqual(self.store.live, "LIVE-WORK")


class SnapshotRoundTripTest(unittest.TestCase):
    """A parked snapshot goes through JSON, so datetimes must survive it."""

    def test_limits_survive_a_round_trip(self):
        limits = [
            limit(group="session", remaining=12, kind="session"),
            limit(group="weekly", remaining=40, kind="weekly_scoped", scope="Fable", resets_at=None),
        ]

        restored = claude.snapshot_from_json(json.loads(json.dumps(claude.snapshot_to_json(limits))))

        self.assertEqual(restored, limits)

    def test_an_account_file_edited_by_hand_does_not_break_the_hook(self):
        """The account store is enumerated on the rotate path, so one bad file
        would otherwise take out rotation for every account."""
        for garbage in ("SNAPSHOT", 42, {"kind": "session"}, [{"nope": 1}]):
            with self.subTest(garbage=garbage):
                self.assertIsNone(claude.snapshot_from_json(garbage))

    def test_locked_survives_a_round_trip(self):
        self.assertEqual(claude.snapshot_from_json(claude.snapshot_to_json("locked")), "locked")

    def test_never_parked_survives_a_round_trip(self):
        self.assertIsNone(claude.snapshot_from_json(claude.snapshot_to_json(None)))


class InspectTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.accounts_dir = os.path.join(self.directory, "accounts")
        os.mkdir(self.accounts_dir)
        self.config_path = os.path.join(self.directory, ".claude.json")
        with open(self.config_path, "w") as handle:
            json.dump(config_json(percent=80), handle)
        self.paths = claude.Paths(
            config_path=self.config_path,
            config_dir=self.directory,
            accounts_dir=self.accounts_dir,
        )

    def write_account(self, identifier, data):
        with open(os.path.join(self.accounts_dir, identifier + ".json"), "w") as handle:
            json.dump(data, handle)

    def test_it_reports_the_active_account_and_its_headroom(self):
        active, limits = claude.inspect(self.paths, now=NOW)

        self.assertEqual(active, "uuid-work")
        self.assertEqual(limits[0].remaining, 20)

    def test_an_unreadable_config_is_unknown_not_a_traceback(self):
        """Not logged in, or caught mid-write. This runs on every status change
        of every pane, so it must degrade rather than raise."""
        with open(self.config_path, "w") as handle:
            handle.write('{"oauthAccount": {"accountUu')

        self.assertEqual(claude.inspect(self.paths, now=NOW), (None, "unknown"))

        os.unlink(self.config_path)
        self.assertEqual(claude.inspect(self.paths, now=NOW), (None, "unknown"))

    def test_accounts_come_back_in_the_configured_preference_order(self):
        self.write_account("uuid-work", {"name": "work"})
        self.write_account("uuid-personal", {"name": "personal", "snapshot": "locked"})

        accounts = claude.load_accounts(self.paths, ["personal", "work"])

        self.assertEqual([account.name for account in accounts], ["personal", "work"])
        self.assertEqual(accounts[0].id, "uuid-personal")
        self.assertEqual(accounts[0].snapshot, "locked")

    def test_one_corrupt_account_file_does_not_hide_the_others(self):
        """The store is read whole on the rotate path, so a single bad file
        would otherwise stop rotation for every account."""
        self.write_account("uuid-work", {"name": "work"})
        with open(os.path.join(self.accounts_dir, "uuid-broken.json"), "w") as handle:
            handle.write("{not json at all")

        self.assertEqual([a.name for a in claude.load_accounts(self.paths, [])], ["work"])

    def test_it_ignores_anything_that_is_not_an_account_file(self):
        """The directory also holds grazr's own atomic-write leftovers."""
        self.write_account("uuid-work", {"name": "work"})
        for debris in (".grazr-tmp123", "notes.txt", ".DS_Store"):
            with open(os.path.join(self.accounts_dir, debris), "w") as handle:
                handle.write("not an account")

        self.assertEqual([a.name for a in claude.load_accounts(self.paths, [])], ["work"])

    def test_an_empty_preference_list_falls_back_to_every_enrolled_account(self):
        self.write_account("uuid-work", {"name": "work"})
        self.write_account("uuid-personal", {"name": "personal"})

        self.assertEqual(
            sorted(account.name for account in claude.load_accounts(self.paths, [])),
            ["personal", "work"],
        )


class EnrolTest(unittest.TestCase):
    # Real uuids, because enrolment validates the shape Claude actually sends.
    WORK = "11111111-1111-4111-8111-111111111111"
    PERSONAL = "22222222-2222-4222-8222-222222222222"
    OTHER = "33333333-3333-4333-8333-333333333333"

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.accounts_dir = os.path.join(self.directory, "accounts")
        os.mkdir(self.accounts_dir)
        self.config_path = os.path.join(self.directory, ".claude.json")
        with open(self.config_path, "w") as handle:
            json.dump(
                {"oauthAccount": {"accountUuid": self.WORK, "emailAddress": "a@b.c"}}, handle
            )
        self.paths = claude.Paths(
            config_path=self.config_path,
            config_dir=self.directory,
            accounts_dir=self.accounts_dir,
        )
        self.store = FakeStore(live="LIVE-WORK")

    def run_enrol(self, name="work", source=None):
        return claude.enrol(self.paths, self.store, name, source)

    def test_it_parks_the_live_credential_under_the_account_uuid(self):
        identifier = self.run_enrol()

        self.assertEqual(identifier, self.WORK)
        self.assertEqual(self.store.events, [("write_parked", self.WORK, "LIVE-WORK")])

    def test_it_records_the_identity_needed_to_rotate_back(self):
        self.run_enrol(name="work")

        with open(os.path.join(self.accounts_dir, self.WORK + ".json")) as handle:
            stored = json.load(handle)
        self.assertEqual(stored["name"], "work")
        self.assertEqual(stored["oauthAccount"]["accountUuid"], self.WORK)
        self.assertIsNone(stored["snapshot"])

    def test_reusing_a_name_is_refused_rather_than_hiding_an_account(self):
        """Accounts are looked up by display name, so a duplicate would make one
        of them permanently unreachable."""
        with open(os.path.join(self.accounts_dir, self.OTHER + ".json"), "w") as handle:
            json.dump({"name": "work", "oauthAccount": {"accountUuid": self.OTHER}}, handle)

        with self.assertRaises(RuntimeError):
            self.run_enrol(name="work")

        self.assertEqual(self.store.events, [], "nothing parked for a refused enrolment")

    def test_an_uppercase_uuid_is_accepted(self):
        """Some providers hand back uppercase. It is the same account."""
        with open(self.config_path, "w") as handle:
            json.dump({"oauthAccount": {"accountUuid": self.WORK.upper()}}, handle)

        self.assertEqual(self.run_enrol(), self.WORK)

    def test_an_account_id_that_is_not_a_uuid_is_refused(self):
        """The id comes from the server and becomes a filename, so it must not
        be able to point anywhere but the account store."""
        for bogus in ("../../evil", "a/b", "..", "", "  "):
            with self.subTest(bogus=bogus):
                with open(self.config_path, "w") as handle:
                    json.dump({"oauthAccount": {"accountUuid": bogus}}, handle)
                with self.assertRaises(RuntimeError):
                    self.run_enrol()
        self.assertEqual(self.store.events, [])

    def test_a_login_with_no_identity_yet_is_refused(self):
        """Without an accountUuid the credential would be parked under
        `grazr-None`, where the next enrolment would overwrite it."""
        with open(self.config_path, "w") as handle:
            json.dump({"oauthAccount": {}}, handle)

        with self.assertRaises(RuntimeError):
            self.run_enrol()

        self.assertEqual(self.store.events, [])

    def test_it_refuses_when_there_is_no_login_to_save(self):
        self.store.live = None

        with self.assertRaises(RuntimeError):
            self.run_enrol()

        self.assertEqual(os.listdir(self.accounts_dir), [], "no half-written account file")

    def test_discarding_a_throwaway_login_removes_its_credential_and_its_directory(self):
        """The isolated login is a real credential. Leaving it in the store
        after enrolment is a copy nobody is tracking."""
        isolated = os.path.join(self.directory, "enrol-tmp")
        os.mkdir(isolated)
        self.store.isolated[isolated] = "LIVE-THROWAWAY"

        removed = claude.discard_isolated_login(self.store, isolated)

        self.assertTrue(removed)
        self.assertEqual(self.store.isolated, {})
        self.assertFalse(os.path.exists(isolated))

    def test_the_directory_goes_even_when_the_store_cannot_delete(self):
        """Cleanup runs from a finally on paths where things are already going
        wrong, so it must not abandon half the job."""
        isolated = os.path.join(self.directory, "enrol-tmp")
        os.mkdir(isolated)
        self.store.discard_result = False

        removed = claude.discard_isolated_login(self.store, isolated)

        self.assertFalse(os.path.exists(isolated))
        self.assertFalse(removed, "a stranded credential must be reported, not hidden")

    def test_an_isolated_directory_is_read_instead_of_the_live_login(self):
        isolated = os.path.join(self.directory, "enrol-tmp")
        os.mkdir(isolated)
        with open(os.path.join(isolated, ".claude.json"), "w") as handle:
            json.dump({"oauthAccount": {"accountUuid": self.PERSONAL}}, handle)
        self.store.isolated[isolated] = "LIVE-PERSONAL"

        identifier = self.run_enrol(name="personal", source=isolated)

        self.assertEqual(identifier, self.PERSONAL)
        self.assertEqual(self.store.events, [("write_parked", self.PERSONAL, "LIVE-PERSONAL")])


class InteractiveExitTest(unittest.TestCase):
    """A popup pane owns the terminal, so every way out has to be quiet."""

    def read(self, text):
        stream = io.StringIO(text)
        stream.isatty = lambda: False
        return grazr.read_key(stream)

    def test_a_single_letter_is_taken_as_the_choice(self):
        self.assertEqual(self.read("S\n"), "s")

    def test_nothing_typed_reads_as_close(self):
        for text in ("\n", ""):
            with self.subTest(text=repr(text)):
                self.assertIn(self.read(text), grazr.CLOSE_KEYS)

    def test_escape_reads_as_close(self):
        self.assertIn(grazr.ESCAPE, grazr.CLOSE_KEYS)
        self.assertIn(self.read(grazr.ESCAPE), grazr.CLOSE_KEYS)

    def test_ctrl_c_and_ctrl_d_leave_no_traceback(self):
        for interrupt in (KeyboardInterrupt, EOFError):
            with self.subTest(interrupt=interrupt.__name__):
                with mock.patch.object(grazr, "enrol", side_effect=interrupt), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    code = grazr.main(["grazr.py", "enrol"])

                self.assertEqual(code, 130)
                self.assertIn("cancelled", out.getvalue())

    def test_a_refusal_is_one_line_rather_than_a_traceback(self):
        """A locked keychain or a busy lock is an expected refusal, and the
        message names the cause. A traceback in a pane names it too, but buries
        it under a stack nobody can act on."""
        refusal = RuntimeError("the keychain refused to read the credential: locked")
        with mock.patch.object(grazr, "status", side_effect=refusal), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = grazr.main(["grazr.py", "status"])

        self.assertEqual(code, 1)
        self.assertIn("keychain refused", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())

    def test_the_menu_says_how_to_leave(self):
        with mock.patch.object(grazr, "read_key", lambda: "q"), \
                mock.patch.object(grazr, "_paths", side_effect=AssertionError):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = grazr.enrol()

        self.assertEqual(code, 0, "closing on purpose is not a failure")
        self.assertIn("Esc", out.getvalue())


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = os.path.join(self.directory, "config.env")

    def write(self, text):
        with open(self.path, "w") as handle:
            handle.write(text)

    def test_it_reads_quotes_and_ignores_comments(self):
        self.write(
            "# how much to leave\n"
            "REMAINING_SESSION=15   # the 5-hour window\n"
            "REMAINING_WEEKLY=20\n"
            'ACCOUNTS="work personal"\n'
            "\n"
            "ENABLED=1\n"
            "DRY_RUN=0\n"
        )

        config = grazr.load_config(self.path)

        self.assertEqual(config.thresholds, {"session": 15, "weekly": 20})
        self.assertEqual(config.accounts, ["work", "personal"])
        self.assertTrue(config.enabled)
        self.assertFalse(config.dry_run)

    def test_a_missing_file_is_seeded_and_reads_back(self):
        config = grazr.load_config(self.path)

        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(config, grazr.load_config(self.path))

    def test_an_unquoted_list_is_rejected_rather_than_half_read(self):
        """It used to keep only the first name, halving the rotation pool."""
        self.write("ACCOUNTS=work personal\n")

        with self.assertRaises(ValueError) as raised:
            grazr.load_config(self.path)

        self.assertIn("ACCOUNTS", str(raised.exception))

    def test_spaces_around_the_equals_sign_are_accepted(self):
        self.write("REMAINING_SESSION = 15\nACCOUNTS = \"work personal\"\n")

        config = grazr.load_config(self.path)

        self.assertEqual(config.thresholds["session"], 15)
        self.assertEqual(config.accounts, ["work", "personal"])

    def test_the_same_key_twice_is_rejected(self):
        self.write("REMAINING_SESSION=15\nREMAINING_SESSION=20\nACCOUNTS=work\n")

        with self.assertRaises(ValueError):
            grazr.load_config(self.path)

    def test_a_threshold_outside_nought_to_a_hundred_is_rejected_by_name(self):
        self.write("REMAINING_SESSION=140\nREMAINING_WEEKLY=20\nACCOUNTS=work\n")

        with self.assertRaises(ValueError) as raised:
            grazr.load_config(self.path)

        self.assertIn("REMAINING_SESSION", str(raised.exception))

    def test_a_repeated_account_is_rejected(self):
        self.write('REMAINING_SESSION=15\nREMAINING_WEEKLY=20\nACCOUNTS="work work"\n')

        with self.assertRaises(ValueError):
            grazr.load_config(self.path)

    def test_a_flag_that_is_neither_nought_nor_one_is_rejected(self):
        for value in ("true", "yes", "2", ""):
            with self.subTest(value=value):
                self.write("ACCOUNTS=work\nENABLED=%s\n" % value)
                with self.assertRaises(ValueError):
                    grazr.load_config(self.path)

    def test_an_unknown_key_is_rejected_rather_than_silently_ignored(self):
        self.write("REMAINING_SESSION=15\nREMAINING_WEEKLY=20\nACCOUNTS=work\nREMAINING_MONTHLY=5\n")

        with self.assertRaises(ValueError) as raised:
            grazr.load_config(self.path)

        self.assertIn("REMAINING_MONTHLY", str(raised.exception))


class TurnBoundaryTest(unittest.TestCase):
    """`done` is the same idle state in a tab nobody has looked at, so both are
    turn ends. Anything else, any other agent, or a malformed envelope: exit."""

    def test_it_acts_on_a_finished_claude_turn(self):
        for status in ("idle", "done"):
            with self.subTest(status=status):
                event = json.dumps({"data": {"agent": "claude", "agent_status": status}})
                self.assertTrue(grazr.is_turn_end(event))

    def test_it_ignores_anything_that_is_not_a_finished_claude_turn(self):
        ignored = [
            {"data": {"agent": "claude", "agent_status": "working"}},
            {"data": {"agent": "claude", "agent_status": "blocked"}},
            {"data": {"agent": "claude", "agent_status": "unknown"}},
            {"data": {"agent": "codex", "agent_status": "idle"}},
            {"data": {"agent_status": "idle"}},
            {"data": {}},
            {},
        ]
        for event in ignored:
            with self.subTest(event=event):
                self.assertFalse(grazr.is_turn_end(json.dumps(event)))

    def test_a_malformed_envelope_is_ignored_rather_than_raising(self):
        for event in ("", "not json", "null", "[]"):
            with self.subTest(event=event):
                self.assertFalse(grazr.is_turn_end(event))


class PathResolutionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def paths_on(self, mac, environment):
        with mock.patch.object(stores, "IS_MAC", mac), mock.patch.dict(
            os.environ, environment
        ):
            if "CLAUDE_CONFIG_DIR" not in environment:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            return grazr._paths()

    def test_an_isolated_config_dir_gets_its_own_keychain_item(self):
        """Writing the identity to an isolated config while swapping the
        default keychain item would break both logins at once."""
        isolated = os.path.join(self.directory, "isolated")
        _, store, _ = self.paths_on(
            True, {"CLAUDE_CONFIG_DIR": isolated, "HERDR_PLUGIN_STATE_DIR": self.directory}
        )

        self.assertEqual(store.service, stores.service_name(isolated))
        self.assertNotEqual(store.service, stores.SERVICE)

    def test_the_ordinary_login_uses_the_unnamespaced_keychain_item(self):
        _, store, _ = self.paths_on(True, {"HERDR_PLUGIN_STATE_DIR": self.directory})

        self.assertEqual(store.service, stores.SERVICE)

    def test_off_mac_the_store_is_claude_s_own_credentials_file(self):
        _, store, _ = self.paths_on(False, {"HERDR_PLUGIN_STATE_DIR": self.directory})

        self.assertEqual(
            store.live_path, os.path.expanduser("~/.claude/.credentials.json")
        )
        self.assertEqual(store.parked_dir, os.path.join(self.directory, "credentials"))

    def test_off_mac_an_isolated_config_dir_keeps_its_own_credentials_file(self):
        isolated = os.path.join(self.directory, "isolated")
        _, store, _ = self.paths_on(
            False, {"CLAUDE_CONFIG_DIR": isolated, "HERDR_PLUGIN_STATE_DIR": self.directory}
        )

        self.assertEqual(store.live_path, os.path.join(isolated, ".credentials.json"))

    def test_an_isolated_config_dir_keeps_its_own_claude_json(self):
        """`CLAUDE_CONFIG_DIR=x claude` writes x/.claude.json, not ~/.claude.json."""
        isolated = os.path.join(self.directory, "isolated")
        with mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": isolated, "HERDR_PLUGIN_STATE_DIR": self.directory}
        ):
            paths, _, _ = grazr._paths()

        self.assertEqual(paths.config_path, os.path.join(isolated, ".claude.json"))
        self.assertEqual(paths.config_dir, isolated)

    def test_the_ordinary_login_uses_the_home_config(self):
        environment = {"HERDR_PLUGIN_STATE_DIR": self.directory}
        with mock.patch.dict(os.environ, environment, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            paths, _, _ = grazr._paths()

        self.assertEqual(paths.config_path, os.path.expanduser("~/.claude.json"))


class NotifyTest(unittest.TestCase):
    """`herdr notification show` exits 0 even when it shows nothing: toasts are
    off by default, a toast already on screen returns busy, and there is a rate
    limit. grazr must not mistake any of that for having told the user."""

    def notify(self, payload, herdr="/usr/bin/herdr"):
        recorded = {}

        def fake_subprocess(argv, capture_output, text=False, **kwargs):
            recorded["argv"] = argv
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

        with mock.patch.dict(os.environ, {"HERDR_BIN_PATH": herdr}):
            shown = grazr.notify("t", "b", spawn=fake_subprocess)
        return shown, recorded.get("argv")

    def test_a_shown_toast_reports_success(self):
        shown, argv = self.notify({"result": {"shown": True, "reason": "shown"}})

        self.assertTrue(shown)
        self.assertEqual(argv[:3], ["/usr/bin/herdr", "notification", "show"])
        # A swap changes which subscription you spend and drops Remote Control.
        self.assertEqual(argv[argv.index("--sound") + 1], "request")

    def test_a_suppressed_toast_reports_failure(self):
        for reason in ("disabled", "busy", "rate_limited", "no_foreground_client"):
            with self.subTest(reason=reason):
                shown, _ = self.notify({"result": {"shown": False, "reason": reason}})
                self.assertFalse(shown)

    def test_a_hanging_herdr_cli_does_not_hang_the_hook(self):
        def fake_subprocess(argv, **kwargs):
            self.assertTrue(kwargs.get("timeout"), "notify needs a timeout")
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        with mock.patch.dict(os.environ, {"HERDR_BIN_PATH": "/usr/bin/herdr"}):
            self.assertFalse(grazr.notify("t", "b", spawn=fake_subprocess))

    def test_a_broken_herdr_binary_is_not_a_claim_of_success(self):
        def fake_subprocess(argv, **kwargs):
            raise OSError("no such file")

        with mock.patch.dict(os.environ, {"HERDR_BIN_PATH": "/usr/bin/herdr"}):
            self.assertFalse(grazr.notify("t", "b", spawn=fake_subprocess))

    def test_no_herdr_binary_is_not_a_claim_of_success(self):
        shown, argv = self.notify({"result": {"shown": True}}, herdr="")

        self.assertFalse(shown)
        self.assertIsNone(argv)


class ActOnDecisionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.state_dir = os.path.join(self.directory, "state")
        os.mkdir(self.state_dir)
        self.paths = claude.Paths(
            config_path=os.path.join(self.directory, ".claude.json"),
            config_dir=self.directory,
            accounts_dir=self.directory,
        )
        self.store = FakeStore()
        self.rotations = []
        self.notices = []

    def record_notice(self, title, body):
        """Stands in for grazr.notify, which reports whether Herdr showed it."""
        self.notices.append(title)
        return True

    def act(self, decision, dry_run=False, limits=None, accounts=None, now=None):
        with mock.patch.object(
            claude, "rotate", lambda *arguments: self.rotations.append(arguments)
        ), mock.patch.object(grazr, "notify", self.record_notice):
            return grazr.act_on(
                decision,
                self.paths,
                self.store,
                self.state_dir,
                "uuid-work",
                limits or [],
                dry_run,
                accounts or [],
                now or NOW,
            )

    def test_staying_is_silent(self):
        self.act("stay")

        self.assertEqual((self.rotations, self.notices), ([], []))

    def test_rotating_swaps_and_says_so(self):
        self.act(("rotate", "uuid-personal"))

        self.assertEqual(len(self.rotations), 1)
        self.assertEqual(self.rotations[0][2:4], ("uuid-work", "uuid-personal"))
        self.assertEqual(len(self.notices), 1)

    def test_it_reports_the_names_you_chose_not_uuids(self):
        """You named these accounts at enrolment. A uuid fragment tells you
        nothing about which subscription you are now spending."""
        accounts = [account_named("work", "uuid-work"), account_named("personal", "uuid-personal")]

        line = self.act(("rotate", "uuid-personal"), accounts=accounts)

        self.assertIn("work", line)
        self.assertIn("personal", line)
        self.assertNotIn("uuid-", line)
        self.assertNotIn("uuid-", self.notices[0])

    def test_a_dry_run_decides_but_never_swaps(self):
        self.act(("rotate", "uuid-personal"), dry_run=True)

        self.assertEqual(self.rotations, [])

    def test_having_nothing_enrolled_says_so_and_points_at_the_fix(self):
        line = self.act("unenrolled")

        self.assertIn("enrol", line)
        self.assertNotIn("spent", line)
        self.assertEqual(self.rotations, [])

    def test_a_locked_account_is_reported_and_never_rotated_away_from(self):
        self.act("locked")

        self.assertEqual(self.rotations, [])
        self.assertEqual(len(self.notices), 1)

    def test_exhaustion_is_announced_once_not_on_every_idle_pane(self):
        limits = [limit(group="session", remaining=0)]
        for _ in range(4):
            self.act("exhausted", limits=limits)

        self.assertEqual(len(self.notices), 1)

    def test_a_repeated_situation_stays_out_of_the_log_too(self):
        """Reached on every idle event of every pane; one line, not hundreds."""
        limits = [limit(group="session", remaining=0)]

        first = self.act("exhausted", limits=limits)
        again = self.act("exhausted", limits=limits)

        self.assertTrue(first)
        self.assertIsNone(again)

    def test_the_log_line_does_not_depend_on_the_toast_appearing(self):
        """Toasts are off by default in Herdr, so the plugin log is the only
        record most users will have."""
        limits = [limit(group="session", remaining=0)]
        with mock.patch.object(grazr, "notify", lambda title, body: False):
            line = grazr.act_on(
                "exhausted", self.paths, self.store, self.state_dir, "uuid-work", limits, False
            )

        self.assertIn("earliest reset", line or "")

    def test_a_toast_that_never_appeared_is_retried_next_time(self):
        """A toast suppressed as busy or disabled was never seen. Marking it
        announced would mean the user is never told at all."""
        limits = [limit(group="session", remaining=0)]
        with mock.patch.object(grazr, "notify", lambda title, body: False):
            grazr.act_on(
                "exhausted", self.paths, self.store, self.state_dir, "uuid-work", limits, False
            )

        self.act("exhausted", limits=limits)

        self.assertEqual(len(self.notices), 1, "must announce again after a suppressed toast")

    def test_a_rotation_clears_the_situation_it_resolved(self):
        """The marker is the dedupe key as well as the status line. Leaving
        "unenrolled" on it after a rotation means the day you really are out of
        accounts again, nobody is ever told."""
        first = self.act("unenrolled")
        self.act(("rotate", "uuid-personal"))
        again = self.act("unenrolled")

        self.assertTrue(first)
        self.assertTrue(again, "the situation is new again after a rotation")
        self.assertEqual(len(self.notices), 3)

    def test_the_reset_it_names_covers_every_account_not_just_the_active_one(self):
        """When nothing has headroom, the useful answer is when the first
        account becomes usable, which may not be the one you are on."""
        active = [limit(group="session", remaining=0, resets_at=LATER)]
        others = [
            account_named("spent", "uuid-spent", snapshot=[
                limit(group="session", remaining=0, resets_at=SOONER)
            ])
        ]

        line = self.act("exhausted", limits=active, accounts=others)

        self.assertIn(SOONER.isoformat(), line)

    def test_a_reset_already_in_the_past_is_not_announced(self):
        """Otherwise it says the wait ended hours ago."""
        limits = [
            limit(group="session", remaining=0, resets_at=EARLIER),
            limit(group="weekly", remaining=0, resets_at=LATER),
        ]

        line = self.act("exhausted", limits=limits, now=NOW)

        self.assertIn(LATER.isoformat(), line)
        self.assertNotIn(EARLIER.isoformat(), line)

    def test_the_soonest_reset_is_the_earliest_not_the_latest(self):
        limits = [
            limit(group="weekly", remaining=0, resets_at=LATER),
            limit(group="session", remaining=0, resets_at=SOONER),
        ]

        line = self.act("exhausted", limits=limits)

        self.assertIn(SOONER.isoformat(), line)

    def test_exhaustion_without_usable_limits_still_reports(self):
        for limits in ("unknown", "locked", [], [limit(resets_at=None)]):
            with self.subTest(limits=limits):
                self.state_dir = tempfile.mkdtemp(dir=self.directory)
                self.assertIn("earliest reset", self.act("exhausted", limits=limits) or "")

    def test_a_new_reset_time_earns_a_fresh_announcement(self):
        self.act("exhausted", limits=[limit(group="session", remaining=0, resets_at=LATER)])
        self.act("exhausted", limits=[limit(group="session", remaining=0, resets_at=EARLIER)])

        self.assertEqual(len(self.notices), 2)


class HookTest(unittest.TestCase):
    """The hook is what Herdr actually runs, and it carries the concurrency
    argument: several panes go idle together and must produce one rotation."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.state_dir = os.path.join(self.directory, "state")
        self.claude_dir = os.path.join(self.directory, "claude")
        for made in (self.state_dir, self.claude_dir, os.path.join(self.state_dir, "accounts")):
            os.makedirs(made)
        # hook() reads the real clock. A fixed timestamp here goes stale and
        # quietly stops testing rotation at all.
        self.write_usage(percent=99)
        for identifier, name in (("uuid-work", "work"), ("uuid-personal", "personal")):
            with open(os.path.join(self.state_dir, "accounts", identifier + ".json"), "w") as f:
                json.dump({"name": name, "oauthAccount": {"accountUuid": identifier}}, f)
        self.write_config('ACCOUNTS="work personal"\n')
        self.rotations = []

    def write_config(self, text):
        with open(os.path.join(self.state_dir, "config.env"), "w") as handle:
            handle.write(text)

    def write_usage(self, percent):
        wall_now = datetime.now(timezone.utc)
        with open(os.path.join(self.claude_dir, ".claude.json"), "w") as handle:
            json.dump(
                config_json(
                    fetched_at=wall_now,
                    limits=[
                        {
                            "kind": "session",
                            "group": "session",
                            "percent": percent,
                            "resets_at": (wall_now + timedelta(hours=5)).isoformat(),
                            "scope": None,
                        }
                    ],
                ),
                handle,
            )

    def run_hook(self, status="idle", now=None):
        environment = {
            "HERDR_PLUGIN_STATE_DIR": self.state_dir,
            "HERDR_PLUGIN_CONFIG_DIR": self.state_dir,
            "CLAUDE_CONFIG_DIR": self.claude_dir,
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(
                {"data": {"agent": "claude", "agent_status": status}}
            ),
        }
        with mock.patch.dict(os.environ, environment), mock.patch.object(
            claude, "rotate", lambda *arguments: self.rotations.append(arguments)
        ), mock.patch.object(grazr, "notify", lambda title, body: True), contextlib.redirect_stdout(
            io.StringIO()
        ):
            return grazr.hook()

    def test_a_spent_account_rotates(self):
        self.assertEqual(self.run_hook(), 0)

        self.assertEqual(len(self.rotations), 1)
        self.assertEqual(self.rotations[0][2:4], ("uuid-work", "uuid-personal"))

    def test_a_healthy_account_never_reads_the_account_files(self):
        """This runs on every status change of every pane. Reading one JSON per
        enrolled account to conclude "stay" is work nobody asked for."""
        self.write_usage(percent=10)
        reads = []

        with mock.patch.object(
            claude, "load_accounts", lambda paths, names: reads.append(names) or []
        ):
            self.run_hook()

        self.assertEqual(reads, [], "the common path must not touch the account store")

    def test_a_pane_that_is_still_working_does_nothing(self):
        self.run_hook(status="working")

        self.assertEqual(self.rotations, [])

    def test_disabling_the_plugin_stops_it_before_it_looks_at_anything(self):
        self.write_config('ACCOUNTS="work personal"\nENABLED=0\n')

        self.assertEqual(self.run_hook(), 0)
        self.assertEqual(self.rotations, [])

    def test_taking_the_lock_does_not_truncate_the_file(self):
        """A pane that loses the race must not have written to it on the way."""
        marker = os.path.join(self.state_dir, "rotate.lock")
        with open(marker, "w") as handle:
            handle.write("held by someone")

        with grazr._rotation_lock(self.state_dir):
            pass

        with open(marker) as handle:
            self.assertEqual(handle.read(), "held by someone")

    def test_a_pane_that_loses_the_race_does_not_rotate_as_well(self):
        """Two panes idle at the same instant. The loser must stay, or it parks
        over what the winner just wrote."""
        held = open(os.path.join(self.state_dir, "rotate.lock"), "w")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        self.assertEqual(self.run_hook(), 0)

        self.assertEqual(self.rotations, [], "the pane that lost the lock must stay")

    def test_the_decision_is_taken_again_once_the_lock_is_held(self):
        """Whatever the first pane did is already done by the time this one gets
        in, so the decision has to be made against the world as it is now."""
        inspected = []
        real_inspect = claude.inspect

        def counting_inspect(paths, now):
            inspected.append(now)
            return real_inspect(paths, now)

        with mock.patch.object(claude, "inspect", counting_inspect):
            self.run_hook()

        self.assertEqual(len(inspected), 2, "inspect once to decide, once under the lock")


class FitnessTest(unittest.TestCase):
    """core.py stays pure and agent-neutral, or the seam has already leaked."""

    def setUp(self):
        beside_this_test = os.path.join(os.path.dirname(os.path.abspath(__file__)), "core.py")
        with open(beside_this_test) as source:
            self.source = source.read()

    def test_core_names_no_agent(self):
        self.assertNotIn("claude", self.source.lower())

    def test_core_reaches_for_no_io(self):
        for forbidden in ("import os", "import subprocess", "import pathlib", "open("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)


if __name__ == "__main__":
    unittest.main()
