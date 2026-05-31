"""Walking Skeleton α — the compact integrated Requiem engine.

Composes Phase A recommended variants:

    outcomes     Stravinsky B   PEP 604 sealed unions + match
    events       Brahms B       envelope-loose + typed emit helper
    persistence  Bach A         pure log
    kernel       Beethoven C    data-driven interpreter
    dsl          Wagner A       fluent builder → pydantic data
    agent        Mahler A       Protocol AgentProvider + FakeProvider
    toolbelt     Liszt B+C      per-tool clients in a frozen Toolbelt
    teams        Pattern #9     .team() sugar over parallel_fork primitive

Nothing in this package is intended for production — it is the smallest
shape that proves the Phase A recommendations compose without friction.
"""
from engine.outcomes import (  # noqa: F401
    Outcome, Success, RetryableFailure, PermanentFailure, NeedsHuman, Cancelled,
)
from engine.events import EventEmitter  # noqa: F401
from engine.persistence import EventStore, replay  # noqa: F401
from engine.kernel import Engine, RunResult, Completed, Suspended, Failed  # noqa: F401
from engine.dsl import Workflow  # noqa: F401
from engine.agent import AgentProvider, AgentSpec, AgentCall, FakeProvider  # noqa: F401
from engine.toolbelt import Toolbelt, GitClient, FileClient  # noqa: F401
from engine.teams import TeamBranch  # noqa: F401
