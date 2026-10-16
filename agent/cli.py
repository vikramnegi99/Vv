"""Command-line entrypoint.

Usage:
    python -m agent.cli scan [--mode auto|canary|live]
    python -m agent.cli dashboard
    python -m agent.cli status
    python -m agent.cli report [--write PATH]
    python -m agent.cli canary
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import dashboard, report
from .config import load_config
from .db import DB
from .main import Agent
from .providers import build_provider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _paths(cfg):
    db_path = os.environ.get("AGENT_DB") or os.path.join(
        ROOT, cfg.get_path("storage.db_path", "data/state.db"))
    backup_dir = os.path.join(ROOT, cfg.get_path("storage.backup_dir", "data/backups"))
    dash_path = os.path.join(ROOT, cfg.get_path("dashboard.output_path", "docs/index.html"))
    return db_path, backup_dir, dash_path


def _open_db(cfg) -> DB:
    db_path, backup_dir, _ = _paths(cfg)
    db = DB(db_path)
    db.migrate(backup_dir=backup_dir)
    return db


def cmd_scan(cfg, args) -> int:
    db = _open_db(cfg)
    try:
        provider_name = args.provider or ("mock" if args.mode == "canary" else None)
        provider = build_provider(cfg, provider_name)
        agent = Agent(cfg, db, provider)
        summary = agent.run_cycle(mode=args.mode)
        _, _, dash_path = _paths(cfg)
        dashboard.render(db, cfg, dash_path)
        print(json.dumps(summary, indent=2, default=str))
    finally:
        db.close()
    return 0


def cmd_canary(cfg, args) -> int:
    db = _open_db(cfg)
    try:
        from .providers import build_provider as bp
        agent = Agent(cfg, db, bp(cfg, "mock"))
        agent.challenge.ensure_initialized("mock")
        result = agent.run_canary(reason="cli canary")
        _, _, dash_path = _paths(cfg)
        dashboard.render(db, cfg, dash_path)
        print(json.dumps(result, indent=2, default=str))
    finally:
        db.close()
    return 0


def cmd_dashboard(cfg, args) -> int:
    db = _open_db(cfg)
    try:
        _, _, dash_path = _paths(cfg)
        out = dashboard.render(db, cfg, dash_path)
        print(f"dashboard written: {out}")
    finally:
        db.close()
    return 0


def cmd_status(cfg, args) -> int:
    db = _open_db(cfg)
    try:
        print(json.dumps(report.build_status(db, cfg), indent=2, default=str))
    finally:
        db.close()
    return 0


def cmd_report(cfg, args) -> int:
    db = _open_db(cfg)
    try:
        text = report.build_report(db, cfg)
        if args.write:
            with open(args.write, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"report written: {args.write}")
        else:
            print(text)
    finally:
        db.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description="Paper-only trading agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="run one scan cycle")
    p.add_argument("--mode", default="auto", choices=["auto", "live", "canary"])
    p.add_argument("--provider", default=None, help="override provider (e.g. mock)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("canary", help="run the deterministic canary")
    p.set_defaults(func=cmd_canary)

    p = sub.add_parser("dashboard", help="re-render the dashboard")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("status", help="print machine-readable status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="print the challenge report")
    p.add_argument("--write", default=None)
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    cfg = load_config()
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
