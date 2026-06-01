from requiem.teams import TeamBranch


def test_team_branch_round_trip():
    b = TeamBranch(agent="a1", prompt_verb="p")
    assert b.as_tuple() == ("a1", "p")
