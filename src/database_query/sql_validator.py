"""AST validation of untrusted (LLM-generated) SQL. Regex is never used as the control.

Pipeline: size limit -> parse (SQLGlot, sqlite dialect) -> exactly one statement -> root must
be SELECT or a set operation of SELECTs -> walk every node:
  - reject mutation/DDL/administrative nodes, SELECT ... INTO, FOR UPDATE, WITH RECURSIVE
  - reject any function not in an explicit allowlist (incl. every unknown/anonymous function,
    e.g. load_extension, readfile, pragma_* table-valued functions)
  - reject schema-qualified tables (main.x, attached DBs) and tables outside the allowlist
-> resolve every column against a schema containing ONLY allowlisted columns (unknown column ->
   rejected; `*` expands to allowlisted columns only) -> force a row LIMIT -> move literal values
   into bound parameters -> regenerate SQL from the AST.

Only the regenerated SQL is executed, never the model's original text.
"""

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from src.database_query.schema_registry import SchemaRegistry

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # parse fallbacks are handled as rejections


class QueryRejectedError(Exception):
    """Raised with a short, caller-safe reason."""


def _node_types(*names: str) -> tuple[type[exp.Expression], ...]:
    """Resolve SQLGlot node classes by name, tolerating names absent in this SQLGlot version."""
    return tuple(t for name in names if isinstance(t := getattr(exp, name, None), type))


FORBIDDEN_NODES = _node_types(
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Drop",
    "Create",
    "Alter",
    "AlterTable",
    "Command",
    "Pragma",
    "Attach",
    "Detach",
    "Transaction",
    "Commit",
    "Rollback",
    "Set",
    "Use",
    "Into",
    "Lock",
    "Analyze",
    "Copy",
    "LoadData",
    "Kill",
    "Grant",
    "Revoke",
)

# Functions a reporting query legitimately needs. Anything else (including every unknown SQLite
# function) is rejected.
ALLOWED_FUNCTIONS = _node_types(
    # aggregates
    "Count",
    "Sum",
    "Avg",
    "Min",
    "Max",
    "GroupConcat",
    # scalar
    "Abs",
    "Round",
    "Coalesce",
    "Nullif",
    "If",
    "Case",
    "Cast",
    "TryCast",
    "Lower",
    "Upper",
    "Length",
    "Trim",
    "Substring",
    "Replace",
    "Concat",
    # date/time (SQLite date(), strftime(), current_*)
    "Date",
    "TimeToStr",
    "TsOrDsToTimestamp",
    "TsOrDsToDate",
    "StrToTime",
    "CurrentDate",
    "CurrentTimestamp",
    "CurrentTime",
    "DateDiff",
    "Extract",
    # window
    "RowNumber",
    "Rank",
    "DenseRank",
    # boolean connectors/operators that SQLGlot models as functions
    "And",
    "Or",
    "Not",
    "Xor",
)
ALLOWED_ANONYMOUS_FUNCTIONS = frozenset({"julianday", "datetime", "time"})
ROOT_TYPES = _node_types("Select", "SetOperation", "Union", "Intersect", "Except")
# Literals that must stay inline: ordinals and frame bounds change meaning when bound.
INLINE_LITERAL_PARENTS = _node_types(
    "Limit", "Offset", "Fetch", "Group", "Ordered", "WindowSpec", "DataType"
)


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    parameters: dict[str, object]
    tables: tuple[str, ...]
    max_rows: int


def _parse_single(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except ParseError as exc:
        raise QueryRejectedError("SQL could not be parsed") from exc
    if len(statements) != 1:
        raise QueryRejectedError("exactly one SQL statement is allowed")
    root = statements[0]
    if not isinstance(root, ROOT_TYPES):
        raise QueryRejectedError("only read-only SELECT queries are allowed")
    return root


def _check_nodes(root: exp.Expression, registry: SchemaRegistry) -> list[str]:
    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    tables: set[str] = set()
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise QueryRejectedError(f"{type(node).__name__.upper()} is not allowed")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise QueryRejectedError("recursive CTEs are not allowed")
        if isinstance(node, exp.Select) and (node.args.get("into") or node.args.get("locks")):
            raise QueryRejectedError("SELECT INTO and locking clauses are not allowed")
        if isinstance(node, exp.Anonymous):
            if str(node.name).lower() not in ALLOWED_ANONYMOUS_FUNCTIONS:
                raise QueryRejectedError(f"function {str(node.name).lower()}() is not allowed")
        elif isinstance(node, exp.Func) and not isinstance(node, ALLOWED_FUNCTIONS):
            raise QueryRejectedError(f"function {node.sql_name().lower()}() is not allowed")
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier):
                raise QueryRejectedError("table-valued functions are not allowed")
            if node.args.get("db") or node.args.get("catalog"):
                raise QueryRejectedError("schema-qualified table names are not allowed")
            name = node.name.lower()
            if name in cte_names:
                continue
            if name not in registry.tables:
                raise QueryRejectedError(f"table {name!r} is not available")
            tables.add(name)
    return sorted(tables)


def _qualify(root: exp.Expression, registry: SchemaRegistry) -> exp.Expression:
    try:
        qualified = qualify(
            root,
            schema=registry.sqlglot_schema,
            dialect="sqlite",
            validate_qualify_columns=True,
            quote_identifiers=True,
        )
    except OptimizeError as exc:
        raise QueryRejectedError(f"query references an unavailable column: {exc}") from exc
    for select in qualified.find_all(exp.Select):
        if any(isinstance(e, exp.Star) for e in select.expressions):
            raise QueryRejectedError("could not expand SELECT * against the allowlist")
    return qualified


def _apply_limit(root: exp.Expression, max_rows: int) -> exp.Expression:
    """Keep a smaller caller LIMIT; otherwise cap at max_rows + 1 (the extra row only signals
    truncation and is never returned)."""
    limit = root.args.get("limit")
    if limit is not None:
        value = limit.expression
        if not (isinstance(value, exp.Literal) and not value.is_string and value.this.isdigit()):
            raise QueryRejectedError("LIMIT must be a non-negative integer literal")
        if int(value.this) <= max_rows:
            return root
    offset = root.args.get("offset")
    if offset is not None:
        value = offset.expression
        if not (isinstance(value, exp.Literal) and not value.is_string and value.this.isdigit()):
            raise QueryRejectedError("OFFSET must be a non-negative integer literal")
    return root.limit(max_rows + 1)  # type: ignore[attr-defined]


def _parameterize(root: exp.Expression) -> dict[str, object]:
    parameters: dict[str, object] = {}
    for literal in list(root.find_all(exp.Literal)):
        if isinstance(literal.parent, INLINE_LITERAL_PARENTS):
            continue
        name = f"p{len(parameters)}"
        if literal.is_string:
            value: object = literal.this
        else:
            text = literal.this
            value = int(text) if text.lstrip("-").isdigit() else float(text)
        parameters[name] = value
        literal.replace(exp.Placeholder(this=name))
    return parameters


def validate_sql(
    sql: str, registry: SchemaRegistry, *, max_rows: int, max_chars: int
) -> ValidatedQuery:
    if not sql or not sql.strip():
        raise QueryRejectedError("empty SQL")
    if len(sql) > max_chars:
        raise QueryRejectedError(f"SQL longer than {max_chars} characters")
    root = _parse_single(sql)
    tables = _check_nodes(root, registry)
    root = _qualify(root, registry)
    _check_nodes(root, registry)  # re-check after qualification rewrote the tree
    root = _apply_limit(root, max_rows)
    parameters = _parameterize(root)
    return ValidatedQuery(
        sql=root.sql(dialect="sqlite"),
        parameters=parameters,
        tables=tuple(tables),
        max_rows=max_rows,
    )
