"""The database commits BEFORE the response is sent, and refusals say so.

`get_db` commits when it exits. A yield dependency's default ("request")
scope exits after the response has gone out, so a commit the database refused
— a RESTRICT foreign key on delete, a unique index — used to be answered 2xx
and then silently rolled back. These pin the two halves of the fix: the
dependency scope, and a constraint failure becoming a 409/422 with a sentence
a person can act on rather than "an internal error occurred".
"""

from __future__ import annotations

from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from suliko.api import deps, portal_deps
from suliko.core.errors import install_error_handlers


class PasswordBody(BaseModel):
    password: str = Field(min_length=12)


@pytest.mark.parametrize("alias", [deps.Db, portal_deps.PlatformDb])
def test_database_dependencies_are_function_scoped(alias: object) -> None:
    depends = get_args(alias)[1]
    assert depends.scope == "function", (
        "request scope commits after the response is sent — a refused commit "
        "would already have been answered 2xx"
    )


def _integrity_error(sql: str) -> IntegrityError:
    """A real driver error from SQLite, so the classifier sees what it will see."""
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("CREATE TABLE parent (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE child (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER NOT NULL REFERENCES parent(id), "
                "amount INTEGER CHECK (amount > 0), code TEXT UNIQUE)"
            )
        )
        conn.execute(text("INSERT INTO parent (id) VALUES (1)"))
        conn.execute(text("INSERT INTO child (id, parent_id, amount, code) VALUES (1, 1, 5, 'a')"))
        try:
            conn.execute(text(sql))
        except IntegrityError as exc:
            return exc
    raise AssertionError("statement did not violate a constraint")


def _client(exc: Exception) -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("sql", "status", "code"),
    [
        ("INSERT INTO child (id, parent_id, amount, code) VALUES (2, 1, 5, 'a')", 409, "conflict"),
        ("DELETE FROM parent WHERE id = 1", 409, "in_use"),
        ("INSERT INTO child (id, parent_id, amount) VALUES (3, 1, -1)", 422, "validation_failed"),
        ("INSERT INTO child (id, amount) VALUES (4, 5)", 422, "validation_failed"),
    ],
)
def test_constraint_failures_are_answered_as_what_they_are(
    sql: str, status: int, code: str
) -> None:
    response = _client(_integrity_error(sql)).get("/boom")

    assert response.status_code == status
    body = response.json()
    assert body["type"].endswith(f"/{code}")
    # The driver's message quotes SQL and values; none of it may leak.
    assert "child" not in body["detail"] and "parent" not in body["detail"]


def test_validation_errors_never_echo_what_was_sent() -> None:
    """A too-short password or an integration secret would otherwise be
    copied into the response, and from there into every log it passes."""
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/set")
    async def set_password(body: PasswordBody) -> None:
        return None

    response = TestClient(app).post("/set", json={"password": "hunter2"})

    assert response.status_code == 422
    assert "hunter2" not in response.text
    body = response.json()
    assert body["errors"][0]["loc"] == ["body", "password"]
    assert body["detail"].startswith("password:")
