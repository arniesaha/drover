#!/bin/bash
set -euo pipefail
hook_dir="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/env python3 "$hook_dir/pre_job_guard.py"
