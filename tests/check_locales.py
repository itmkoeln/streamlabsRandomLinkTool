#!/usr/bin/env python3
"""Checks that every locale file has exactly the same keys as locales/en.json
and that {placeholders} used in the strings match the English ones.

Usage: python3 tests/check_locales.py
"""
import glob
import json
import os
import re
import sys

root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "locales")
files = sorted(glob.glob(os.path.join(root, "*.json")))
data = {os.path.basename(f)[:-5]: json.load(open(f, encoding="utf-8")) for f in files}
if "en" not in data:
    sys.exit("locales/en.json is missing")

ref = data["en"]
fields = lambda s: sorted(re.findall(r"\{(\w+)\}", s)) if isinstance(s, str) else None
problems = 0
for code, d in data.items():
    missing = sorted(set(ref) - set(d))
    extra = sorted(set(d) - set(ref))
    if missing:
        problems += 1
        print(f"[{code}] missing keys: {', '.join(missing)}")
    if extra:
        problems += 1
        print(f"[{code}] unknown keys: {', '.join(extra)}")
    for k in ref:
        if k in d and fields(ref[k]) != fields(d[k]):
            problems += 1
            print(f"[{code}] placeholders differ in '{k}': {fields(d[k])} vs {fields(ref[k])}")
    if len(d.get("weekdays", [])) != 7:
        problems += 1
        print(f"[{code}] 'weekdays' must contain 7 names")

print(f"{len(data)} locales ({', '.join(data)}), {len(ref)} keys each" if not problems else f"{problems} problem(s)")
sys.exit(1 if problems else 0)
