"""Command line: python -m clipper <command>

  serve       start the web app and the job worker
  preflight   print the requirement checks and exit
  backup      write a backup of the database, settings and manifests (see Phase 3)
"""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipper")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="start the web app")
    sub.add_parser("preflight", help="check requirements")
    sub.add_parser("backup", help="back up the database and settings")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .config import load_settings

    settings = load_settings()

    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")
        return 0

    if args.command == "preflight":
        from .preflight import run_checks
        from .services import Services

        checks = run_checks(Services.create(settings))
        failed = 0
        for c in checks:
            mark = "OK  " if c["ok"] else ("WARN" if c["level"] == "warning" else "FAIL")
            failed += 0 if c["ok"] or c["level"] == "warning" else 1
            print(f"[{mark}] {c['name']}: {c['message']}")
        return 1 if failed else 0

    if args.command == "backup":
        from .backup import create_backup

        path = create_backup(settings)
        print(f"Backup written to {path}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
