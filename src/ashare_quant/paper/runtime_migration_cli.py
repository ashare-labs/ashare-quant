"""Explicit pre-start runtime authorization; default operation only checks."""

from ashare_quant.paper.migration_cli import run
from ashare_quant.paper.runtime_migration import migrate


def main():
    run(__doc__, migrate, binding_required=True)


if __name__ == "__main__":
    main()
