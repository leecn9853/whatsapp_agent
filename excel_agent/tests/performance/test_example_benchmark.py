import pytest


@pytest.mark.performance
def test_sum_benchmark(benchmark):
    result = benchmark(sum, range(1000))
    assert result == sum(range(1000))
