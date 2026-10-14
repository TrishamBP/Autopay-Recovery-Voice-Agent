import pytest

from src.database_query.schema_registry import SchemaRegistry
from src.database_query.sql_validator import QueryRejectedError, validate_sql

REGISTRY = SchemaRegistry()


def check(sql: str, max_rows: int = 100):
    return validate_sql(sql, REGISTRY, max_rows=max_rows, max_chars=4000)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT customer_id FROM customer WHERE payment_status = 'failed' "
        "AND next_retry_eligible_at IS NOT NULL",
        "SELECT count(*) AS n, avg(payment_reliability_score) AS avg_score FROM customer",
        "SELECT DISTINCT c.customer_id FROM customer c JOIN payment_history h "
        "ON h.customer_id = c.customer_id WHERE h.status = 'missed'",
        "SELECT subscription_plan, sum(payments_on_time_6m) AS on_time FROM customer "
        "GROUP BY subscription_plan ORDER BY 1",
        "WITH m AS (SELECT customer_id, count(*) AS missed FROM payment_history "
        "WHERE status = 'missed' GROUP BY customer_id) SELECT c.full_name, m.missed "
        "FROM customer c JOIN m USING (customer_id)",
        "SELECT customer_id FROM customer UNION SELECT customer_id FROM payment_history",
        "SELECT customer_id, row_number() OVER (ORDER BY payment_reliability_score) AS r "
        "FROM customer",
        "SELECT strftime('%Y-%m', due_date) AS m, count(*) AS n FROM payment_history GROUP BY m",
    ],
)
def test_valid_read_only_queries_are_accepted(sql: str) -> None:
    validated = check(sql)
    assert validated.sql.upper().startswith(("SELECT", "WITH"))
    assert "LIMIT" in validated.sql.upper()


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("UPDATE customer SET do_not_contact = 0", "only read-only"),
        ("DELETE FROM customer", "only read-only"),
        ("DROP TABLE customer", "only read-only"),
        ("ALTER TABLE customer ADD COLUMN x TEXT", "only read-only"),
        ("CREATE TABLE t (a)", "only read-only"),
        ("INSERT INTO customer (customer_id) VALUES ('x')", "only read-only"),
        ("REPLACE INTO customer VALUES (1)", "only read-only"),
        ("PRAGMA table_info(customer)", "only read-only"),
        ("ATTACH DATABASE 'x.db' AS y", "only read-only"),
        ("DETACH y", "only read-only"),
        ("VACUUM", "only read-only"),
        ("SELECT 1; DROP TABLE customer", "exactly one"),
        ("SELECT 1; SELECT 2", "exactly one"),
        ("SELECT * FROM audit_log", "not available"),
        ("SELECT * FROM tool_execution", "not available"),
        ("SELECT * FROM call_scenario", "not available"),
        ("SELECT * FROM sqlite_master", "not available"),
        ("SELECT * FROM main.customer", "schema-qualified"),
        ("SELECT * FROM pragma_table_info('customer')", "table-valued"),
        ("SELECT phone_number FROM customer", "unavailable column"),
        ("SELECT c.email FROM customer c", "unavailable column"),
        ("SELECT token FROM payment_link", "unavailable column"),
        ("SELECT rowid FROM customer", "unavailable column"),
        ("SELECT (SELECT phone_number FROM customer LIMIT 1) AS p", "unavailable column"),
        ("SELECT customer_id FROM customer UNION SELECT entity_id FROM audit_log", "not available"),
        ("SELECT load_extension('x')", "not allowed"),
        ("SELECT randomblob(10)", "not allowed"),
        ("SELECT hex(full_name) FROM customer", "not allowed"),
        (
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r",
            "recursive",
        ),
        ("SELECT * INTO t FROM customer", "SELECT INTO"),
        ("SELECT customer_id FROM customer LIMIT (SELECT 1)", "LIMIT"),
        ("", "empty"),
        ("SELEC customer_id FRM customer", None),
    ],
)
def test_unsafe_or_unknown_sql_is_rejected(sql: str, reason: str | None) -> None:
    with pytest.raises(QueryRejectedError, match=reason):
        check(sql)


def test_star_expands_to_allowlisted_columns_only() -> None:
    validated = check("SELECT * FROM customer")
    for hidden in ("phone_number", "email", "payment_link_url", "last_call_id"):
        assert hidden not in validated.sql
    assert '"customer"."full_name"' in validated.sql


def test_literals_become_bound_parameters() -> None:
    validated = check("SELECT customer_id FROM customer WHERE customer_id = 'CUS-1001' OR 1 = 1")
    assert "CUS-1001" not in validated.sql
    assert "CUS-1001" in validated.parameters.values()
    assert ":p0" in validated.sql


def test_injection_payload_inside_string_literal_stays_data() -> None:
    validated = check(
        "SELECT customer_id FROM customer WHERE full_name = 'x''; DROP TABLE customer; --'"
    )
    assert "DROP" not in validated.sql
    assert any("DROP TABLE" in str(v) for v in validated.parameters.values())


def test_limit_is_forced_and_smaller_caller_limit_kept() -> None:
    assert check("SELECT customer_id FROM customer", max_rows=10).sql.endswith("LIMIT 11")
    assert check("SELECT customer_id FROM customer LIMIT 5000", max_rows=10).sql.endswith(
        "LIMIT 11"
    )
    assert check("SELECT customer_id FROM customer LIMIT 3", max_rows=10).sql.endswith("LIMIT 3")


def test_oversized_sql_is_rejected() -> None:
    with pytest.raises(QueryRejectedError, match="longer than"):
        validate_sql("SELECT 1" + " " * 5000, REGISTRY, max_rows=10, max_chars=100)
