import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from avwap.config import load_settings  # noqa: E402
from avwap.db import DB  # noqa: E402


@pytest.fixture
def cfg():
    return load_settings()


@pytest.fixture
def db():
    return DB(":memory:")
