"""Check the server-clock contract and execute the real dashboard renderer."""

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.api import dashboard_data
from app.db import MailboxThrottle
from app.settings import Settings


@pytest.mark.integration
@pytest.mark.parametrize("offset", [None, -600, 600])
async def test_dashboard_cooldown_preserves_history_but_reports_current_state(db_session, offset):
    settings = Settings(_env_file=None, gmail_address="cooldown@example.com")
    until = datetime.now(UTC) + timedelta(seconds=offset) if offset is not None else None
    if offset is not None:
        db_session.add(MailboxThrottle(mailbox=settings.gmail_address, cooldown_until=until, reason="SMTP 451 temporary failure"))
        await db_session.commit()
    payload = await dashboard_data("admin", db_session, settings)
    limits = payload["runtime"]["rate_limits"]
    assert limits["cooldown_active"] is (offset is not None and offset > 0)
    assert limits["cooldown_until"] == (until.isoformat() if until else None)
    assert limits["cooldown_reason"] == ("SMTP 451 temporary failure" if until else None)


def test_dashboard_renders_expired_cooldowns_off_and_supports_old_api():
    if shutil.which("node") is None:
        pytest.skip("Node.js is required for dashboard rendering tests")
    html_path = Path(__file__).resolve().parents[1] / "app" / "dashboard.html"
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements = new Map();
const context = {
  document: {addEventListener() {}, querySelector: key => {
    if (!elements.has(key)) elements.set(key, {addEventListener() {}, classList: {toggle() {}}, style: {}});
    return elements.get(key);
  }},
  setInterval() {},
  fetch: async () => {throw new Error('No network in rendering test');},
};
// Disable only the startup fetch; all rendering functions are the shipped code.
vm.createContext(context);
vm.runInContext(source.replace(/^    load\(\);\s*$/m, ''), context);
const base = {generated_at: '2026-09-07T08:00:00Z', runtime: {credentials: {}}, cases_by_status: {}};
const cases = [
  {cooldown_active: false, cooldown_until: '2026-09-03T05:56:37Z'},
  {cooldown_active: true, cooldown_until: '2026-09-07T08:10:00Z'},
  {cooldown_until: '2026-09-03T05:56:37Z'},
  {cooldown_until: '2026-09-07T08:10:00Z'},
  {cooldown_until: '2026-09-07T08:00:00Z'},
  {cooldown_until: null},
  {cooldown_until: 'invalid'},
  {cooldown_active: false, cooldown_until: '2099-01-01T00:00:00Z'},
];
const result = cases.map(limits => {
  context.payload = {...base, runtime: {...base.runtime, rate_limits: limits}};
  vm.runInContext('renderConfig(payload)', context);
  return elements.get('#config-row').innerHTML.match(/<span class="chip">Gmail cooldown[\s\S]*?(?=<span class="chip">|$)/)[0];
});
console.log(JSON.stringify(result));
"""
    result = subprocess.run(["node", "-e", script, str(html_path)], capture_output=True, text=True, check=True)
    for fragment, expected in zip(json.loads(result.stdout), ["OFF", "PAUSED", "OFF", "PAUSED", "OFF", "OFF", "OFF", "OFF"], strict=True):
        assert expected in fragment
        assert ("PAUSED" if expected == "OFF" else "OFF") not in fragment
