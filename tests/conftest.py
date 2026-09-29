from pathlib import Path

import pytest

from sop.insurance.data import FixtureRepo

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="session")
def repo() -> FixtureRepo:
    return FixtureRepo(FIXTURES)
