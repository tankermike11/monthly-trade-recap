"""Build the demo site's data and the folder that gets deployed to Vercel.

    python scripts/build_site.py --results archive/2026-10/run-0001

1. site/data/personas.json: each October persona's recap, built ahead of time from a
   saved model run (no API calls here). The run must match the configured model and
   prompt version, so the site shows what the pipeline produces today.
2. site/downloads/: the blank template and one filled workbook per persona; and
   site/gallery.html: the review gallery for the same recaps.
3. deploy/ (gitignored): only what the site runs, ready for `vercel deploy deploy`:
   the function (api/index.py), the site files, the pipeline code it imports, config,
   prompts and templates, a requirements.txt with the light dependencies only, and
   vercel.json. No mock data, generator or answer keys.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from pipeline.demo import payload  # noqa: E402
from pipeline.ingest import load_month  # noqa: E402
from pipeline.run import facts_for  # noqa: E402
from pipeline.workbook import write_workbook  # noqa: E402
from pipeline.write import ModelConfig  # noqa: E402
from schemas.recap import WriteResult  # noqa: E402

MONTH = "2026-10"
DEPLOY_DIRS = ["site", "pipeline", "schemas", "config", "prompts", "templates"]
DEPLOY_REQUIREMENTS = ["pydantic", "openpyxl", "et_xmlfile", "jinja2", "MarkupSafe", "pyyaml", "anthropic"]


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def build_site_data(results: Path) -> list[dict]:
    cfg = ModelConfig.load()
    if results.is_dir():                    # a monthly run: archive/<month>/run-NNNN
        saved = {p.parent.name: WriteResult.model_validate(json.loads(p.read_text(encoding="utf-8")))
                 for p in sorted((results / "customers").glob("*/recap.json"))}
    else:                                   # a comparison file: out/compare*/<month>/<model>.json
        saved = {r["customer_id"]: WriteResult.model_validate(r)
                 for r in json.loads(results.read_text(encoding="utf-8"))}
    personas = []
    _write(REPO / "site" / "downloads" / "recap_template.xlsx", write_workbook(None, MONTH))
    for folder in sorted((REPO / "data" / "raw" / MONTH).iterdir()):
        pid = folder.name
        res = saved[pid]
        if (res.model, res.prompt_version) != (cfg.model, cfg.prompt_version):
            raise SystemExit(f"{results} was made with {res.model} / {res.prompt_version}; the site needs "
                             f"{cfg.model} / {cfg.prompt_version}. Run pipeline.compare first.")
        raw = load_month(folder)
        linked, sheet = facts_for(REPO / "data" / "raw", MONTH, pid)
        spec = yaml.safe_load(next((REPO / "mock" / "personas").glob(f"{pid}_*.yaml")).read_text(encoding="utf-8"))
        workbook = f"{pid}_{raw.account.first_name.lower()}_{MONTH}.xlsx"
        _write(REPO / "site" / "downloads" / workbook, write_workbook(raw))
        personas.append({"id": pid, "summary": " ".join(str(spec.get("summary", "")).split()),
                         "workbook": workbook, **payload(sheet, linked, res, cfg, [])})
    from pipeline.gallery import build as build_gallery
    _write(REPO / "site" / "gallery.html", build_gallery(MONTH, REPO / "data" / "raw", saved))
    _write(REPO / "site" / "data" / "personas.json",
           json.dumps(personas, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return personas


def assemble_deploy(dest: Path) -> None:
    link = None                                   # keep the Vercel project link (deploy/.vercel) across rebuilds
    if (dest / ".vercel").is_dir():
        link = {p.name: p.read_bytes() for p in (dest / ".vercel").iterdir() if p.is_file()}
    if dest.exists():
        shutil.rmtree(dest)
    if link:
        for name, data in link.items():
            _write(dest / ".vercel" / name, data)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "compare.py", "run.py", "ingest.py")
    for d in DEPLOY_DIRS:
        shutil.copytree(REPO / d, dest / d, ignore=ignore)
    shutil.copytree(REPO / "api", dest / "api", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    pins = {}
    for line in (REPO / "requirements.lock").read_text(encoding="utf-8").splitlines():
        name, _, ver = line.partition("==")
        pins[name.strip().lower()] = line.strip()
    lines = []
    for name in DEPLOY_REQUIREMENTS:
        if name.lower() not in pins:
            raise SystemExit(f"{name} is not in requirements.lock")
        lines.append(pins[name.lower()])
    _write(dest / "requirements.txt", "\n".join(lines) + "\n")
    _write(dest / "public" / "robots.txt", "User-agent: *\nDisallow: /\n")
    _write(dest / ".vercelignore", ".env*\n.gitignore\n__pycache__\n")   # `vercel link` drops a token in .env.local
    _write(dest / "vercel.json", json.dumps({
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "framework": None,
        "buildCommand": "",
        "outputDirectory": "public",
        "functions": {"api/index.py": {"maxDuration": 120,
                                       "includeFiles": "{" + ",".join(DEPLOY_DIRS) + "}/**"}},
        "rewrites": [{"source": "/(.*)", "destination": "/api/index"}],
    }, indent=2) + "\n")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--results", type=Path, required=True,
                    help="October recaps: a monthly run folder (archive/2026-10/run-NNNN) or a pipeline.compare file")
    ap.add_argument("--deploy", type=Path, default=REPO / "deploy")
    a = ap.parse_args(argv)
    personas = build_site_data(a.results)
    assemble_deploy(a.deploy)
    print(f"{len(personas)} personas; site data in site/; deploy folder {a.deploy}")


if __name__ == "__main__":
    main()
