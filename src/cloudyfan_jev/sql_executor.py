"""Data-free SQL executor extracted from the historical workspace harness.

No task datasets, hidden fixtures, or oracles are bundled. Internal API; public
integration lives in sql.py. Cooperative SQLite controls are not an OS sandbox.
"""
import json
import re
import sqlite3
import time
from contextlib import closing
from functools import lru_cache

SQL_EXECUTION_CONTRACT_VERSION = "sqlite-readonly-contract-v2"

MAX_SQL_CHARS = 20000

MAX_RESPONSE_BYTES = 32768

MAX_OUTPUT_ROWS = 100

MAX_OUTPUT_BYTES = 32768

MAX_SQL_COLUMNS = 32

MAX_EXPRESSION_DEPTH = 100

MAX_COMPOUND_SELECT_TERMS = 20

MAX_COMPILED_VM_OPERATIONS = 25000

MAX_FUNCTION_ARGUMENTS = 16

MAX_ATTACHED_DATABASES = 0

MAX_SQLITE_WORKER_THREADS = 0

MAX_VM_STEPS = 100000

PROGRESS_CHECK_VM_STEPS = 500

MAX_QUERY_SECONDS = 0.25

MAX_PUBLIC_FEEDBACK_BYTES = 4096

SQL_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string", "minLength": 1, "maxLength": MAX_SQL_CHARS}},
    "required": ["sql"],
    "additionalProperties": False,
}

_FUNCTIONS = frozenset({
    "abs", "coalesce", "count", "date", "dense_rank", "first_value", "glob", "ifnull", "lag",
    "last_value", "lead", "length", "like", "lower", "max", "min", "nth_value", "nullif",
    "rank", "row_number", "strftime", "substr", "substring", "sum", "trim", "upper",
})

def table_ddl(tables):
    """Render trusted, frozen table specifications, never model-provided DDL."""
    for table, columns in tables.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", table):
            raise ValueError("invalid trusted table name")
        if (not columns or len(columns) > MAX_SQL_COLUMNS
                or len({name for name, _ in columns}) != len(columns)):
            raise ValueError("invalid trusted table columns")
        for name, kind in columns:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) or kind not in {
                "INTEGER", "INTEGER NOT NULL", "INTEGER PRIMARY KEY", "TEXT", "TEXT NOT NULL",
                "TEXT PRIMARY KEY",
            }:
                raise ValueError("invalid trusted column definition")
    return [f"CREATE TABLE {table} (" + ", ".join(f"{name} {kind}" for name, kind in columns) + ")"
            for table, columns in tables.items()]

def _sqlite_runtime_limits():
    """One source for both the public contract and every applied SQLite setlimit."""
    return {
        "SQLITE_LIMIT_LENGTH": MAX_OUTPUT_BYTES,
        "SQLITE_LIMIT_SQL_LENGTH": MAX_RESPONSE_BYTES,
        "SQLITE_LIMIT_COLUMN": MAX_SQL_COLUMNS,
        "SQLITE_LIMIT_EXPR_DEPTH": MAX_EXPRESSION_DEPTH,
        "SQLITE_LIMIT_COMPOUND_SELECT": MAX_COMPOUND_SELECT_TERMS,
        "SQLITE_LIMIT_VDBE_OP": MAX_COMPILED_VM_OPERATIONS,
        "SQLITE_LIMIT_FUNCTION_ARG": MAX_FUNCTION_ARGUMENTS,
        "SQLITE_LIMIT_ATTACHED": MAX_ATTACHED_DATABASES,
        "SQLITE_LIMIT_WORKER_THREADS": MAX_SQLITE_WORKER_THREADS,
    }

@lru_cache(maxsize=1)
def _native_sqlite_limits():
    """Snapshot unmodified host defaults; tuples keep the cached value immutable."""
    with closing(sqlite3.connect(":memory:")) as connection:
        return tuple((name, connection.getlimit(getattr(sqlite3, name))) for name in (
            "SQLITE_LIMIT_LIKE_PATTERN_LENGTH", "SQLITE_LIMIT_VARIABLE_NUMBER",
            "SQLITE_LIMIT_TRIGGER_DEPTH",
        ))

class _ExecutionContractUnavailable(RuntimeError):
    """The local SQLite build cannot enforce the published limits exactly."""

def _apply_sqlite_limits(connection):
    for name, bound in _sqlite_runtime_limits().items():
        kind = getattr(sqlite3, name)
        connection.setlimit(kind, bound)
        # SQLite can silently clamp a requested limit to its compiled-in maximum.
        # Do not execute and blame a model for a contract this host cannot honor.
        if connection.getlimit(kind) != bound:
            raise _ExecutionContractUnavailable
    for name, bound in _native_sqlite_limits():
        if connection.getlimit(getattr(sqlite3, name)) != bound:
            raise _ExecutionContractUnavailable

def _execution_constraints():
    """Describe the existing executor exactly; this does not expand its permissions."""
    return {
        "contract_version": SQL_EXECUTION_CONTRACT_VERSION,
        "sqlite_version": sqlite3.sqlite_version,
        "read_only": True,
        "single_statement": True,
        "statement_must_start_with": ["SELECT", "WITH"],
        "leading_sql_comments_allowed": False,
        "bound_parameters_allowed": False,
        "recursive_cte_allowed": False,
        "cast_allowed": True,
        "supported_syntax": [
            "nonrecursive CTE", "joins", "subqueries", "GROUP BY", "HAVING", "CASE", "CAST",
            "window functions from allowed_functions", "UNION", "UNION ALL", "ORDER BY", "LIMIT",
            "LIKE", "NOT LIKE", "GLOB", "NOT GLOB",
        ],
        "allowed_functions": sorted(_FUNCTIONS),
        "function_policy": (
            "Only listed functions are enabled; unlisted built-ins are forbidden. "
            "SQLite implements LIKE and GLOB operators using the listed like/glob functions."
        ),
        "forbidden_operations": [
            "writes", "DDL", "ATTACH", "DETACH", "PRAGMA", "recursive CTE",
            "system-table reads", "unlisted-table reads", "extension loading", "file functions",
        ],
        "derived_relation_read_caveat": (
            "The read authorizer permits declared base tables only. Some plans for constant "
            "CTEs or derived relations with unused columns, including COUNT(*) over their "
            "joins, emit reads of virtual relation names and are denied. Explicitly consume "
            "the needed columns from each derived relation."
        ),
        "result_cell_types": ["integer", "text", "null"],
        "integer_ratio_rule": (
            "For floor of a nonnegative ratio, divide an integer numerator by an integer "
            "denominator. CAST(expression AS INTEGER) is allowed. Functions floor(), round(), "
            "and julianday() are NOT enabled; floating-point result cells are rejected."
        ),
        "max_sql_characters": MAX_SQL_CHARS,
        "max_sql_utf8_bytes": MAX_RESPONSE_BYTES,
        "max_response_utf8_bytes": MAX_RESPONSE_BYTES,
        "sqlite_runtime_limits": _sqlite_runtime_limits(),
        "sqlite_native_limits": dict(_native_sqlite_limits()),
        "native_limit_policy": (
            "Native values are read from this SQLite build and are not changed by the harness. "
            "LIKE/GLOB patterns use SQLITE_LIMIT_LIKE_PATTERN_LENGTH UTF-8 bytes; bound "
            "parameters and triggers remain forbidden. SQLite also imposes build-specific "
            "parser, join and allocation constraints; this list is not exhaustive."
        ),
        "max_columns": MAX_SQL_COLUMNS,
        "max_expression_depth": MAX_EXPRESSION_DEPTH,
        "max_compound_select_terms": MAX_COMPOUND_SELECT_TERMS,
        "max_compiled_vm_operations": MAX_COMPILED_VM_OPERATIONS,
        "max_function_arguments": MAX_FUNCTION_ARGUMENTS,
        "compound_select_rule": (
            "The term limit applies separately to each compound SELECT, including UNION, "
            "UNION ALL, INTERSECT and EXCEPT. Each SELECT arm counts as one term."
        ),
        "max_output_rows": MAX_OUTPUT_ROWS,
        "max_output_bytes": MAX_OUTPUT_BYTES,
        "output_byte_counting": (
            "Sum UTF-8 bytes of str(cell) over all returned cells, including 'None' for NULL. "
            "SQLite also applies SQLITE_LIMIT_LENGTH to each string, blob and encoded row."
        ),
        "vm_step_budget_per_fixture": MAX_VM_STEPS,
        "progress_check_every_vm_steps": PROGRESS_CHECK_VM_STEPS,
        "wall_time_limit_seconds_per_fixture": MAX_QUERY_SECONDS,
        "resource_check_rule": (
            "The deadline starts before fixture database setup. At each progress check, "
            "abort when counted VM steps exceed the budget or the deadline has elapsed; "
            "also check the deadline after fetching. These cooperative checks may overshoot "
            "between callbacks and do not preempt every preparation or function operation."
        ),
        "max_public_feedback_utf8_bytes": MAX_PUBLIC_FEEDBACK_BYTES,
    }

class SqlResponseError(ValueError):
    def __init__(self, code, message):
        self.code = code
        self.message = message[:160]
        super().__init__(self.message)

def _sql_input(sql):
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL_CHARS or "\x00" in sql:
        raise SqlResponseError(
            "SQL_RESPONSE_SCHEMA",
            f"sql must be nonempty, contain no NUL and use at most {MAX_SQL_CHARS} characters.",
        )
    try:
        size = len(sql.encode("utf-8"))
    except UnicodeEncodeError:
        raise SqlResponseError("SQL_RESPONSE_SCHEMA", "sql must contain valid Unicode.") from None
    if size > MAX_RESPONSE_BYTES:
        raise SqlResponseError(
            "SQL_RESPONSE_LIMIT", f"SQL exceeds the {MAX_RESPONSE_BYTES} UTF-8 byte limit.")
    return sql.strip()

def parse_sql_response(text):
    """Parse exactly one JSON object without duplicate keys or echoed input errors."""
    try:
        size = len(text.encode("utf-8")) if isinstance(text, str) else MAX_RESPONSE_BYTES + 1
    except UnicodeEncodeError:
        raise SqlResponseError(
            "SQL_RESPONSE_JSON", "Response must contain valid Unicode.") from None
    if size > MAX_RESPONSE_BYTES:
        raise SqlResponseError(
            "SQL_RESPONSE_LIMIT",
            f"Response must be text using at most {MAX_RESPONSE_BYTES} UTF-8 bytes.",
        )

    def pairs(values):
        output = {}
        for key, value in values:
            if key in output:
                raise ValueError("duplicate key")
            output[key] = value
        return output

    def constant(_):
        raise ValueError("nonfinite value")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        raise SqlResponseError(
            "SQL_RESPONSE_JSON", "Response must be one valid JSON object.") from None
    if not isinstance(value, dict) or set(value) != {"sql"}:
        raise SqlResponseError(
            "SQL_RESPONSE_SCHEMA", "Response requires only the sql string field.")
    return _sql_input(value["sql"])

def _resource_feedback(category, constraint, limit, message):
    return {"category": category, "constraint": constraint, "limit": limit, "message": message}

def _sqlite_resource_feedback(error):
    """Match known engine errors but never copy an error, SQL fragment or identifier."""
    message = str(error).lower()
    categories = [
        ("too many terms in compound select", "compound_select_limit",
         "max_compound_select_terms", MAX_COMPOUND_SELECT_TERMS,
         "Shorten each UNION/UNION ALL/INTERSECT/EXCEPT chain or compose smaller CTEs."),
        ("too many columns", "column_limit", "max_columns", MAX_SQL_COLUMNS,
         "Reduce projected columns and GROUP BY/ORDER BY terms; avoid unnecessary SELECT *."),
        ("expression tree is too large", "expression_depth_limit", "max_expression_depth",
         MAX_EXPRESSION_DEPTH, "Simplify nested expressions and factor them into smaller CTEs."),
        ("too many arguments on function", "function_argument_limit", "max_function_arguments",
         MAX_FUNCTION_ARGUMENTS, "Reduce the arguments supplied to each function call."),
        ("query string is too large", "sql_byte_limit", "max_sql_utf8_bytes", MAX_RESPONSE_BYTES,
         "Shorten the SQL statement and remove unnecessary repetition."),
        ("string or blob too big", "sqlite_value_length_limit", "SQLITE_LIMIT_LENGTH",
         MAX_OUTPUT_BYTES, "Reduce intermediate text lengths and encoded row sizes."),
    ]
    for prefix, category, constraint, bound, hint in categories:
        if message.startswith(prefix):
            return _resource_feedback(category, constraint, bound, f"Limit: {bound}. {hint}")
    if message.startswith("like or glob pattern too complex"):
        bound = dict(_native_sqlite_limits())["SQLITE_LIMIT_LIKE_PATTERN_LENGTH"]
        return _resource_feedback(
            "pattern_length_limit", "SQLITE_LIMIT_LIKE_PATTERN_LENGTH", bound,
            f"LIKE/GLOB patterns may use at most {bound} UTF-8 bytes; shorten the pattern.",
        )
    if isinstance(error, MemoryError):
        return _resource_feedback(
            "statement_memory_limit", "max_compiled_vm_operations", MAX_COMPILED_VM_OPERATIONS,
            f"SQLite could not allocate statement resources. The compiled-program limit is "
            f"{MAX_COMPILED_VM_OPERATIONS} operations; simplify the query. This may also be a "
            "host allocation failure.",
        )
    return None

def _compiler_feedback(error):
    """Classify errors without retaining the exception's echoed query or identifiers."""
    message = str(error)
    categories = [
        ("no such column:", "unknown_column",
         "Use public-schema columns and columns projected by each CTE. Check alias scope; "
         "compute a derived alias in a prior CTE before referencing it."),
        ("no such table:", "unknown_table", "Use only tables from the public schema."),
        ("no such function:", "unknown_function", "Use only the published function allowlist."),
        ("ambiguous column name:", "ambiguous_column", "Qualify the column with its table alias."),
        ("misuse of window function", "window_placement", "Move the window result into a CTE."),
        ("misuse of aggregate", "aggregate_placement",
         "Do not nest aggregates at the same query level or place them in WHERE. Compute "
         "each aggregation in a separate CTE, then join or aggregate the resulting columns."),
        ("incomplete input", "incomplete_sql", "Complete the SQL statement and parentheses."),
        ("You can only execute one statement", "multiple_statements", "Return one SQL statement."),
        ("Incorrect number of bindings supplied", "bound_parameters_forbidden",
         "Bound parameters are not provided. Return one self-contained SQL statement."),
    ]
    for prefix, category, hint in categories:
        if message.startswith(prefix):
            return {"category": category, "message": hint}
    if ("syntax error" in message or re.fullmatch(
            r"\d+(?:st|nd|rd|th) ORDER BY term does not match any column in the result set", message)):
        return {"category": "syntax_error", "message": "Check the SQLite syntax and parentheses."}
    return {"category": "execution_error", "message": "Check the public SQL execution contract."}

def _execute_fixture(tables, expected_columns, data, sql, *, public=False):
    """A new isolated database per fixture; only public execution may return diagnostics."""
    if not re.match(r"\A(?:SELECT|WITH)\b", sql, flags=re.IGNORECASE):
        return None, "SQL_READ_ONLY_REQUIRED", {}
    columns = {name: {column for column, _ in entries}
               for name, entries in tables.items()}
    denied, limited, steps = False, None, 0
    prepared = False
    deadline = time.monotonic() + MAX_QUERY_SECONDS

    def authorize(action, first, second, database, _trigger):
        nonlocal denied
        allowed = action == sqlite3.SQLITE_SELECT
        if action == sqlite3.SQLITE_READ:
            # SQLite's COUNT(*) READ notification has an empty column and no database name.
            known_database = database == "main" or (database is None and second == "")
            allowed = known_database and first in columns and (
                second in columns[first] or second == "")
        elif action == sqlite3.SQLITE_FUNCTION:
            allowed = isinstance(second, str) and second.lower() in _FUNCTIONS
        if not allowed:
            denied = True
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

    def progress():
        nonlocal limited, steps
        steps += PROGRESS_CHECK_VM_STEPS
        if steps > MAX_VM_STEPS:
            limited = _resource_feedback(
                "vm_step_limit", "vm_step_budget_per_fixture", MAX_VM_STEPS,
                f"VM step budget: {MAX_VM_STEPS}. Reduce repeated scans and Cartesian joins.",
            )
        elif time.monotonic() > deadline:
            limited = _resource_feedback(
                "wall_time_limit", "wall_time_limit_seconds_per_fixture", MAX_QUERY_SECONDS,
                f"Fixture wall-time budget: {MAX_QUERY_SECONDS} seconds. Simplify the query.",
            )
        return int(limited is not None)

    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            try:
                connection.enable_load_extension(False)
                connection.execute("PRAGMA temp_store=MEMORY")
                connection.execute("PRAGMA trusted_schema=OFF")
                for statement in table_ddl(tables):
                    connection.execute(statement)
                for table, rows in data.items():
                    fields = [name for name, _ in tables[table]]
                    placeholders = ",".join("?" for _ in fields)
                    connection.executemany(f"INSERT INTO {table} VALUES ({placeholders})",
                                           [[row[field] for field in fields] for row in rows])
                connection.execute("PRAGMA query_only=ON")
                _apply_sqlite_limits(connection)
                connection.set_authorizer(authorize)
                connection.set_progress_handler(progress, PROGRESS_CHECK_VM_STEPS)
                prepared = True
                cursor = connection.execute(sql)
                if [item[0] for item in cursor.description or []] != expected_columns:
                    detail = ({"expected_columns": list(expected_columns),
                               "actual_column_count": len(cursor.description or [])}
                              if public else {})
                    return None, "SQL_RESULT_COLUMNS", detail
                output, byte_count = [], 0
                for row in cursor:
                    if len(output) >= MAX_OUTPUT_ROWS:
                        detail = {"compiler": _resource_feedback(
                            "output_row_limit", "max_output_rows", MAX_OUTPUT_ROWS,
                            f"Return at most {MAX_OUTPUT_ROWS} rows while preserving task "
                            "semantics.",
                        )} if public else {}
                        return None, "SQL_OUTPUT_LIMIT", detail
                    for value in row:
                        if value is not None and type(value) not in (int, str):
                            return None, "SQL_RESULT_TYPES", {}
                        byte_count += len(str(value).encode("utf-8"))
                    if byte_count > MAX_OUTPUT_BYTES:
                        detail = {"compiler": _resource_feedback(
                            "output_byte_limit", "max_output_bytes", MAX_OUTPUT_BYTES,
                            f"Returned cells exceed {MAX_OUTPUT_BYTES} UTF-8 bytes; "
                            "reduce unnecessary projected data.",
                        )} if public else {}
                        return None, "SQL_OUTPUT_LIMIT", detail
                    output.append(row)
                if time.monotonic() > deadline:
                    detail = {"compiler": _resource_feedback(
                        "wall_time_limit", "wall_time_limit_seconds_per_fixture", MAX_QUERY_SECONDS,
                        f"Fixture wall-time budget: {MAX_QUERY_SECONDS} seconds. "
                        "Simplify the query.",
                    )} if public else {}
                    return None, "SQL_RESOURCE_LIMIT", detail
                return output, None, {}
            finally:
                # Removing callbacks permits connection cleanup without authorizer interference.
                connection.set_authorizer(None)
                connection.set_progress_handler(None, 0)
    except _ExecutionContractUnavailable:
        detail = {"compiler": {
            "category": "execution_environment_unavailable",
            "message": "This SQLite build cannot honor the published limits; fix the harness.",
        }} if public else {}
        return None, "SQL_EXECUTION_CONTRACT", detail
    except (sqlite3.Error, MemoryError, OverflowError) as error:
        # Setup/storage/allocation failures are validator failures, not bad SQL.
        if not prepared or isinstance(error, (MemoryError, OverflowError)):
            return None, "SQL_VALIDATION_ENVIRONMENT", {}
        base = getattr(error, 'sqlite_errorcode', 0) & 255
        if base in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_NOMEM,
                    sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_FULL,
                    sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_NOTADB}:
            return None, "SQL_VALIDATION_ENVIRONMENT", {}
        resource = limited or _sqlite_resource_feedback(error)
        if not denied and not resource and _compiler_feedback(error)['category'] == 'execution_error':
            return None, "SQL_VALIDATION_ENVIRONMENT", {}
        code = ("SQL_UNSAFE" if denied else "SQL_RESOURCE_LIMIT" if resource
                else "SQL_EXECUTION_ERROR")
        detail = ({"compiler": resource or _compiler_feedback(error)}
                  if public and not denied else {})
        return None, code, detail
