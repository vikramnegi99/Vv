import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import load_config  # noqa: E402
from agent.db import DB  # noqa: E402
from agent.providers.mock import canary_scenario  # noqa: E402


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def db(tmp_path):
    d = DB(str(tmp_path / "state.db"))
    d.migrate(backup_dir=str(tmp_path / "backups"))
    yield d
    d.close()


@pytest.fixture
def scenario():
    """(market, history) engineered to produce a strategy signal."""
    return canary_scenario(None)
