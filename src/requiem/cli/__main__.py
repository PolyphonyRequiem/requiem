"""Allow `python -m requiem.cli` to invoke the CLI."""
import sys

from requiem.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
