"""Tool-free public SQL validation for a trusted finite contract, not a sandbox.

Only public sample inputs and expected rows are accepted. There is deliberately
no hidden-grader, arbitrary oracle, database path or model-provided tool API.
"""
from dataclasses import dataclass
import hashlib
import json

from .diagnostics import DiagnosticContract
from . import sql_executor as executor


VERSION = 'sql-environment-v1'
SQL_DIAGNOSTICS = DiagnosticContract(
    version='sql-public-diagnostics-v1', source=VERSION, allowed_fields=('checks',))
_MODEL_ERRORS = frozenset({
    'SQL_READ_ONLY_REQUIRED', 'SQL_RESULT_COLUMNS', 'SQL_RESULT_TYPES',
    'SQL_OUTPUT_LIMIT', 'SQL_RESOURCE_LIMIT', 'SQL_UNSAFE', 'SQL_EXECUTION_ERROR',
})
_PUBLIC_CATEGORIES = frozenset({
    'compound_select_limit', 'column_limit', 'expression_depth_limit',
    'function_argument_limit', 'sql_byte_limit', 'sqlite_value_length_limit',
    'pattern_length_limit', 'unknown_column', 'unknown_table', 'unknown_function',
    'ambiguous_column', 'window_placement', 'aggregate_placement', 'incomplete_sql',
    'multiple_statements', 'bound_parameters_forbidden', 'syntax_error',
    'vm_step_limit', 'output_row_limit', 'output_byte_limit',
})


class SqlValidationUnavailable(RuntimeError):
    """Engineering interruption; retained answers may be explicitly revalidated."""
    category = 'engineering'
    control_plane_error = True

    def __init__(self, code='SQL_VALIDATION_UNAVAILABLE'):
        self.code = code
        super().__init__('SQL validation environment unavailable; evidence retained')


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))


def _cell(value):
    if value is None:
        return True
    if type(value) is int:
        return -(2**63) <= value < 2**63
    return type(value) is str and len(value.encode('utf-8')) <= executor.MAX_OUTPUT_BYTES


def _validate_spec(spec):
    if (not isinstance(spec['version'], str) or not spec['version'].strip()
            or not isinstance(spec['prompt'], str) or not spec['prompt'].strip()
            or not isinstance(spec['tables'], dict) or not 1 <= len(spec['tables']) <= 32):
        raise ValueError('SQL contract requires version, public prompt and declared tables')
    executor.table_ddl(spec['tables'])
    columns = spec['expected_columns']
    if (not isinstance(columns, list) or not 1 <= len(columns) <= executor.MAX_SQL_COLUMNS
            or any(not isinstance(c, str) or not 1 <= len(c) <= 128 for c in columns)
            or len(set(columns)) != len(columns)):
        raise ValueError('SQL contract requires explicit result columns')
    samples = spec['samples']
    if not isinstance(samples, list) or len(samples) > 32:
        raise ValueError('SQL contract accepts at most 32 public samples')
    ids = set()
    for sample in samples:
        if (not isinstance(sample, dict) or set(sample) != {'id', 'tables', 'expected'}
                or not isinstance(sample['id'], str) or not 1 <= len(sample['id']) <= 80
                or sample['id'] in ids or not isinstance(sample['tables'], dict)
                or set(sample['tables']) != set(spec['tables'])):
            raise ValueError('SQL samples must contain only complete public inputs and expected rows')
        ids.add(sample['id'])
        for table, rows in sample['tables'].items():
            fields = {c[0] for c in spec['tables'][table]}
            if (not isinstance(rows, list) or len(rows) > 1000
                    or any(not isinstance(row, dict) or set(row) != fields
                           or not all(_cell(v) for v in row.values()) for row in rows)):
                raise ValueError('SQL public input rows must match the bounded schema')
        expected = sample['expected']
        if (not isinstance(expected, list) or len(expected) > executor.MAX_OUTPUT_ROWS
                or any(not isinstance(row, list) or len(row) != len(columns)
                       or not all(_cell(v) for v in row) for row in expected)
                or sum(len(str(v).encode('utf-8')) for row in expected for v in row)
                   > executor.MAX_OUTPUT_BYTES):
            raise ValueError('SQL public expected rows must match result columns and bounds')


@dataclass(frozen=True, init=False)
class SqlContract:
    """Snapshot all public inputs; use run_sql to bind them to durable identity.

    Expected rows are public contract examples, not an assertion of general SQL
    correctness. Contract semantics and task coverage need independent review.
    """
    _public_json: str
    _runtime_json: str

    def __init__(self, *, version, prompt, tables, expected_columns, samples):
        try:
            encoded = _json(dict(version=version, prompt=prompt, tables=tables,
                                 expected_columns=expected_columns, samples=samples))
            if len(encoded.encode('utf-8')) > 1024 * 1024:
                raise ValueError('oversized public contract')
            _validate_spec(json.loads(encoded))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError('Invalid public SQL contract') from None
        object.__setattr__(self, '_public_json', encoded)
        object.__setattr__(self, '_runtime_json', _json(executor._execution_constraints()))

    @property
    def contract_version(self):
        value = VERSION + self._runtime_json + self._public_json
        return VERSION + ':' + hashlib.sha256(value.encode('utf-8')).hexdigest()

    def task(self):
        spec = json.loads(self._public_json)
        return {'prompt': spec['prompt'] + '\nReturn only one JSON object with a sql string field. '
                'No tools or external database are available. Public contract: ' + self._public_json
                + '\nExecution constraints: ' + self._runtime_json,
                'sql_contract_version': self.contract_version}

    def environment(self):
        return SqlEnvironment(self)


class SqlEnvironment:
    """Fresh in-memory databases; returns only bounded public diagnostic codes."""
    def __init__(self, contract):
        if not isinstance(contract, SqlContract):
            raise ValueError('SqlEnvironment requires a frozen SqlContract')
        self.contract = contract
        self.spec = json.loads(contract._public_json)
        try:
            self._check_runtime()
            # Trusted DDL/data and native limits must work before a model POST.
            for sample in self.spec['samples']:
                _, error, _ = executor._execute_fixture(
                    self.spec['tables'], ['preflight'], sample['tables'],
                    'SELECT 1 AS preflight', public=True)
                if error:
                    raise SqlValidationUnavailable('SQL_VALIDATION_PREFLIGHT')
        except SqlValidationUnavailable:
            raise
        except Exception:
            raise SqlValidationUnavailable('SQL_VALIDATION_PREFLIGHT') from None

    def _check_runtime(self):
        if _json(executor._execution_constraints()) != self.contract._runtime_json:
            raise SqlValidationUnavailable('SQL_VALIDATION_RUNTIME_CHANGED')

    def tools_schema(self):
        return []

    def call(self, name, arguments):
        raise SqlValidationUnavailable('SQL_TOOLS_UNSUPPORTED')

    def validate(self, final_text):
        try:
            return self._validate(final_text)
        except SqlValidationUnavailable:
            raise
        except Exception:
            raise SqlValidationUnavailable() from None

    def _validate(self, final_text):
        self._check_runtime()
        try:
            sql = executor.parse_sql_response(final_text)
        except executor.SqlResponseError as error:
            return {'passed': False, 'checks': [error.code]}
        if not self.spec['samples']:
            return {'passed': None, 'checks': ['SQL_NO_PUBLIC_SAMPLES']}
        checks = []
        for sample in self.spec['samples']:
            rows, error, detail = executor._execute_fixture(
                self.spec['tables'], self.spec['expected_columns'], sample['tables'], sql, public=True)
            if error in {'SQL_EXECUTION_CONTRACT', 'SQL_VALIDATION_ENVIRONMENT'}:
                raise SqlValidationUnavailable('SQL_VALIDATION_ENVIRONMENT')
            if detail.get('compiler', {}).get('category') == 'wall_time_limit':
                raise SqlValidationUnavailable('SQL_VALIDATION_TIMEOUT')
            if error:
                if error not in _MODEL_ERRORS:
                    raise SqlValidationUnavailable()
                category = detail.get('compiler', {}).get('category')
                checks.append(error + ':' + category if category in _PUBLIC_CATEGORIES else error)
            elif [list(row) for row in rows] != sample['expected']:
                checks.append('SQL_PUBLIC_RESULT_MISMATCH')
        return {'passed': not checks, 'checks': sorted(set(checks))}


def run_sql(core, run_id, contract, *, mode='cloudyfan'):
    """Core.run convenience binding; constructing this helper grants no spend authority."""
    if core.diagnostic_contract != SQL_DIAGNOSTICS:
        raise ValueError('SQL requires its versioned diagnostic contract')
    return core.run(run_id, contract.task(), contract.environment,
                    contract_version=contract.contract_version, mode=mode)
