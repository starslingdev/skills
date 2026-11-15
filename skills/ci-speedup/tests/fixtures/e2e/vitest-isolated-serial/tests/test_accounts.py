import pytest


@pytest.mark.parametrize("case", range(4))
def test_account(case):
    print(f"schema_name=schema_test_{case + 1}")
