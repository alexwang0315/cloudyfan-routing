"""Explicit public diagnostic schema; provenance is asserted by the application.

A structural allowlist cannot detect secret facts embedded in permitted prose.
Only a trusted, public-contract validator may supply values to this boundary.
"""
from dataclasses import asdict, dataclass
import json

from . import routing


@dataclass(frozen=True)
class DiagnosticContract:
    version: str = 'public-diagnostics-v1'
    source: str = 'injected-public-validator'
    allowed_fields: tuple[str, ...] = ('checks',)
    max_bytes: int = 4096

    def __post_init__(self):
        if (not isinstance(self.version, str) or not self.version.strip()
                or not isinstance(self.source, str) or not self.source.strip()
                or type(self.max_bytes) is not int or self.max_bytes < 1
                or not isinstance(self.allowed_fields, tuple)
                or any(not isinstance(f, str) or not f.strip() for f in self.allowed_fields)
                or len(set(self.allowed_fields)) != len(self.allowed_fields)
                or set(self.allowed_fields) & {'passed', 'diagnostic_version', 'diagnostic_source'}):
            raise ValueError('Diagnostic contract requires version, source, fields and positive byte limit')
        routing.project_public({'task': 'validation', 'public_diagnostics':
                                dict.fromkeys(self.allowed_fields)})

    def manifest(self):
        return asdict(self)

    def project(self, value):
        if not isinstance(value, dict) or 'passed' not in value or (
                value['passed'] is not None and type(value['passed']) is not bool):
            raise ValueError('Validator must explicitly return passed=True, False or None')
        routing.project_public({'task': 'validation', 'public_diagnostics': value})
        if set(value) - {'passed', *self.allowed_fields}:
            raise ValueError('Unknown public diagnostic field')
        for field, item in value.items():
            values = item if isinstance(item, list) else [item]
            if any(type(v) not in (str, int, float, bool, type(None)) for v in values):
                raise ValueError('Public diagnostic values must be scalars or flat scalar lists')
        public = {**value, 'diagnostic_version': self.version, 'diagnostic_source': self.source}
        encoded = json.dumps(public, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > self.max_bytes:
            raise ValueError('Public diagnostic exceeds byte limit')
        return public
