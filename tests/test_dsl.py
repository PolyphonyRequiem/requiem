import pytest

from requiem.dsl import WorkflowBuilder


def test_simple_workflow_builds():
    wf = (
        WorkflowBuilder("w").entry("a")
            .script("a", verb="v").edge("a", on="success", to="end")
            .terminate("end").build()
    )
    assert wf.name == "w"
    assert len(wf.nodes) == 2


def test_missing_entry_rejected():
    with pytest.raises(ValueError):
        WorkflowBuilder("w").script("a", verb="v").terminate("end").build()


def test_edge_to_unknown_node_rejected():
    with pytest.raises(ValueError, match="edge to unknown"):
        (
            WorkflowBuilder("w").entry("a")
                .script("a", verb="v").edge("a", on="success", to="ghost")
                .terminate("end").build()
        )


def test_no_terminate_rejected():
    with pytest.raises(ValueError, match="no terminate"):
        WorkflowBuilder("w").entry("a").script("a", verb="v").build()


def test_team_node_serialises_branches():
    wf = (
        WorkflowBuilder("w").entry("t")
            .team("t", team_id="grp", branches=[("a1", "p"), ("a2", "p")])
            .edge("t", on="success", to="end")
            .terminate("end").build()
    )
    team = wf.nodes[0]
    assert team.kind == "team"
    assert [b.agent for b in team.branches] == ["a1", "a2"]
