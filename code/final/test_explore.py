"""The exhaustive search, at a depth that fits in a test run. Deeper runs are
`python explore.py --depth N`."""

from explore import explore


def test_every_reachable_state_to_depth_3_holds_the_catalogue():
    per_depth, edges, tally = explore(3)
    assert sum(per_depth) > 500 and edges > 5_000
    # The guards that need a few steps to bite are reached by depth 3.
    for k in ("born_pending", "born_dead", "born_acquired", "delayed", "expired", "retry",
              "lease_expired", "unblock", "execute", "refused"):
        assert tally[k] > 0, (k, tally)
