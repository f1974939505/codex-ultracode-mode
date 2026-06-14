from src.sample_math import add, risky_divide


def test_add():
    assert add(2, 3) == 5


def test_risky_divide():
    assert risky_divide(6, 2) == 3
