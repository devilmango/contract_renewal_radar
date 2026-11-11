"""PostgreSQL SQL-adapter regression tests that run without a local server."""

import re

from renewal_radar.store import Store, _PostgresConnection


class _Result:
    def __init__(self, row=None, rows=()):
        self._row = row
        self._rows = list(rows)
        self.rowcount = 1 if row is not None or self._rows else 0

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _PostgresMock:
    """Small DB-API mock that rejects the PostgreSQL failure seen in CI."""

    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        # PostgreSQL cannot infer a parameter's type from `$n IS NOT NULL`.
        if re.search(r"%s\s+IS\s+NOT\s+NULL", sql, re.IGNORECASE):
            raise AssertionError("untyped PostgreSQL parameter used in IS NOT NULL")
        self.statements.append((sql, params))

        if sql.startswith("SELECT id FROM scim_users"):
            return _Result()
        if sql.startswith("SELECT id FROM scim_groups"):
            return _Result()
        if sql.startswith("SELECT * FROM scim_users"):
            user_id, tenant_id = params
            return _Result({
                "id": user_id,
                "tenant_id": tenant_id,
                "external_id": None,
                "user_name": "reviewer@example.com",
                "display_name": "Reviewer",
                "email": None,
                "active": 1,
                "attributes_json": "{}",
            })
        if sql.startswith("SELECT * FROM scim_groups"):
            group_id, tenant_id = params
            return _Result({
                "id": group_id,
                "tenant_id": tenant_id,
                "external_id": None,
                "display_name": "Legal reviewers",
            })
        if sql.startswith("SELECT active FROM scim_users"):
            return _Result({"active": 1})
        return _Result(rows=[])


def test_scim_nullable_identity_queries_are_postgres_type_inferable():
    connection = _PostgresMock()
    store = Store.__new__(Store)
    store.connect = lambda: _PostgresConnection(connection)

    user = store.scim_upsert_user(
        "acme",
        {"userName": "reviewer@example.com", "displayName": "Reviewer", "externalId": None},
        "scim-test",
    )
    group = store.scim_upsert_group(
        "acme",
        {"displayName": "Legal reviewers", "externalId": None},
    )

    assert user["active"] is True
    assert group["members"] == []
    assert store.scim_user_is_active("acme", "reviewer@example.com", email=None) is True
    assert connection.statements
    assert all("%s" in sql for sql, _params in connection.statements)
    assert any(
        "user_name=%s OR external_id=%s" in sql and params[2] is None
        for sql, params in connection.statements
    )
    assert any(
        "display_name=%s OR external_id=%s" in sql and params[2] is None
        for sql, params in connection.statements
    )
    assert any(
        "lower(email)=lower(%s)" in sql and params[2] is None
        for sql, params in connection.statements
    )
