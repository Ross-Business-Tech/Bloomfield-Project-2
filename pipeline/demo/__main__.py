"""
Build and open the screenshare demo.

    python -m demo                  # build demo/demo.html (about 20 seconds), then tell you how to open it
    python -m demo --open           # build, then open it in your browser
    python -m demo --no-build --open  # just open the page that is already built (instant)
    python -m demo --serve          # build, then serve it at http://localhost:8000 (for a phone on the same wifi)
    python -m demo --live 6         # also let the REAL AI Reader read the first 6 documents (needs an API key with quota)

The page works offline and uses only FAKE data. You can also just double-click demo/demo.html.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.build import DEFAULT_DATA, DEFAULT_OUT, build  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and open the Bloomfield demo page (fake data, no API needed).")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the page")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA, help="scratch folder for the demo database (wiped each build)")
    parser.add_argument("--no-build", action="store_true", help="do not rebuild; use the existing page")
    parser.add_argument("--open", action="store_true", help="open the page in your browser")
    parser.add_argument("--serve", action="store_true", help="serve the page on http://localhost:8000")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--live", type=int, default=0, metavar="N", help="let the real AI Reader read the first N documents")
    args = parser.parse_args()

    if not args.no_build:
        build(args.out, args.data_dir, live_limit=args.live)
    elif not args.out.exists():
        sys.exit(f"{args.out} does not exist yet: run  python -m demo  first.")
    url = args.out.resolve().as_uri()
    print(f"\nDemo page: {args.out.resolve()}\n  open it by double-clicking, or paste this into a browser: {url}")

    if args.serve:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(args.out.resolve().parent))
        with socketserver.TCPServer(("", args.port), handler) as httpd:
            link = f"http://localhost:{args.port}/{args.out.name}"
            print(f"\nServing at {link}  (Ctrl+C to stop). On a phone on the same wifi use your computer's address instead of localhost.")
            if args.open:
                webbrowser.open(link)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nStopped.")
    elif args.open:
        webbrowser.open(url)


if __name__ == "__main__":
    main()
