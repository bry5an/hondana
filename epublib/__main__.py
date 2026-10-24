"""`python -m epublib …` is the same command line as `hondana …`."""
import sys

from .cli import main

sys.exit(main())
