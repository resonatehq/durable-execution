"""The exhaustive search, at depths that fit in a test run. Deeper runs are
`python explore.py --depth N [--alphabet narrow]`.

Two profiles, because breadth and depth trade against each other. The broad
alphabet says many things a few steps deep, which is where the door checks and
the birth shapes live. The narrow one says few things far enough to reach the
long chains — acquire, suspend, settle, wake, halt, continue — which no broad
search gets to and which is exactly where the Hypothesis machine found a
divergence from the specification.
"""

from resonate.explore import BROAD, NARROW, explore


def test_the_broad_alphabet_to_depth_3():
    per_depth, edges, tally = explore(3, ab=BROAD)
    assert sum(per_depth) > 500 and edges > 5_000
    for k in ("born_pending", "born_dead", "born_acquired", "delayed", "expired", "retry",
              "unblock", "execute", "refused"):
        assert tally[k] > 0, (k, tally)


def test_the_narrow_alphabet_to_depth_5():
    """Every state reachable in five steps of the long-chain alphabet, with
    the whole catalogue checked on each. This is what proves the deep
    transitions are reached: a random search only samples them."""
    per_depth, edges, tally = explore(5, ab=NARROW)
    assert sum(per_depth) > 10_000 and edges > 40_000
    for k in ("wake", "halted_buffer", "lease_expired", "carry_on_300", "retry",
              "born_acquired", "unblock", "execute"):
        assert tally[k] > 0, (k, tally)
