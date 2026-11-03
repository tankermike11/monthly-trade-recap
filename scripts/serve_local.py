"""Run the demo site's function locally, exactly as Vercel runs it.

    SITE_PASSWORD=... python scripts/serve_local.py [--root deploy] [--port 8000]

--root is the folder holding api/index.py: the repo (default) or the assembled deploy/
folder, to test exactly what gets deployed. Uploads call Claude with ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=REPO)
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    root = a.root.resolve()
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("site_function", root / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    print(f"serving {root} on http://localhost:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), mod.handler).serve_forever()


if __name__ == "__main__":
    main()
