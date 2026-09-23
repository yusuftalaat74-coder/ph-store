"""`rova db upgrade | seed | catalogue import | jobs tick | serve` (A2.14)."""
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: rova {db|seed|catalogue|jobs|serve} ...", file=sys.stderr)
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

    if cmd == "catalogue" and len(sys.argv) >= 3 and sys.argv[2] == "import":
        from rova.catalogue.importer import run_import
        from rova.core.db import get_sessionmaker
        from rova.domain.hooks import wire
        wire()
        directory = sys.argv[3] if len(sys.argv) > 3 else "data"
        stock = 500
        for i, a in enumerate(sys.argv):
            if a == "--assume-stock" and i + 1 < len(sys.argv):
                stock = int(sys.argv[i + 1])
        session = get_sessionmaker()()
        try:
            report = run_import(session, directory, assume_stock=stock)
        finally:
            session.close()
        for line in report.lines():
            print(line)
        print()
        print("Every product is loaded as NOT price-regulated: the official")
        print("Diploma Ministerial 21/2017 figures are not in these files.")
        print("Offer quantities are a placeholder — a price list carries no")
        print("stock, and both vendors accept orders with MANUAL_CONFIRM.")
        return

    if cmd == "jobs" and len(sys.argv) >= 3 and sys.argv[2] == "tick":
        from rova.jobs.tick import tick
        tick()
        return

    print(f"unknown command: {' '.join(sys.argv[1:])}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
