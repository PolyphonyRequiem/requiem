"""Workflow DSL — Wagner A (fluent builder lowered to pydantic data).

Authors write left-to-right method chains; `.build()` validates topology
(typos die at construction, not at run-time, per `INV-NO-CORRUPT-FORWARD`).
The build output is a pure pydantic `Workflow` the kernel interprets —
the kernel never imports user code by type, only by registered name.
"""
from __future__ import annotations

from typing import Any, Callable, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from requiem.agent import AgentSpec


# ---- data model (Beethoven C: kernel sees nothing but this) -----------


class ScriptNode(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    kind: Literal["script"] = "script"
    node_id: str
    verb: str
    retry_max: int = 0


class AgentNode(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    kind: Literal["agent"] = "agent"
    node_id: str
    agent: str
    prompt_verb: str
    retry_max: int = 0


class TeamBranchModel(BaseModel):
    """One arm of a parallel fork — an agent invocation."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    agent: str
    prompt_verb: str


class TeamNode(BaseModel):
    """`parallel_fork` primitive. Awaits every branch concurrently."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    kind: Literal["team"] = "team"
    node_id: str
    team_id: str
    branches: list[TeamBranchModel]


class HumanGateNode(BaseModel):
    kind: Literal["human_gate"] = "human_gate"
    node_id: str
    prompt: str
    options: list[str]


class TerminateNode(BaseModel):
    kind: Literal["terminate"] = "terminate"
    node_id: str
    disposition: Literal["completed", "failed", "cancelled", "needs_human"] = "completed"


class SubWorkflowNode(BaseModel):
    """Invokes another workflow as a node within the parent.

    The child workflow is loaded by importable module path (same convention
    as ``requiem run <module>``) and given its own Engine instance. The
    child writes to its OWN ``{sub_run_id}.events.jsonl`` log so the
    parent's log stays a clean record of the parent's transitions (Bach A
    purity). The parent's log records ``subworkflow_started`` /
    ``subworkflow_completed`` markers — enough to resume after a crash.

    See ADR 0005 and ``INV-SUBWORKFLOW-LOG-ISOLATION`` in the north-star.
    """

    kind: Literal["subworkflow"] = "subworkflow"
    node_id: str
    workflow_module: str
    inputs_verb: str | None = None
    """Optional verb name that returns a dict of inputs for the child.

    The returned dict is recorded in ``subworkflow_started.inputs_summary``
    for observability, and passed to the child's ``build_engine`` as an
    ``inputs`` kwarg if the factory accepts one. v0 author-cooperative
    contract: child workflows are responsible for fetching their own inputs
    via the toolbelt — passing inputs across engine instances is best-effort.
    """
    sub_run_id: str | None = None
    """Optional override for the child run_id.

    Defaults to ``f'{parent_run_id}__{node_id}'`` (double underscore — ``::``
    is unsafe on Windows paths). Three-level nesting yields
    ``g__p__c``, all distinct files in the same log_dir.
    """
    retry_max: int = 0


NodeModel = Union[
    ScriptNode, AgentNode, TeamNode, HumanGateNode, TerminateNode, SubWorkflowNode
]


class Edge(BaseModel):
    from_node: str
    on: str
    to_node: str


class Workflow(BaseModel):
    """Pure-data workflow. The kernel interprets this; the DSL produces it."""

    name: str
    entry: str
    nodes: list[NodeModel] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    humanize: dict[str, str] = Field(default_factory=dict)
    """Per-node human-readable labels for the CLI renderer.

    Maps `node_id` → noun phrase used in narration lines
    (e.g. `"read_snippet"` → `"Read sample_snippet.py"`). Default empty:
    the renderer falls back to the raw `node_id`.
    """
    module: str | None = None
    """Importable module path that produced this workflow.

    Recorded in the ``run_started`` event so post-hoc tools (``requiem
    events``, ``requiem list-runs``) can re-import the workflow and recover
    its humanize map / render hints without an explicit ``--workflow`` flag.
    Optional; ``None`` is permitted for ad-hoc workflows built in tests.
    """
    version: str = "0"
    """Workflow version per ADR 0004 §4.7.

    Recorded in ``run_started`` so replay against a changed workflow shape
    can be detected (and refused unless ``--force-replay`` is passed).
    """

    def validate_topology(self) -> list[str]:
        errs: list[str] = []
        nm = {n.node_id: n for n in self.nodes}
        if len(nm) != len(self.nodes):
            errs.append("duplicate node_id")
        if self.entry not in nm:
            errs.append(f"entry {self.entry!r} not in nodes")
        for e in self.edges:
            if e.from_node not in nm:
                errs.append(f"edge from unknown node {e.from_node!r}")
            if e.to_node not in nm:
                errs.append(f"edge to unknown node {e.to_node!r}")
        terminals = [n for n in self.nodes if isinstance(n, TerminateNode)]
        if not terminals:
            errs.append("workflow has no terminate node")
        return errs


# ---- Wagner-A fluent builder -----------------------------------------


class WorkflowBuilder:
    """Fluent. Every method returns self. `.build()` runs topology checks."""

    def __init__(self, name: str, *, module: str | None = None, version: str = "0") -> None:
        self._name = name
        self._entry: str | None = None
        self._nodes: list[NodeModel] = []
        self._edges: list[Edge] = []
        self._humanize: dict[str, str] = {}
        self._module = module
        self._version = version

    def entry(self, node_id: str) -> "WorkflowBuilder":
        self._entry = node_id
        return self

    def script(
        self, node_id: str, *, verb: str, retry_max: int = 0
    ) -> "WorkflowBuilder":
        self._nodes.append(
            ScriptNode(node_id=node_id, verb=verb, retry_max=retry_max)
        )
        return self

    def agent(
        self,
        node_id: str,
        *,
        agent: str,
        prompt_verb: str,
        retry_max: int = 0,
    ) -> "WorkflowBuilder":
        self._nodes.append(
            AgentNode(
                node_id=node_id,
                agent=agent,
                prompt_verb=prompt_verb,
                retry_max=retry_max,
            )
        )
        return self

    def team(
        self,
        node_id: str,
        *,
        team_id: str,
        branches: list[tuple[str, str]],
    ) -> "WorkflowBuilder":
        """The agent-team primitive (`.team(...)` sugar over `parallel_fork`).

        `branches` is a list of (agent_name, prompt_verb) — each runs in
        parallel; results aggregate into `Success.value['findings']`.
        """
        self._nodes.append(
            TeamNode(
                node_id=node_id,
                team_id=team_id,
                branches=[
                    TeamBranchModel(agent=a, prompt_verb=p) for a, p in branches
                ],
            )
        )
        return self

    def human_gate(
        self, node_id: str, *, prompt: str, options: list[str]
    ) -> "WorkflowBuilder":
        self._nodes.append(
            HumanGateNode(node_id=node_id, prompt=prompt, options=options)
        )
        return self

    def terminate(
        self, node_id: str, *, disposition: str = "completed"
    ) -> "WorkflowBuilder":
        self._nodes.append(
            TerminateNode(node_id=node_id, disposition=disposition)  # type: ignore[arg-type]
        )
        return self

    def subworkflow(
        self,
        node_id: str,
        *,
        workflow: str,
        inputs_verb: str | None = None,
        sub_run_id: str | None = None,
        retry_max: int = 0,
    ) -> "WorkflowBuilder":
        """Add a sub-workflow invocation node.

        ``workflow`` is the importable module path of the child workflow
        (e.g. ``requiem.workflows.code_review_demo``). The module must
        expose ``build_engine(log_dir, ...)`` per the standard contract.

        ``inputs_verb`` (optional) names a registered verb that returns a
        dict; it's recorded in ``subworkflow_started.inputs_summary`` and
        forwarded to the child's ``build_engine`` as an ``inputs`` kwarg
        if the factory accepts one. See ADR 0005.
        """
        self._nodes.append(
            SubWorkflowNode(
                node_id=node_id,
                workflow_module=workflow,
                inputs_verb=inputs_verb,
                sub_run_id=sub_run_id,
                retry_max=retry_max,
            )
        )
        return self

    def edge(self, from_node: str, *, on: str, to: str) -> "WorkflowBuilder":
        self._edges.append(Edge(from_node=from_node, on=on, to_node=to))
        return self

    def humanize(self, mapping: dict[str, str]) -> "WorkflowBuilder":
        """Register human-readable noun phrases for nodes.

        Merges into the existing map; later calls override earlier ones.
        """
        self._humanize.update(mapping)
        return self

    def module(self, dotted: str) -> "WorkflowBuilder":
        """Record the importable module path that produced this workflow.

        Lets ``requiem events`` and ``requiem list-runs`` recover the
        humanize map and render hints from a stored log without an
        explicit ``--workflow`` flag.
        """
        self._module = dotted
        return self

    def version(self, ver: str) -> "WorkflowBuilder":
        """Pin a workflow version (per ADR 0004 §4.7).

        Recorded in ``run_started`` so replay against a mismatched shape
        can be detected.
        """
        self._version = ver
        return self

    def build(self) -> Workflow:
        if self._entry is None:
            raise ValueError(f"workflow {self._name!r} has no entry")
        wf = Workflow(
            name=self._name,
            entry=self._entry,
            nodes=self._nodes,
            edges=self._edges,
            humanize=self._humanize,
            module=self._module,
            version=self._version,
        )
        errs = wf.validate_topology()
        if errs:
            raise ValueError(f"workflow {self._name!r} invalid: {errs}")
        return wf


# ---- registries -------------------------------------------------------

VerbFn = Callable[..., Any]


class VerbRegistry:
    """Verbs are looked up by name; the workflow is pure data."""

    def __init__(self) -> None:
        self._verbs: dict[str, VerbFn] = {}

    def register(self, name: str) -> Callable[[VerbFn], VerbFn]:
        def deco(fn: VerbFn) -> VerbFn:
            if name in self._verbs:
                raise ValueError(f"verb {name!r} already registered")
            self._verbs[name] = fn
            return fn

        return deco

    def get(self, name: str) -> VerbFn:
        try:
            return self._verbs[name]
        except KeyError as e:
            raise KeyError(f"verb {name!r} not registered") from e


class AgentRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> AgentSpec:
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> AgentSpec:
        try:
            return self._specs[name]
        except KeyError as e:
            raise KeyError(f"agent {name!r} not registered") from e
