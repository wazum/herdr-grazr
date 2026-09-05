"""Putting a file in place so no reader ever sees it half made.

grazr, Claude and the panes all read each other's files with no lock between
them, and a plain open empties a file before it fills it.
"""

import os
import tempfile


def write(path, text):
    """Write `text` and move it over `path` in one step.

    The temporary file is made in the target's own directory, because a replace
    across filesystems is not atomic. It carries mkstemp's mode, owner-only,
    which is what the credential and the account files need.
    """
    directory = os.path.dirname(path) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".grazr-")
    try:
        with os.fdopen(handle, "w") as writer:
            writer.write(text)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
