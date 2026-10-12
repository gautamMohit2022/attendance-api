import os
import pathlib
import sys

# app.main reads its configuration at import time; the values are never used to connect in unit tests.
os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGO_DB", "unit_tests")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import mongomock  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import minimongo  # noqa: E402


class FakeColl:
    """mongomock handles CRUD; aggregation is delegated to tests/minimongo.py."""

    def __init__(self, coll, database):
        self._c, self._db = coll, database

    def __getattr__(self, name):
        return getattr(self._c, name)

    def aggregate(self, pipeline):
        return iter(minimongo.run_pipeline(list(self._c.find({})), pipeline, self._db))


class FakeDB:
    def __init__(self):
        self._m = mongomock.MongoClient(tz_aware=True)["unit"]
        self.employees = FakeColl(self._m["employees"], self)
        self.attendance_logs = FakeColl(self._m["attendance_logs"], self)

    def __getitem__(self, name):
        return getattr(self, name)

    def command(self, *_a, **_k):
        return {"ok": 1.0, "queryPlanner": {"winningPlan": {"stage": "FAKE"}}}


class FakeClient:
    class admin:
        @staticmethod
        def command(*_a, **_k):
            return {"ok": 1}


@pytest.fixture()
def main():
    import app.main as m
    m.db = FakeDB()
    m.client = FakeClient()
    m.ensure_indexes()
    return m


@pytest.fixture()
def api(main):
    return TestClient(main.app)
