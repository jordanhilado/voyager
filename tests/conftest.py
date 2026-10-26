from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text():
    def _load(name: str) -> str:
        return (FIX / name).read_text()

    return _load
