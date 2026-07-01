"""Pytest entry for the Cogni-vs-Verilator oracle. Skips without verilator."""
import pytest
from agent.oracle_check import check, _verilator_present


@pytest.mark.skipif(not _verilator_present(), reason="verilator not installed")
def test_no_inference_regression():
    assert check(verbose=True) == 0, "Cogni diverged from Verilator beyond baseline"
