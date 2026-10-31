"""Check or explicitly authorize a settled v3 ledger; never starts a trading day."""

from ashare_quant.paper.cli import settled_migration_main as main

if __name__ == "__main__":
    main()
