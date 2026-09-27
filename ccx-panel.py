#!/usr/bin/env python3
from fleet.cli import main
import sys

if __name__ == '__main__':
    raise SystemExit(main([*sys.argv[1:], 'panel']))
