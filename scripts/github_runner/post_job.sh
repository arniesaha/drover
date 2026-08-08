#!/bin/bash
set -euo pipefail
hook_dir="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/env python3 "$hook_dir/post_job_cleanup.py"
