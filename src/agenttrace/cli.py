"""
Command-line interface for Tensorscope.
"""

import argparse
import os
import sys
import webbrowser
import subprocess
import time
import signal
import importlib.resources as pkg_resources
import json

from .agenttrace import TraceManager
from .trajectory import GitVersionManager, PackagingApprovalRequired, TrajectoryBuilder

def get_frontend_dir():
    """Get the path to the frontend directory."""
    # In a proper installation, the frontend directory will be in the package
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'frontend')

def start_command(args):
    """Start the Tensorscope frontend server."""
    print("Starting Tensorscope frontend...")
    
    frontend_dir = get_frontend_dir()
    if not os.path.exists(frontend_dir):
        print(f"Error: Frontend directory not found at {frontend_dir}")
        return 1
    
    # Navigate to the frontend directory and start the servers
    try:
        # Run npm install if requested
        if args.install:
            print("Installing frontend dependencies...")
            subprocess.run(['npm', 'run', 'install:all'], cwd=frontend_dir, check=True)
        
        # Start both servers
        process = subprocess.Popen(
            ['npm', 'run', 'start'],
            cwd=frontend_dir,
            stdout=subprocess.PIPE if args.quiet else None,
            stderr=subprocess.STDOUT if args.quiet else None
        )
        
        # Give the servers a moment to start
        time.sleep(3)
        
        # Open browser if requested
        if not args.no_browser:
            webbrowser.open(f"http://localhost:5173")
        
        print("Tensorscope web interface is running.")
        print("- Frontend: http://localhost:5173")
        print("- API: http://localhost:3002")
        print("\nPress Ctrl+C to stop the servers")
        
        # Keep the process running until interrupted
        process.wait()
        
    except KeyboardInterrupt:
        print("\nShutting down Tensorscope servers...")
        if 'process' in locals():
            process.terminate()
            process.wait()
    except subprocess.CalledProcessError as e:
        print(f"Error running npm commands: {e}")
        return 1
    except Exception as e:
        print(f"Error starting Tensorscope frontend: {e}")
        return 1
    
    return 0

def snapshot_command(args):
    """Create a git-backed task production snapshot."""
    tracer = TraceManager(db_path=args.db, colored_logging=not args.no_color)
    manager = GitVersionManager(args.repo, tracer=tracer, session_id=args.session_id)
    commit = manager.snapshot(args.message, paths=args.paths, allow_empty=args.allow_empty)
    print(commit)
    return 0

def preview_command(args):
    """Show a redacted trajectory preview before packaging."""
    tracer = TraceManager(db_path=args.db, colored_logging=not args.no_color)
    builder = TrajectoryBuilder(
        repo_path=args.repo,
        tracer=tracer,
        session_id=args.session_id,
        task_context=args.task_context or "",
    )
    preview = builder.preview_package(paths=args.paths, rev_range=args.rev_range)
    payload = json.dumps(preview, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as file_obj:
            file_obj.write(payload)
        print(args.output)
    else:
        print(payload)
    return 0

def export_command(args):
    """Export a redacted trajectory directory and zip after expert confirmation."""
    tracer = TraceManager(db_path=args.db, colored_logging=not args.no_color)
    builder = TrajectoryBuilder(
        repo_path=args.repo,
        tracer=tracer,
        session_id=args.session_id,
        task_context=args.task_context or "",
    )
    try:
        result = builder.export_package(
            paths=args.paths,
            output_path=args.output,
            confirmed_preview_id=args.confirmed_preview_id,
            rev_range=args.rev_range,
        )
    except PackagingApprovalRequired as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

def tokens_command(args):
    """Print token usage for a session."""
    tracer = TraceManager(db_path=args.db, colored_logging=not args.no_color)
    print(json.dumps(tracer.get_token_usage(session_id=args.session_id), ensure_ascii=False, indent=2))
    return 0

def main():
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(description="Tensorscope command-line interface")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Start command
    start_parser = subparsers.add_parser("start", help="Start the Tensorscope frontend")
    start_parser.add_argument("--no-browser", action="store_true", help="Don't open a browser window")
    start_parser.add_argument("--install", action="store_true", help="Install dependencies before starting")
    start_parser.add_argument("--quiet", action="store_true", help="Suppress npm output")

    snapshot_parser = subparsers.add_parser("snapshot", help="Commit a git snapshot for the current task")
    snapshot_parser.add_argument("--repo", default=".", help="Repository to version")
    snapshot_parser.add_argument("--db", default="traces.db", help="Trace database path")
    snapshot_parser.add_argument("--session-id", required=True, help="Task/session id")
    snapshot_parser.add_argument("--message", required=True, help="Git commit message")
    snapshot_parser.add_argument("--allow-empty", action="store_true", help="Allow an empty git commit")
    snapshot_parser.add_argument("--no-color", action="store_true", help="Disable colored trace logging")
    snapshot_parser.add_argument("paths", nargs="*", help="Paths to include in the snapshot")

    preview_parser = subparsers.add_parser("trajectory-preview", help="Generate a redacted trajectory preview")
    preview_parser.add_argument("--repo", default=".", help="Repository to extract from")
    preview_parser.add_argument("--db", default="traces.db", help="Trace database path")
    preview_parser.add_argument("--session-id", required=True, help="Task/session id")
    preview_parser.add_argument("--task-context", default="", help="Task context used to build the redaction policy")
    preview_parser.add_argument("--rev-range", default=None, help="Optional git revision range")
    preview_parser.add_argument("--output", help="Write preview JSON to this path")
    preview_parser.add_argument("--no-color", action="store_true", help="Disable colored trace logging")
    preview_parser.add_argument("paths", nargs="*", help="Paths to include")

    export_parser = subparsers.add_parser("trajectory-export", help="Export a confirmed redacted trajectory directory and zip")
    export_parser.add_argument("--repo", default=".", help="Repository to extract from")
    export_parser.add_argument("--db", default="traces.db", help="Trace database path")
    export_parser.add_argument("--session-id", required=True, help="Task/session id")
    export_parser.add_argument("--task-context", default="", help="Task context used to build the redaction policy")
    export_parser.add_argument("--rev-range", default=None, help="Optional git revision range")
    export_parser.add_argument("--output", required=True, help="Output zip package path")
    export_parser.add_argument("--confirmed-preview-id", required=True, help="Preview id confirmed by the expert")
    export_parser.add_argument("--no-color", action="store_true", help="Disable colored trace logging")
    export_parser.add_argument("paths", nargs="*", help="Paths to include")

    tokens_parser = subparsers.add_parser("tokens", help="Summarize recorded token usage")
    tokens_parser.add_argument("--db", default="traces.db", help="Trace database path")
    tokens_parser.add_argument("--session-id", required=True, help="Task/session id")
    tokens_parser.add_argument("--no-color", action="store_true", help="Disable colored trace logging")
    
    args = parser.parse_args()
    
    if args.command == "start":
        return start_command(args)
    elif args.command == "snapshot":
        return snapshot_command(args)
    elif args.command == "trajectory-preview":
        return preview_command(args)
    elif args.command == "trajectory-export":
        return export_command(args)
    elif args.command == "tokens":
        return tokens_command(args)
    else:
        parser.print_help()
        return 1

if __name__ == "__main__":
    sys.exit(main()) 
