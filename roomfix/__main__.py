"""roomfix: automatic speaker and room correction. Run `roomfix` or `python -m roomfix`."""
import argparse
import os
import sys
import tempfile
import threading
import time
import webbrowser

from . import __version__

IDLE_S = 30 * 60  # started from the tray icon: close this long after the page was last open


def _log_to_file():
    """Without a window there is nowhere to print to: messages and errors go to page.log."""
    from . import config
    out = open(config.data_dir() / "page.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = out
    print(time.strftime("\n%Y-%m-%d %H:%M:%S"), "roomfix", __version__)


def _close_when_idle(server, app, idle=IDLE_S, every=30.0):
    """The open page asks for the state every few seconds (at least once a minute in a
    background tab). Long silence means it has been closed: stop, unless a measurement runs."""
    while True:
        time.sleep(every)
        if not app.job["running"] and time.monotonic() - app.seen > idle:
            print("the page has been closed for", idle // 60, "minutes: closing")
            server.shutdown()
            return


def main():
    parser = argparse.ArgumentParser(prog="roomfix", description=__doc__)
    parser.add_argument("--demo", action="store_true", help="try it with simulated speakers, no hardware needed")
    parser.add_argument("--port", type=int, help="default: 8137, or the next free one")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--engine", action="store_true",
                        help="run the applied correction in the background: keeps the playback engines running "
                             "(roomfix starts this itself when you apply a correction, and at login)")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--from-tray", action="store_true", help=argparse.SUPPRESS)  # no window, see IDLE_S
    args = parser.parse_args()
    if args.engine:
        from . import supervisor
        return supervisor.main()
    if args.demo:
        os.environ.setdefault("ROOMFIX_HOME", tempfile.mkdtemp(prefix="roomfix-demo-"))
    if args.from_tray:
        _log_to_file()

    from .server import App, open_server
    kind = "roomfix demo" if args.demo else "roomfix"
    try:
        app = App(demo=args.demo)
        server, url = open_server(app, args.port)
    except OSError:
        print(f"Port {args.port} is in use by another program; choose another one with --port."
              if args.port else "No free port found; choose one with --port.")
        return
    if server is None:
        print(f"{kind} is already running at {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    print(f"{kind} {__version__} is running at {url}" + ("" if args.from_tray else "  (Ctrl+C to quit)"))
    if not args.no_browser:
        webbrowser.open(url)
    if args.from_tray:
        threading.Thread(target=_close_when_idle, args=(server, app), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
