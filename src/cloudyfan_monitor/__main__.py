"""Local preview only; live service assembly requires an explicit host integration."""
import argparse
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from .demo import build_demo
from .integration import build_local_integration


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", required=True, help="Mock models only; no provider credentials")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data", type=Path, default=Path("runtime/local-integration"))
    parser.add_argument("--core-root", type=Path)
    parser.add_argument("--legacy-demo", action="store_true", help="Explicitly open the prior synthetic fixture")
    args = parser.parse_args()
    origin = f"http://127.0.0.1:{args.port}"
    if args.legacy_demo:
        app, _ = build_demo(args.data, core_root=args.core_root, origin=origin)
    else:
        app = build_local_integration(args.data, core_root=args.core_root, origin=origin, seed=True).app
    print(f"CloudyFan Monitor: {origin} (MockTransport; displayed fees are synthetic)", flush=True)
    with make_server("127.0.0.1", args.port, app, server_class=ThreadedServer) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
