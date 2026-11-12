#!/usr/bin/env python3
"""Source-tree entry point."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kubedeploy.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
