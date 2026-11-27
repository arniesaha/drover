"""Fresh-process coverage of quota/routing without the analytics MCP facade."""

import os
import subprocess
import sys
from pathlib import Path


def test_control_quota_and_route_without_analytics_imports(tmp_path):
    source = r"""
import importlib.abc
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

class NoAnalytics(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'drover.server.mcp.tools' or fullname.startswith((
            'drover.server.lake', 'drover.server.summarizer',
            'drover.server.cockpit', 'drover.server.embeddings',
        )):
            raise AssertionError('forbidden analytics import: ' + fullname)
sys.meta_path.insert(0, NoAnalytics())
from drover.server.providers.quota_read import read_provider_quota
from drover.server.work_queue.service import route, WorkDenied

class Usage:
    def latest_accounts(self):
        return []
assert read_provider_quota(duckdb_path=Path(sys.argv[1]), service=Usage())['accounts'] == []
now = datetime.now(timezone.utc)
account = dict(provider='openai', status='ok', hosts=['host'], windows=[dict(
    kind='primary', used_percent=10, status='ok',
    resets_at=(now + timedelta(hours=1)).isoformat())])
policy = SimpleNamespace(quota=SimpleNamespace(reserve_fraction=0.1))
assert route([account], policy, 5, {('host', 'codex')}) == ('host', 'codex')
account['windows'][0]['resets_at'] = (now - timedelta(seconds=1)).isoformat()
try:
    route([account], policy, 5, {('host', 'codex')})
except WorkDenied:
    pass
else:
    raise AssertionError('expired quota admitted work')
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path / "unused.duckdb")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
