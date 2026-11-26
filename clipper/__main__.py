"""Command line: python -m clipper <command>

  serve       start the web app (it runs jobs too unless CLIPPER_RUN_WORKER=false)
  worker      run the job worker on its own, so transcribing never slows the website
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
    sub.add_parser("worker", help="run the job worker")
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

    if args.command == "worker":
        return _run_worker(settings)

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


def _run_worker(settings) -> int:
    """Process jobs from the shared queue until stopped. Any number of workers can share the
    database queue, and the web app can keep running in its own process."""
    import signal
    import threading

    from .maintenance import Maintenance
    from .pipeline import handlers
    from .services import Services

    svc = Services.create(settings)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    svc.queue.start(handlers(svc), settings.worker_concurrency)
    maintenance = Maintenance(svc)
    maintenance.start()
    logging.getLogger("clipper").info("Worker running with concurrency %d", settings.worker_concurrency)
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    maintenance.stop()
    svc.queue.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
