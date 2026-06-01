"""Requiem — single-process SDLC orchestration engine.

Top-level re-exports for the package public surface.
"""
from requiem.agent import AgentCall, AgentProvider, AgentSpec, FakeProvider
from requiem.dsl import (
    AgentNode,
    AgentRegistry,
    Edge,
    HumanGateNode,
    ScriptNode,
    TeamBranchModel,
    TeamNode,
    TerminateNode,
    VerbRegistry,
    Workflow,
    WorkflowBuilder,
)
from requiem.events import Event, EventEmitter, SCHEMA_VERSION, parse_envelope
from requiem.kernel import (
    Completed,
    Engine,
    Failed,
    RunResult,
    Suspended,
    VerbContext,
)
from requiem.outcomes import (
    BadOutput,
    Cancelled,
    NeedsHuman,
    Outcome,
    PermanentFailure,
    RetryableFailure,
    Success,
    outcome_from_dict,
    outcome_kind,
    outcome_to_dict,
)
from requiem.persistence import CorruptLogError, EventStore, replay
from requiem.teams import TeamBranch
from requiem.providers import (
    AnthropicProvider,
    OpenAIProvider,
    default_provider,
    make_receipt,
)
from requiem.clients.gh import (
    GhAuthError,
    GhClient,
    GhClientError,
    GhNotFoundError,
    GhPullRequest,
    GhRateLimitedError,
    GhServerError,
    GhUnknownError,
)
from requiem.toolbelt import (
    FileClient,
    FileMissing,
    FileRead,
    GitClient,
    GitNotARepo,
    GitShowMissing,
    GitShowOk,
    Toolbelt,
)

__version__ = "0.0.1"

__all__ = [
    "__version__",
    # outcomes
    "Outcome", "Success", "RetryableFailure", "PermanentFailure",
    "BadOutput", "NeedsHuman", "Cancelled",
    "outcome_to_dict", "outcome_from_dict", "outcome_kind",
    # events
    "Event", "EventEmitter", "SCHEMA_VERSION", "parse_envelope",
    # persistence
    "EventStore", "CorruptLogError", "replay",
    # dsl
    "Workflow", "WorkflowBuilder", "Edge",
    "ScriptNode", "AgentNode", "TeamNode", "TeamBranchModel",
    "HumanGateNode", "TerminateNode",
    "VerbRegistry", "AgentRegistry",
    # agent
    "AgentSpec", "AgentCall", "AgentProvider", "FakeProvider",
    # providers (real LLMs)
    "AnthropicProvider", "OpenAIProvider", "default_provider", "make_receipt",
    # toolbelt
    "Toolbelt", "FileClient", "GitClient",
    "FileRead", "FileMissing", "GitShowOk", "GitShowMissing", "GitNotARepo",
    # clients.gh
    "GhClient", "GhPullRequest",
    "GhClientError", "GhRateLimitedError", "GhNotFoundError",
    "GhAuthError", "GhServerError", "GhUnknownError",
    # teams
    "TeamBranch",
    # kernel
    "Engine", "RunResult", "Completed", "Suspended", "Failed", "VerbContext",
]
