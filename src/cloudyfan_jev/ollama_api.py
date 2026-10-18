"""Explicit Ollama text-protocol subset over registered CloudyFan tasks.

No Ollama runtime or SDK is started. This is a protocol-fixture target, not a
claim of compatibility with a named product client or a full Ollama server.
"""
from datetime import datetime, timezone
from http import HTTPStatus
import json

from .http_api import HTTPApp, ServiceError

PROTOCOL_VERSION = 'cloudyfan-ollama-text-v1'
DOCS_AS_OF = '2026-10-18'


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON keys')
        result[key] = value
    return result


def _payload(environ, *, chat=False):
    if environ.get('CONTENT_TYPE', '').split(';')[0] != 'application/json':
        raise ServiceError('json_required', 415)
    try:
        length = int(environ.get('CONTENT_LENGTH') or 0)
        if length < 1 or length > 65536:
            raise ServiceError('invalid_body_length', 413)
        payload = json.loads(environ['wsgi.input'].read(length), object_pairs_hook=_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid constant')))
    except (ValueError, UnicodeError):
        raise ServiceError('invalid_json') from None
    fields = {'model', 'messages' if chat else 'prompt', 'stream'}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ServiceError('unsupported_request_fields')
    if payload['stream'] is not False:
        raise ServiceError('only_explicit_nonstream_text_supported')
    if not isinstance(payload['model'], str):
        raise ServiceError('invalid_model')
    if chat:
        messages = payload['messages']
        if (not isinstance(messages, list) or len(messages) != 1 or not isinstance(messages[0], dict)
                or set(messages[0]) != {'role', 'content'} or messages[0]['role'] != 'user'
                or not isinstance(messages[0]['content'], str)):
            raise ServiceError('only_single_user_text_supported')
        return {'model': payload['model'], 'prompt': messages[0]['content'], 'stream': False}
    return payload


def _now():
    return datetime.now(timezone.utc).isoformat()


class OllamaApp:
    """Routing aliases map only to trusted, server-registered public contracts.

    Authentication and a caller-chosen stable X-CloudyFan-Run-ID are required.
    Model listing deliberately omits unobserved local weight metadata.
    """
    def __init__(self, service, *, models, authenticate, clock=_now, audit=None):
        self.service, self.models = service, dict(models)
        if not self.models or any(not isinstance(k, str) or not k or v not in service.contracts
                                  for k, v in self.models.items()):
            raise ValueError('Model aliases must map to registered validation contracts')
        self.authenticate, self.clock, self.audit = authenticate, clock, audit
        self.tasks = HTTPApp(service, authenticate=authenticate, audit=audit)

    def __call__(self, environ, start_response):
        if environ.get('PATH_INFO', '').startswith('/v1/tasks'):
            return self.tasks(environ, start_response)
        code = 200
        try:
            principal = self.authenticate(environ)
            self.service.authorize(principal)
            method, path = environ['REQUEST_METHOD'], environ.get('PATH_INFO', '')
            if method == 'GET' and path == '/api/tags':
                body = {'models': [{'name': name, 'model': name} for name in sorted(self.models)],
                        'cloudyfan': {'protocol_version': PROTOCOL_VERSION, 'model_kind': 'routing_alias'}}
            elif method == 'POST' and path in ('/api/generate', '/api/chat'):
                payload = _payload(environ, chat=path == '/api/chat')
                alias = payload['model']
                if alias not in self.models:
                    raise ServiceError('model_not_registered', 404)
                run_id = environ.get('HTTP_X_CLOUDYFAN_RUN_ID')
                state = self.service.create(principal, {'run_id': run_id, 'contract_id': self.models[alias],
                                                         'prompt': payload['prompt']})
                # Duplicate native requests are lookups, not implicit resume.
                if state['status'] == 'ready':
                    state = self.service.execute(principal, run_id)
                metadata = {'protocol_version': PROTOCOL_VERSION, 'run_id': run_id,
                            'status': state['status'], 'costs': state['costs']}
                if state['status'] in ('passed', 'failed', 'unverifiable'):
                    result = self.service.result(principal, run_id)
                    metadata['costs'] = result['costs']
                    metadata['provenance'] = result['provenance']
                    if result['status'] == 'passed' and result['final_public_pass'] is True:
                        body = {'model': alias, 'created_at': self.clock(), 'response': result['answer'],
                                'done': True, 'cloudyfan': metadata}
                        if path == '/api/chat':
                            body['message'] = {'role': 'assistant', 'content': body.pop('response')}
                    else:
                        code = 422
                        error = ('public_validation_failed' if result['status'] == 'failed'
                                 else 'public_validation_unavailable')
                        body = {'error': error, 'cloudyfan': metadata}
                else:
                    code = {'policy_denied': 403, 'accounting_interrupted': 409,
                            'engineering_interrupted': 503}.get(state['status'], 409)
                    body = {'error': state.get('error', {}).get('code', 'task_not_complete'), 'cloudyfan': metadata}
            else:
                raise ServiceError('endpoint_not_supported', 404)
        except ServiceError as error:
            code, body = error.status, {'error': error.code}
        except Exception:
            code, body = 500, {'error': 'service_interrupted'}
        if self.audit:
            try:
                self.audit({'event': 'ollama_protocol_request', 'http_status': code})
            except Exception:
                pass
        encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        start_response(f'{code} {HTTPStatus(code).phrase}', [('Content-Type', 'application/json'),
            ('Content-Length', str(len(encoded))), ('Cache-Control', 'no-store')])
        return [encoded]
