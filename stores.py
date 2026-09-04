"""Credential stores: where the live and parked secrets are kept.

Callers never learn which store they are talking to; how items are named and
moved is each store's own business.
"""

import hashlib
import re
import shlex
import subprocess
import unicodedata

SERVICE = "Claude Code-credentials"

# At 4096 security truncates the line, writes the truncated prefix over the
# item, and only then exits 1.
MAX_SECURITY_LINE = 4095

# A swap makes three keychain calls; all three at full timeout must still fit
# inside the lock stale ages claude.py mirrors.
SECURITY_TIMEOUT_SECONDS = 15

# errSecItemNotFound. Any other non-zero exit means the keychain itself was
# unhappy, which is not the same as the item being absent.
ITEM_NOT_FOUND = 44


def service_name(config_dir=None):
    """Claude's own scheme: one item per config directory. NFC-normalise before
    hashing, because the filesystem can return an umlaut as two characters and
    the same directory must always hash the same."""
    if config_dir is None:
        return SERVICE
    normalized = unicodedata.normalize("NFC", config_dir)
    return "%s-%s" % (SERVICE, hashlib.sha256(normalized.encode()).hexdigest()[:8])


class KeychainStore:
    def __init__(self, service, keychain_account, spawn=subprocess.run):
        self.service = service
        self.keychain_account = keychain_account
        self._spawn = spawn

    def read_live(self):
        return self._read(self.service)

    def write_live(self, blob):
        self._install(self.service, blob)

    def read_parked(self, account_id):
        return self._read(self._parked_service(account_id))

    def write_parked(self, account_id, blob):
        self._install(self._parked_service(account_id), blob)

    def read_isolated(self, config_dir):
        return self._read(service_name(config_dir))

    def discard_isolated(self, config_dir):
        """Returns whether the item is gone, and never raises: the caller runs
        in a finally, and masking the failure it is cleaning up after would
        hide the real problem."""
        try:
            completed = self._spawn(
                [
                    "security",
                    "delete-generic-password",
                    "-s",
                    service_name(config_dir),
                    "-a",
                    self.keychain_account,
                ],
                capture_output=True,
                timeout=SECURITY_TIMEOUT_SECONDS,
            )
            return getattr(completed, "returncode", 0) in (0, ITEM_NOT_FOUND)
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _parked_service(self, account_id):
        return "grazr-%s" % account_id

    def _read(self, service):
        """The stored blob, or None when there is no such item. Service and
        account are not secrets, so argv is fine here; the blob comes back on
        stdout."""
        try:
            completed = self._spawn(
                ["security", "find-generic-password", "-s", service, "-a", self.keychain_account, "-w"],
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

    def _install(self, service, blob):
        # `security -i` splits its line into tokens and the live service name
        # has a space, so the names need quoting. The hex payload never does.
        line = "add-generic-password -U -s %s -a %s -X %s" % (
            shlex.quote(service),
            shlex.quote(self.keychain_account),
            blob.encode().hex(),
        )
        size = len(line.encode())
        if size > MAX_SECURITY_LINE:
            raise ValueError(
                "credential for %s needs a %d byte security line, over the %d byte limit; "
                "installing it would truncate and destroy the item"
                % (service, size, MAX_SECURITY_LINE)
            )
        self._run_security(line)

    def _run_security(self, line):
        """Feed one command to `security -i`. The secret rides stdin, argv stays clean."""
        try:
            completed = self._spawn(
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
            # Scrub before trimming, or the cut can leave a hex fragment too
            # short to match.
            scrubbed = re.sub(r"[0-9a-f]{8,}", "<hex>", completed.stderr.strip())[:200]
            raise RuntimeError("security refused the command: %s" % scrubbed)
        return completed.stdout
