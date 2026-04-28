import sqlite3

import pytest

from code_index import config as cfg_mod
from code_index import db_router as db_mod


def test_open_config_applies_schema_and_closes_connection(tmp_path):
    config = cfg_mod.Config(root=tmp_path)

    with db_mod.open_config(config, schema="apply") as conn:
        assert db_mod.get_schema_version(conn) == db_mod.SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] >= 1

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
