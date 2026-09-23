"""`rova db upgrade | seed | jobs tick | serve` (A2.14)."""
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: rova {db|seed|jobs|serve} ...", file=sys.stderr)
        sys.exit(2)

    cmd = sys.argv[1]

    if cmd == "db" and len(sys.argv) >= 3 and sys.argv[2] == "upgrade":
        from alembic import command
        from alembic.config import Config
        cfg = Config("alembic.ini")
        command.upgrade(cfg, "head")
        return

    if cmd == "seed":
        from rova.domain.hooks import wire
        from rova.seed.seed import run_seed
        wire()
        config_only = "--config-only" in sys.argv
        run_seed(config_only=config_only)
        return

    if cmd == "serve":
        import uvicorn
        uvicorn.run("rova.main:create_app", factory=True, host="0.0.0.0", port=8000)
        return

    if cmd == "jobs" and len(sys.argv) >= 3 and sys.argv[2] == "tick":
        from rova.jobs.tick import tick
        tick()
        return

    print(f"unknown command: {' '.join(sys.argv[1:])}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
