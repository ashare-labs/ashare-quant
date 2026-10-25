"""Check or explicitly authorize a settled v3 ledger; never starts a trading day."""

from ashare_quant.paper.migration_cli import run
from ashare_quant.paper.settled_migration import migrate


def main():
    run(__doc__, migrate, ensure_ascii=False)


if __name__ == "__main__":
    main()
