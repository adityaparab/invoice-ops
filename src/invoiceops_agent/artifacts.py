"""Atomic create-only publication for immutable local artifacts."""

import os
import tempfile
from pathlib import Path


def write_new_artifact(path: Path, data: bytes) -> None:
    """Publish complete bytes or raise; never replace an existing destination.

    The parent directory must exist. Failures before publication leave no destination;
    temporary files are removed on normal exit and exceptions. Filesystem errors propagate.
    This helper performs blocking I/O; async callers must run it in a worker thread.
    """
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        os.link(stream.name, path)
