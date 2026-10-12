import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from game_engine import load_content  # noqa: E402


@pytest.fixture(scope="session")
def content():
    return load_content()


@pytest.fixture()
def db_path(tmp_path):
    import database

    path = tmp_path / "test.db"
    database.init_db(path)
    return path
