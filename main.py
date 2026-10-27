"""
Red Bricks Property Management - Main Application Entrypoint
CLI and Service Launcher for AI-Powered Automated Rent Reminder Workflow
"""

import sys
import logging
import argparse
import threading
from src.config import settings
from src.workflow import WorkflowOrchestrator
from src.reporting import ReportGenerator
from src.clients.rentvine_mcp import RentVineMCPClient
from src.server import start_server

logger = logging.getLogger("RedBricksMain")


def sync_live_data():
    """Removes demo tenants and synchronizes active RentVine portfolio via sync_manager."""
    from src.sync_manager import sync_manager
    client = RentVineMCPClient()
    if not client.mock_mode:
        print("[SYNC] Connecting to RentVine and syncing live portfolio (parallel)...")
        synced = sync_manager.sync_live_portfolio(limit=1000)
        print(f"[SYNC] Done — synced {synced} live RentVine leases into local database.")
    else:
        print("[WARN] RentVine is in mock mode — no live data to sync. Check .env credentials.")


def main():
    parser = argparse.ArgumentParser(
        description="Red Bricks Property Management - AI Rent Reminder Workflow"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Command: serve (starts web dashboard)
    serve_parser = subparsers.add_parser("serve", help="Start the web dashboard & API server")
    serve_parser.add_argument("--host", default=settings.HOST, help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=settings.PORT, help="Port to listen on")

    # Command: run (runs live daily automation)
    run_parser = subparsers.add_parser("run", help="Execute daily rent reminder workflow")
    run_parser.add_argument("--dry-run", action="store_true", help="Simulate without dispatching SMS")

    # Command: dry-run (alias)
    subparsers.add_parser("dry-run", help="Run simulation without sending live SMS")

    # Command: seed (now syncs live data instead of inserting demo data)
    subparsers.add_parser("seed", help="Wipe demo data and sync live portfolio from RentVine")

    # Command: report (prints latest daily report)
    subparsers.add_parser("report", help="Generate and print daily markdown report")

    args = parser.parse_args()

    if args.command == "serve" or args.command is None:
        host = getattr(args, "host", settings.HOST)
        port = getattr(args, "port", settings.PORT)
        # Kick off live RentVine sync in the background so the server starts immediately
        sync_thread = threading.Thread(target=sync_live_data, daemon=True, name="RentVineSyncThread")
        sync_thread.start()
        start_server(host=host, port=port)

    elif args.command == "run":
        orchestrator = WorkflowOrchestrator()
        summary = orchestrator.execute_daily_run(is_dry_run=args.dry_run)
        print(ReportGenerator.generate_markdown(summary))

    elif args.command == "dry-run":
        orchestrator = WorkflowOrchestrator()
        summary = orchestrator.execute_daily_run(is_dry_run=True)
        print(ReportGenerator.generate_markdown(summary))

    elif args.command == "seed":
        # Wipe demo data and sync live portfolio (runs synchronously so you see progress)
        sync_live_data()

    elif args.command == "report":
        orchestrator = WorkflowOrchestrator()
        summary = orchestrator.execute_daily_run(is_dry_run=True)
        print(ReportGenerator.generate_markdown(summary))


if __name__ == "__main__":
    main()
