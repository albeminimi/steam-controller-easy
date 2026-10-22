#!/usr/bin/env python3
"""Esce con 0 se i due status.json differiscono solo negli orari di aggiornamento."""
import json
import sys


def strip(d):
    d = dict(d)
    d.pop("generated_at", None)
    d["sources"] = {k: {kk: vv for kk, vv in v.items() if kk != "updated_at"} for k, v in d.get("sources", {}).items()}
    return d


try:
    a, b = (json.load(open(f)) for f in sys.argv[1:3])
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if strip(a) == strip(b) else 1)
