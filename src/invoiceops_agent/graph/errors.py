"""Typed and sanitized orchestration failures."""

from uuid import UUID


class GraphError(Exception):
    def __init__(
        self, detail: str, *, run_id: UUID | None = None, trace_id: str | None = None
    ) -> None:
        self.run_id = run_id
        self.trace_id = trace_id
        super().__init__(f"{detail} run_id={run_id} trace_id={trace_id}")


class RunConflict(GraphError):
    """A persisted run belongs to a different invoice."""


class RunInProgress(GraphError):
    """Another process currently owns the run's advisory lock."""


class GraphExecutionError(GraphError):
    """A node failed; the persisted checkpoint can be resumed."""


class GraphTimeout(GraphError):
    """The configured graph execution time limit expired."""


class CheckpointUnavailable(GraphError):
    """The checkpoint store could not be reached or initialized."""


class InvalidCheckpoint(GraphError):
    """Persisted state is incompatible with this graph version."""
