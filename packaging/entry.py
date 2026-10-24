"""The program PyInstaller freezes: the same command line as `hondana`, and with no
arguments (double-clicking the app) it runs `desktop`."""
import multiprocessing
import sys

from epublib.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
