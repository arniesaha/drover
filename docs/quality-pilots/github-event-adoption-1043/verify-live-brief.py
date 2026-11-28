#!/usr/bin/env python3
"""Recheck the reviewed #1043 brief in an execution checkout before launch."""

import argparse
import hashlib
import json
import pathlib
import sys

PACKET = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PACKET))

from config_contract import BRIEF_SHA256, brief_ref  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--execution-cwd", type=pathlib.Path, required=True)
parser.add_argument("--brief-ref", required=True)
args = parser.parse_args()
reference = brief_ref(args.execution_cwd, args.brief_ref)
print(json.dumps({"brief_ref": reference, "sha256": BRIEF_SHA256}, indent=2))
