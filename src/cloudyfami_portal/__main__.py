import argparse
import os
import tempfile
import time

from .http_link import HttpLink, PortalClient
from .relay import DeliveryPolicy, Relay, State


def demo():
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
            receipt = alice.status(sent.id, until=State.ACKED, wait=2)
            print(f'甲 查到回條：{receipt["state"]}')
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


def serve(args):
    relay = Relay(args.db, policy=DeliveryPolicy(args.ack_timeout, args.max_attempts, args.ttl))
    link = HttpLink(relay, args.host, args.port)
    print(f'Cloudyfami portal relay listening on {link.url} (db: {args.db})', flush=True)
    try:
        link.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        relay.close()


def main(argv=None):
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
    args = parser.parse_args(argv)
    if args.command == 'demo':
        demo()
    else:
        serve(args)


if __name__ == '__main__':
    main()
