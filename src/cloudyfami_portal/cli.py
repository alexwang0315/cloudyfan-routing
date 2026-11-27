"""The `cloudyfami-portal` command: run a relay, or act as a client of one."""
import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path

from .envelope import Kind
from .http_link import HttpLink, PortalClient, PortalError
from .relay import DeliveryPolicy, Relay, State
from .transfer import DEFAULT_SPOOL_LIMIT, Manifest, Transfers

DEFAULT_CONFIG = Path('~/.config/cloudyfami-portal/client.toml').expanduser()


class SettingsError(SystemExit):
    pass


def client_settings(args):
    """Portal URL and client name: command line first, then environment, then the config file."""
    config_path = args.config or os.environ.get('CLOUDYFAMI_PORTAL_CONFIG')
    config = {}
    if config_path or DEFAULT_CONFIG.exists():
        path = Path(config_path).expanduser() if config_path else DEFAULT_CONFIG
        try:
            config = tomllib.loads(path.read_text(encoding='utf-8'))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise SettingsError(f'cannot read config {path}: {error}') from error
    url = args.url or os.environ.get('CLOUDYFAMI_PORTAL_URL') or config.get('url')
    name = args.name or os.environ.get('CLOUDYFAMI_PORTAL_NAME') or config.get('name')
    if not url or not name:
        raise SettingsError('set the portal URL and this client\'s name with --url/--name, '
                            'CLOUDYFAMI_PORTAL_URL/CLOUDYFAMI_PORTAL_NAME, or a config file')
    return url, name


def _client(args):
    url, name = client_settings(args)
    return PortalClient(url, name)


def _print(args, record, text):
    print(json.dumps(record, ensure_ascii=False) if args.json else text, flush=True)


def _wait_for_receipt(client, args, message_id):
    if args.wait_ack <= 0:
        return None
    return client.status(message_id, until=State.ACKED, wait=args.wait_ack)['state']


def cmd_send(args):
    client = _client(args)
    kind = Kind.SIGNAL if args.signal else Kind.TEXT
    text = args.text if args.text is not None else sys.stdin.read()
    sent = client.send(args.recipient, text, kind)
    state = _wait_for_receipt(client, args, sent.id) or 'queued'
    _print(args, {'id': sent.id, 'recipient': args.recipient, 'size': sent.size, 'state': state},
           f'{sent.id} → {args.recipient}（{sent.size} 位元組）：{state}')
    return 0 if args.wait_ack <= 0 or state == 'acked' else 1


def cmd_send_file(args):
    client = _client(args)
    manifest = client.send_file(args.recipient, args.path, chunk_size=args.chunk_size)
    state = _wait_for_receipt(client, args, manifest.id) or 'queued'
    _print(args, {'id': manifest.id, 'recipient': args.recipient, 'name': manifest.name, 'size': manifest.size,
                  'sha256': manifest.sha256, 'state': state},
           f'{manifest.id} → {args.recipient}：{manifest.name}（{manifest.size} 位元組，{manifest.chunk_count} 塊）：{state}')
    return 0 if args.wait_ack <= 0 or state == 'acked' else 1


def _save_path(folder, manifest):
    target = folder / (manifest.name or manifest.id)
    if target.exists():
        target = target.with_name(f'{target.stem}-{manifest.id[:8]}{target.suffix}')
    return target


def cmd_listen(args):
    client = _client(args)
    save_dir = Path(args.save_dir).expanduser()
    stop = threading.Event()
    handled = []

    def handle(envelope):
        record = {'id': envelope.id, 'sender': envelope.sender, 'kind': envelope.kind.name.lower(),
                  'size': envelope.size, 'created_at': envelope.created_at}
        stdin = envelope.body
        if envelope.kind is Kind.TRANSFER:
            manifest = Manifest.from_envelope(envelope)
            save_dir.mkdir(parents=True, exist_ok=True)
            target = _save_path(save_dir, manifest)
            client.fetch(envelope, target)
            record.update(file=str(target), name=manifest.name, size=manifest.size, sha256=manifest.sha256)
            stdin = b''
            shown = f'檔案 {target}（{manifest.size} 位元組）'
        else:
            text = envelope.body.decode('utf-8', 'replace')
            record['text'] = text
            shown = text
        if args.exec:
            env = {**os.environ, 'CLOUDYFAMI_MESSAGE_ID': envelope.id, 'CLOUDYFAMI_SENDER': envelope.sender,
                   'CLOUDYFAMI_KIND': record['kind'], 'CLOUDYFAMI_FILE': record.get('file', '')}
            result = subprocess.run(args.exec, shell=True, input=stdin, env=env, check=False)
            if result.returncode != 0:
                raise RuntimeError(f'handler exited with {result.returncode}')
        stamp = time.strftime('%H:%M:%S', time.localtime(envelope.created_at))
        _print(args, record, f'{stamp} {envelope.sender} [{record["kind"]}] {envelope.id[:8]} {shown}')
        handled.append(envelope.id)
        if args.count and len(handled) >= args.count:
            stop.set()

    until = time.monotonic() + args.timeout if args.timeout else None
    try:
        client.listen(handle, stop=stop, until=until, wait=args.wait)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0 if not args.count or len(handled) >= args.count else 1


def cmd_status(args):
    record = _client(args).status(args.message_id)
    if args.json:
        _print(args, record, '')
    else:
        print(f'{record["id"]} {record["sender"]}→{record["recipient"]} {record["size"]}B {record["state"]}'
              f'（投遞 {record["attempts"]} 次）')
        for step in record['history']:
            stamp = time.strftime('%H:%M:%S', time.localtime(step['at']))
            print(f'  {stamp} {step["state"]} [{step["link"]}]')
    return 0


def cmd_log(args):
    client = _client(args)
    items, transfers = client.transmissions(), client.transfers()
    if args.json:
        _print(args, {'transmissions': items, 'transfers': transfers}, '')
        return 0
    for item in items:
        stamp = time.strftime('%H:%M:%S', time.localtime(item['at']))
        print(f'{stamp} {item["id"][:8]} {item["sender"]}→{item["recipient"]} {item["kind"]} {item["size"]}B '
              f'{item["from"] or "-"}→{item["to"]} [{item["link"]}]')
    for item in transfers:
        reason = f'：{item["reason"]}' if item['reason'] else ''
        print(f'檔案 {item["id"][:8]} {item["size"]}B {item["chunks"]} 塊 {item["state"]}{reason}'
              f'（上傳 {item["uploads"]}、下載 {item["downloads"]}、重傳 {item["retransmissions"]}）')
    return 0


def cmd_demo(args):
    with tempfile.TemporaryDirectory() as folder:
        relay = Relay(os.path.join(folder, 'portal.sqlite3'))
        link = HttpLink(relay).start()
        try:
            alice, bob = PortalClient(link.url, '甲'), PortalClient(link.url, '乙')
            sent = alice.send('乙', '你好，這是經平台轉送的測試訊息')
            print(f'甲 送出 {sent.id}（{sent.size} 位元組，sha256 {sent.sha256[:12]}…）')
            for received in bob.receive(wait=2):
                print(f'乙 收到 {received.id}：{received.body.decode("utf-8")}')
                print(f'乙 核對雜湊值：{"一致" if received.sha256 == sent.sha256 else "不一致"}')
                bob.ack(received)
            print(f'甲 查到回條：{alice.status(sent.id, until=State.ACKED, wait=2)["state"]}')
            reply = bob.send('甲', '收到了')
            for received in alice.receive(wait=2):
                print(f'甲 收到 {received.id}：{received.body.decode("utf-8")}')
                alice.ack(received)
            print(f'乙 查到回條：{bob.status(reply.id, until=State.ACKED, wait=2)["state"]}')
            print('傳送紀錄：')
            for item in alice.transmissions():
                stamp = time.strftime('%H:%M:%S', time.localtime(item['at']))
                print(f'  {stamp} {item["id"][:8]} {item["sender"]}→{item["recipient"]} {item["size"]}B '
                      f'{item["from"] or "-"}→{item["to"]} [{item["link"]}]')
        finally:
            link.stop()
            relay.close()
    return 0


def cmd_serve(args):
    relay = Relay(args.db, policy=DeliveryPolicy(args.ack_timeout, args.max_attempts, args.ttl))
    transfers = Transfers(relay, args.spool or f'{args.db}.spool', limit=args.spool_limit)
    link = HttpLink(relay, args.host, args.port, transfers=transfers)
    print(f'Cloudyfami portal relay listening on {link.url} (db: {args.db})', flush=True)
    try:
        link.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        relay.close()
    return 0


def _client_options(parser):
    parser.add_argument('--url', help='portal URL, e.g. http://127.0.0.1:8790')
    parser.add_argument('--name', help="this client's name on the portal")
    parser.add_argument('--config', help=f'TOML file with url and name (default: {DEFAULT_CONFIG})')
    parser.add_argument('--json', action='store_true', help='print one JSON object per line')


def build_parser():
    parser = argparse.ArgumentParser(prog='cloudyfami-portal')
    commands = parser.add_subparsers(dest='command', required=True)

    commands.add_parser('demo', help='run one full round trip between two clients on this machine')

    serving = commands.add_parser('serve', help='run the relay over HTTP')
    serving.add_argument('--db', required=True)
    serving.add_argument('--host', default='127.0.0.1')
    serving.add_argument('--port', type=int, default=8790)
    serving.add_argument('--ack-timeout', type=float, default=DeliveryPolicy.ack_timeout,
                         help='seconds to wait for a receipt before handing a message out again')
    serving.add_argument('--max-attempts', type=int, default=DeliveryPolicy.max_attempts,
                         help='hand-outs without a receipt before a message is marked failed')
    serving.add_argument('--ttl', type=float, default=DeliveryPolicy.ttl,
                         help='seconds a message is kept before it is marked expired')
    serving.add_argument('--spool', help='folder for file chunks (default: next to --db)')
    serving.add_argument('--spool-limit', type=int, default=DEFAULT_SPOOL_LIMIT,
                         help='bytes of spool shared by unfinished file transfers')

    send = commands.add_parser('send', help='send a text message (reads stdin when TEXT is omitted)')
    _client_options(send)
    send.add_argument('recipient')
    send.add_argument('text', nargs='?')
    send.add_argument('--signal', action='store_true', help='send as a short signal instead of text')
    send.add_argument('--wait-ack', type=float, default=10.0,
                      help='seconds to wait for the receipt; 0 returns as soon as it is queued')

    send_file = commands.add_parser('send-file', help='send a file in chunks')
    _client_options(send_file)
    send_file.add_argument('recipient')
    send_file.add_argument('path')
    send_file.add_argument('--chunk-size', type=int, default=1024 * 1024)
    send_file.add_argument('--wait-ack', type=float, default=0.0,
                           help='seconds to wait for the receipt; 0 returns once the upload finishes')

    listen = commands.add_parser('listen', help='print incoming messages, or hand them to --exec')
    _client_options(listen)
    listen.add_argument('--exec', help='shell command run per message; the body is on stdin and '
                                       'CLOUDYFAMI_MESSAGE_ID/SENDER/KIND/FILE are set. Exit 0 acknowledges')
    listen.add_argument('--save-dir', default='.', help='where received files are written')
    listen.add_argument('--count', type=int, default=0, help='stop after this many messages')
    listen.add_argument('--timeout', type=float, default=0.0, help='stop after this many seconds')
    listen.add_argument('--wait', type=float, default=20.0, help='long-poll seconds per request')

    status = commands.add_parser('status', help="show one message's delivery history")
    _client_options(status)
    status.add_argument('message_id')

    log = commands.add_parser('log', help='show the transmission log and file transfers')
    _client_options(log)
    return parser


COMMANDS = {'demo': cmd_demo, 'serve': cmd_serve, 'send': cmd_send, 'send-file': cmd_send_file,
            'listen': cmd_listen, 'status': cmd_status, 'log': cmd_log}


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format='cloudyfami-portal: %(message)s')
    try:
        return COMMANDS[args.command](args)
    except PortalError as error:
        print(f'cloudyfami-portal: {error}', file=sys.stderr)
        return 2
