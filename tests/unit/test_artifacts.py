"""Immutable publication survives interrupted writes without poisoning retries."""

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from invoiceops_agent.artifacts import write_new_artifact

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_failed_publication_leaves_no_partial_file_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    target = tmp_path / "immutable.bin"
    data = b"synthetic-artifact-contents\x00\xff"
    real_temporary_file = tempfile.NamedTemporaryFile

    @contextmanager
    def partial_write(*, mode: str, dir: Path) -> Iterator[MagicMock]:
        with real_temporary_file(mode=mode, dir=dir) as stream:

            def fail_write(value: bytes) -> None:
                stream.write(value[:3])
                stream.flush()
                raise OSError("Synthetic mid-write failure")

            wrapper = MagicMock(wraps=stream)
            wrapper.write.side_effect = fail_write
            yield wrapper

    def fail_fsync(descriptor: int) -> None:
        raise OSError("Synthetic fsync failure")

    with monkeypatch.context() as patches:
        if failure == "write":
            patches.setattr("invoiceops_agent.artifacts.tempfile.NamedTemporaryFile", partial_write)
        else:
            patches.setattr("invoiceops_agent.artifacts.os.fsync", fail_fsync)
        with pytest.raises(OSError):
            write_new_artifact(target, data)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []

    write_new_artifact(target, data)
    assert target.read_bytes() == data
    assert list(tmp_path.iterdir()) == [target]


def test_existing_artifact_is_preserved_and_temporary_file_is_cleaned(tmp_path: Path) -> None:
    target = tmp_path / "immutable.bin"
    target.write_bytes(b"original-artifact")
    with pytest.raises(FileExistsError):
        write_new_artifact(target, b"replacement-must-not-be-published")
    assert target.read_bytes() == b"original-artifact"
    assert list(tmp_path.iterdir()) == [target]
