"""roomfix: automatic speaker and room correction. Run `roomfix` or `python -m roomfix`."""
import argparse
import os
import tempfile
import webbrowser

from . import __version__


def main():
    parser = argparse.ArgumentParser(prog="roomfix", description=__doc__)
    parser.add_argument("--demo", action="store_true", help="try it with simulated speakers, no hardware needed")
    parser.add_argument("--port", type=int, help="default: 8137, or the next free one")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    if args.demo:
        os.environ.setdefault("ROOMFIX_HOME", tempfile.mkdtemp(prefix="roomfix-demo-"))

    from .server import App, open_server
    kind = "roomfix demo" if args.demo else "roomfix"
    try:
        server, url = open_server(App(demo=args.demo), args.port)
    except OSError:
        print(f"Port {args.port} is in use by another program; choose another one with --port."
              if args.port else "No free port found; choose one with --port.")
        return
    if server is None:
        print(f"{kind} is already running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    print(f"{kind} {__version__} is running at {url}  (Ctrl+C to quit)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
