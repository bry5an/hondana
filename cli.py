#!/usr/bin/env python3
"""`uv run cli.py …` from a checkout: the same as the installed `hondana` command."""
import sys

from epublib.cli import main

if __name__ == "__main__":
    sys.exit(main())
