"""The internet link: an HTTP adapter in front of the relay, and the client that dials out to it."""
import json
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, parse, request

from .connectivity import Connectivity, Mode
from .envelope import Envelope, EnvelopeError, Kind
from .relay import Conflict, NotFound, State

LINK = 'http'
MAX_WAIT_SECONDS = 30.0
MAX_REQUEST_BYTES = 8 * 1024 * 1024


class BadRequest(ValueError):
    pass


def _message_id(text):
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise BadRequest('message id must be hex') from exc
    if len(raw) != 16:
        raise BadRequest('message id must be 16 bytes')
    return raw


def _wait(query):
    try:
        return max(0.0, min(MAX_WAIT_SECONDS, float(query.get('wait', ['0'])[0])))
    except ValueError as exc:
        raise BadRequest('wait must be a number of seconds') from exc


def _handler(relay):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_GET(self):
            self._dispatch('GET')

        def do_POST(self):
            self._dispatch('POST')

        def _dispatch(self, method):
            url = parse.urlsplit(self.path)
            parts = [parse.unquote(part) for part in url.path.strip('/').split('/')]
            query = parse.parse_qs(url.query)
            try:
                status, payload = self._route(method, parts, query)
            except (BadRequest, EnvelopeError) as exc:
                status, payload = 400, {'error': str(exc)}
            except NotFound as exc:
                status, payload = 404, {'error': str(exc)}
            except Conflict as exc:
                status, payload = 409, {'error': str(exc)}
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self, method, parts, query):
            match method, parts:
                case 'GET', ['health']:
                    return 200, {'ok': True}
                case 'POST', ['v1', 'messages']:
                    state = relay.submit(Envelope.from_json(self._json()), LINK)
                    return 200, {'state': state.value}
                case 'GET', ['v1', 'messages', message_id]:
                    wait_for = State(query['until'][0]) if 'until' in query else None
                    return 200, relay.status(_message_id(message_id), wait_for=wait_for, wait=_wait(query))
                case 'GET', ['v1', 'inbox', recipient]:
                    try:
                        limit = max(1, min(100, int(query.get('limit', ['10'])[0])))
                    except ValueError as exc:
                        raise BadRequest('limit must be a whole number') from exc
                    claimed = relay.claim(recipient, LINK, limit=limit, wait=_wait(query))
                    return 200, {'messages': [envelope.to_json() for envelope in claimed]}
                case 'POST', ['v1', 'inbox', recipient, message_id, 'ack']:
                    state = relay.ack(recipient, _message_id(message_id), LINK)
                    return 200, {'state': state.value}
                case 'GET', ['v1', 'transmissions']:
                    return 200, {'transmissions': relay.transmissions()}
            raise NotFound('no such route')

        def _json(self):
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError as exc:
                raise BadRequest('Content-Length is required') from exc
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise BadRequest('request body is empty or too large')
            try:
                return json.loads(self.rfile.read(length))
            except ValueError as exc:
                raise BadRequest('request body must be JSON') from exc

    return Handler


class HttpLink:
    """Serves the relay over HTTP. Clients only ever dial out to it."""

    def __init__(self, relay, host='127.0.0.1', port=0):
        self._server = ThreadingHTTPServer((host, port), _handler(relay))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self):
        host, port = self._server.server_address[:2]
        return f'http://{host}:{port}'

    def start(self):
        self._thread.start()
        return self

    def serve_forever(self):
        self._server.serve_forever()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        if self._thread.is_alive():
            self._thread.join()


class PortalError(RuntimeError):
    """A request the portal answered with an error. Retrying the same request will not help."""


class PortalUnreachable(PortalError):
    """The portal could not be reached, or failed on its side. Retrying the same request may help."""


DEFAULT_RETRY_DELAYS = (0.5, 1.0, 2.0)
_REMEMBERED_ACKS = 10_000


class PortalClient:
    """A client that dials out to the portal.

    Sends and acknowledgements are retried with the same message id, so a retry never creates a
    second message. Messages this client already acknowledged are dropped if the portal hands them
    out again, which happens when an acknowledgement was lost on the way.
    """

    def __init__(self, base_url, name, *, timeout=10.0, retry_delays=DEFAULT_RETRY_DELAYS, sleep=time.sleep):
        self._base = base_url.rstrip('/')
        self.name = name
        self._timeout = timeout
        self._retry_delays = tuple(retry_delays)
        self._sleep = sleep
        self._acked = OrderedDict()
        self.connectivity = Connectivity(mode=Mode.ONLINE)

    @property
    def online(self):
        return self.connectivity.mode is Mode.ONLINE

    def send(self, recipient, body, kind=Kind.TEXT):
        return self.resend(Envelope.new(self.name, recipient, body, kind))

    def resend(self, envelope):
        """Send an envelope that may already have reached the portal. The portal keeps only one copy."""
        self._retrying('POST', '/v1/messages', envelope.to_json())
        return envelope

    def receive(self, *, wait=0.0, limit=10):
        path = f'/v1/inbox/{parse.quote(self.name, safe="")}?wait={wait}&limit={limit}'
        data = self._call('GET', path, extra_timeout=wait)
        fresh = []
        for item in data['messages']:
            envelope = Envelope.from_json(item)
            if envelope.id in self._acked:
                self._ack_remote(envelope.id)
            else:
                fresh.append(envelope)
        return fresh

    def ack(self, envelope):
        self._acked[envelope.id] = None
        while len(self._acked) > _REMEMBERED_ACKS:
            self._acked.popitem(last=False)
        return self._ack_remote(envelope.id)

    def status(self, message_id, *, until=None, wait=0.0):
        path = f'/v1/messages/{message_id}?wait={wait}'
        if until is not None:
            path += f'&until={until.value}'
        return self._call('GET', path, extra_timeout=wait)

    def transmissions(self):
        return self._call('GET', '/v1/transmissions')['transmissions']

    def _ack_remote(self, message_id):
        path = f'/v1/inbox/{parse.quote(self.name, safe="")}/{message_id}/ack'
        return self._retrying('POST', path, {})['state']

    def _retrying(self, method, path, payload):
        for delay in self._retry_delays:
            try:
                return self._call(method, path, payload)
            except PortalUnreachable:
                self._sleep(delay)
        return self._call(method, path, payload)

    def _call(self, method, path, payload=None, *, extra_timeout=0.0):
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        req = request.Request(self._base + path, data=data, method=method,
                              headers={'Content-Type': 'application/json'})
        try:
            with request.urlopen(req, timeout=self._timeout + extra_timeout) as response:
                result = json.loads(response.read())
        except error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')
            reachable = exc.code < 500
            self.connectivity = self.connectivity.observe(reachable)
            kind = PortalError if reachable else PortalUnreachable
            raise kind(f'{method} {path} failed with {exc.code}: {detail}') from exc
        except (error.URLError, OSError) as exc:
            self.connectivity = self.connectivity.observe(False)
            reason = getattr(exc, 'reason', exc)
            raise PortalUnreachable(f'cannot reach the portal at {self._base}: {reason}') from exc
        self.connectivity = self.connectivity.observe(True)
        return result
