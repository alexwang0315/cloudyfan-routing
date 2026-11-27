"""The internet link: an HTTP adapter in front of the relay, and the client that dials out to it."""
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, parse, request

from .connectivity import Connectivity, Mode
from .envelope import Envelope, EnvelopeError, Kind
from .relay import Conflict, NotFound, State
from .transfer import CHUNK_MAX, DEFAULT_CHUNK, ChunkUnavailable, Manifest, StorageFull, TransferError

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


def _chunk_index(text):
    try:
        return int(text)
    except ValueError as exc:
        raise BadRequest('chunk index must be a whole number') from exc


def _handler(relay, transfers):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_GET(self):
            self._dispatch('GET')

        def do_POST(self):
            self._dispatch('POST')

        def do_PUT(self):
            self._dispatch('PUT')

        def _dispatch(self, method):
            url = parse.urlsplit(self.path)
            parts = [parse.unquote(part) for part in url.path.strip('/').split('/')]
            query = parse.parse_qs(url.query)
            try:
                status, payload = self._route(method, parts, query)
            except (BadRequest, EnvelopeError, TransferError) as exc:
                status, payload = (507, 'storage_full') if isinstance(exc, StorageFull) else (400, 'bad_request')
                payload = {'error': str(exc), 'code': payload}
            except ChunkUnavailable as exc:
                status, payload = 409, {'error': str(exc), 'code': 'chunk_unavailable'}
            except NotFound as exc:
                status, payload = 404, {'error': str(exc), 'code': 'not_found'}
            except Conflict as exc:
                status, payload = 409, {'error': str(exc), 'code': 'conflict'}
            if isinstance(payload, bytes):
                body, content_type = payload, 'application/octet-stream'
            else:
                body, content_type = json.dumps(payload, ensure_ascii=False).encode('utf-8'), \
                    'application/json; charset=utf-8'
            self.send_response(status)
            self.send_header('Content-Type', content_type)
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
                case 'GET', ['v1', 'clients']:
                    return 200, {'clients': relay.clients()}
                case 'GET', ['v1', 'transmissions']:
                    return 200, {'transmissions': relay.transmissions(),
                                 'transfers': transfers.list() if transfers else []}
                case 'POST', ['v1', 'transfers'] if transfers:
                    return 200, transfers.create(Manifest.from_json(self._json()), LINK)
                case 'GET', ['v1', 'transfers', transfer_id] if transfers:
                    return 200, transfers.record(_message_id(transfer_id))
                case 'PUT', ['v1', 'transfers', transfer_id, 'chunks', index] if transfers:
                    data = self._raw(CHUNK_MAX)
                    return 200, transfers.put_chunk(_message_id(transfer_id), _chunk_index(index), data, LINK)
                case 'GET', ['v1', 'transfers', transfer_id, 'chunks', index] if transfers:
                    return 200, transfers.get_chunk(_message_id(transfer_id), _chunk_index(index))
            raise NotFound('no such route')

        def _raw(self, limit):
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError as exc:
                raise BadRequest('Content-Length is required') from exc
            if not 0 < length <= limit:
                raise BadRequest(f'request body must be 1..{limit} bytes')
            return self.rfile.read(length)

        def _json(self):
            try:
                return json.loads(self._raw(MAX_REQUEST_BYTES))
            except ValueError as exc:
                raise BadRequest('request body must be JSON') from exc

    return Handler


class HttpLink:
    """Serves the relay over HTTP. Clients only ever dial out to it."""

    def __init__(self, relay, host='127.0.0.1', port=0, *, transfers=None):
        self._server = ThreadingHTTPServer((host, port), _handler(relay, transfers))
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


class StorageFullError(PortalError):
    """The portal has no spool space left for this file."""


class ChunkUnavailableError(PortalError):
    """A chunk is not on the portal right now; the sender has to upload it again."""


class FileCorrupted(PortalError):
    """A downloaded file kept failing its hash check."""


_ERROR_CODES = {'storage_full': StorageFullError, 'chunk_unavailable': ChunkUnavailableError}


DEFAULT_RETRY_DELAYS = (0.5, 1.0, 2.0)
log = logging.getLogger(__name__)
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
        self.keep_receipts = False

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
            if envelope.kind is Kind.RECEIPT and not self.keep_receipts:
                self.ack(envelope)
            elif envelope.id in self._acked:
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

    def clients(self):
        return self._call('GET', '/v1/clients')['clients']

    def transfers(self):
        return self._call('GET', '/v1/transmissions')['transfers']

    def send_file(self, recipient, path, *, chunk_size=DEFAULT_CHUNK, on_chunk=None):
        manifest = Manifest.from_file(path, self.name, recipient, chunk_size=chunk_size)
        self.upload(manifest, path, on_chunk=on_chunk)
        return manifest

    def upload(self, manifest, path, *, on_chunk=None):
        """Upload whatever chunks the portal is still missing. Safe to call again after an interruption."""
        with open(path, 'rb') as source:
            def read_chunk(index):
                source.seek(index * manifest.chunk_size)
                return source.read(manifest.chunk_length(index))

            return self.upload_chunks(manifest, read_chunk, on_chunk=on_chunk)

    def upload_chunks(self, manifest, read_chunk, *, on_chunk=None):
        """Like `upload`, with chunks coming from `read_chunk(index)` instead of a file."""
        record = self._retrying('POST', '/v1/transfers', manifest.to_json())
        for index in record['missing']:
            record = self._retrying('PUT', f'/v1/transfers/{manifest.id}/chunks/{index}', body=read_chunk(index))
            if on_chunk:
                on_chunk(index)
        return record

    def transfer(self, transfer_id):
        return self._call('GET', f'/v1/transfers/{transfer_id}')

    def fetch(self, envelope, destination, *, attempts=3):
        """Download a file transfer to `destination`, checking every chunk and the whole file."""
        manifest = Manifest.from_envelope(envelope)
        destination = Path(destination)
        partial = destination.with_name(destination.name + '.part')
        whole = hashlib.sha256()
        try:
            with open(partial, 'wb') as target:
                for index in range(manifest.chunk_count):
                    for _ in range(attempts):
                        data = self._download_chunk(manifest.id, index)
                        if manifest.check_chunk(index, data):
                            break
                    else:
                        raise FileCorrupted(f'chunk {index} failed its hash check {attempts} times')
                    target.write(data)
                    whole.update(data)
            if whole.hexdigest() != manifest.sha256:
                raise FileCorrupted('the reassembled file does not match its sha256')
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)
        return manifest

    def listen(self, handler, *, stop=None, until=None, wait=20.0, backoff=(0.5, 1.0, 2.0, 5.0, 10.0)):
        """Hand every incoming message to `handler` until `stop` is set, reconnecting on its own.

        `until` is a `time.monotonic()` deadline; no long poll runs past it.

        A message is acknowledged only after `handler` returns. If `handler` raises, the message is
        left unacknowledged and the portal hands it out again after its ack timeout.
        """
        stop = stop or threading.Event()
        failures = 0
        while not stop.is_set():
            remaining = None if until is None else until - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            try:
                batch = self.receive(wait=wait if remaining is None else min(wait, remaining))
                failures = 0
            except PortalUnreachable as error:
                delay = backoff[min(failures, len(backoff) - 1)]
                log.warning('portal unreachable, retrying in %.1fs: %s', delay, error)
                stop.wait(delay if remaining is None else min(delay, remaining))
                failures += 1
                continue
            for envelope in batch:
                if stop.is_set():
                    return
                try:
                    handler(envelope)
                except Exception:
                    log.exception('handler failed for message %s; it will be handed out again', envelope.id)
                    continue
                try:
                    self.ack(envelope)
                except PortalUnreachable as error:
                    log.warning('receipt for %s not delivered yet, will resend it: %s', envelope.id, error)

    def download_chunk(self, transfer_id, index):
        return self._download_chunk(transfer_id, index)

    def _download_chunk(self, transfer_id, index):
        return self._retrying('GET', f'/v1/transfers/{transfer_id}/chunks/{index}', raw=True)

    def _ack_remote(self, message_id):
        path = f'/v1/inbox/{parse.quote(self.name, safe="")}/{message_id}/ack'
        return self._retrying('POST', path, {})['state']

    def _retrying(self, method, path, payload=None, **options):
        for delay in self._retry_delays:
            try:
                return self._call(method, path, payload, **options)
            except PortalUnreachable:
                self._sleep(delay)
        return self._call(method, path, payload, **options)

    def _call(self, method, path, payload=None, *, body=None, raw=False, extra_timeout=0.0):
        if payload is not None:
            body, content_type = json.dumps(payload).encode('utf-8'), 'application/json'
        else:
            content_type = 'application/octet-stream'
        req = request.Request(self._base + path, data=body, method=method, headers={'Content-Type': content_type})
        try:
            with request.urlopen(req, timeout=self._timeout + extra_timeout) as response:
                content = response.read()
                result = content if raw else json.loads(content)
        except error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')
            try:
                code = json.loads(detail).get('code')
            except ValueError:
                code = None
            server_side = exc.code >= 500 and code not in _ERROR_CODES
            self.connectivity = self.connectivity.observe(not server_side)
            kind = PortalUnreachable if server_side else _ERROR_CODES.get(code, PortalError)
            raise kind(f'{method} {path} failed with {exc.code}: {detail}') from exc
        except (error.URLError, OSError) as exc:
            self.connectivity = self.connectivity.observe(False)
            reason = getattr(exc, 'reason', exc)
            raise PortalUnreachable(f'cannot reach the portal at {self._base}: {reason}') from exc
        self.connectivity = self.connectivity.observe(True)
        return result
