"""Unit tests for the pure delta-debugging minimizer used by stress mode."""

from __future__ import annotations

from app.stress import ddmin


def test_reduces_to_the_two_required_elements():
    minimal = ddmin(list(range(20)), lambda subset: 3 in subset and 11 in subset)
    assert set(minimal) == {3, 11}


def test_single_required_element():
    assert ddmin([1, 2, 3, 4, 5], lambda subset: 4 in subset) == [4]


def test_preserves_order():
    assert ddmin([5, 1, 9, 2, 7], lambda subset: 9 in subset and 7 in subset) == [9, 7]


def test_keeps_everything_when_all_required():
    items = [1, 2, 3]
    assert ddmin(items, lambda subset: len(subset) == 3) == [1, 2, 3]


def test_only_keeps_reductions_that_satisfy_predicate():
    # Predicate needs the sum to stay >= 100; ddmin must not drop below it.
    items = [60, 50, 1, 1, 1]
    minimal = ddmin(items, lambda subset: sum(subset) >= 100)
    assert sum(minimal) >= 100
