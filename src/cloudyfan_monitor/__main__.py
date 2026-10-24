"""Local preview only; live service assembly requires an explicit host integration."""
import argparse
from contextlib import ExitStack
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

import httpx

from .app import App
from .billing_config import BillingConfigurationError, load_billing_credentials
from .demo import build_demo
from .integration import build_local_integration
from .openrouter_accounting import OpenRouterAccounting


class ThreadedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", required=True,
                        help="Mock model calls only; read-only billing requires explicit opt-in")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data", type=Path, default=Path("runtime/local-integration"))
    parser.add_argument("--core-root", type=Path)
    parser.add_argument("--legacy-demo", action="store_true", help="Explicitly open the prior synthetic fixture")
    parser.add_argument("--simulation50", action="store_true",
                        help="Ollama customer -> MRC -> Monitor50-question simulation, mock models only")
    parser.add_argument("--openrouter-billing", action="store_true",
                        help="Enable manual read-only OpenRouter account queries; no startup requests")
    parser.add_argument("--billing-key-file", type=Path,
                        help="Read only recognized OpenRouter billing assignments from this explicit file")
    args = parser.parse_args(argv)
    if args.simulation50 and (args.legacy_demo or args.openrouter_billing or args.billing_key_file is not None):
        parser.error("simulation50_requires_separate_mock_only_configuration")
    if args.billing_key_file is not None and not args.openrouter_billing:
        parser.error("billing_key_file_requires_openrouter_billing")
    try:
        credentials = load_billing_credentials(key_file=args.billing_key_file) if args.openrouter_billing else None
    except BillingConfigurationError as error:
        parser.error(error.code)
    origin = f"http://127.0.0.1:{args.port}"
    with ExitStack() as resources:
        billing_source = None
        if credentials is not None:
            http = resources.enter_context(httpx.Client(follow_redirects=False, trust_env=False))
            billing_source = OpenRouterAccounting(http, api_key=credentials.api_key,
                                                  management_key=credentials.management_key)
        if args.simulation50:
            from .simulation import build_simulation
            app = build_simulation(args.data, origin=origin).app
        elif args.legacy_demo:
            app, _ = build_demo(args.data, core_root=args.core_root, origin=origin)
        else:
            app = build_local_integration(args.data, core_root=args.core_root, origin=origin, seed=True).app
        if billing_source is not None:
            app = App(app.gateway, admin_token=app.admin_token, source=app.source, origin=app.origin,
                      identity_store=app.identity_store, billing_source=billing_source)
        billing_notice = "; OpenRouter account queries require manual action" if billing_source else ""
        print(f"CloudyFan Monitor: {origin} (MockTransport models; synthetic task fees{billing_notice})", flush=True)
        if args.simulation50:
            print("Ollama customer simulation ready; use the Monitor50-question control to start", flush=True)
        with make_server("127.0.0.1", args.port, app, server_class=ThreadedServer) as server:
            server.serve_forever()


if __name__ == "__main__":
    main()
