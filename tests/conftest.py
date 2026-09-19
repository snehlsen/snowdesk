from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Qt must not try to open a real window in the test suite.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def fake_conn():
    from tests.fakes import FakeConnection

    return FakeConnection()
