import json
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]


def read_json(relative_path):
	with (APP_ROOT / relative_path).open(encoding="utf-8") as handle:
		return json.load(handle)


def test_agent_keeps_internal_module_for_syncing():
	modules = (APP_ROOT / "bookkeeping_agent" / "modules.txt").read_text(encoding="utf-8").splitlines()
	assert modules == ["Bookkeeping Agent"]

	metadata_files = [
		"bookkeeping_agent/bookkeeping_agent/page/bookkeeping_agent/bookkeeping_agent.json",
		"bookkeeping_agent/bookkeeping_agent/doctype/bookkeeping_agent_memory/bookkeeping_agent_memory.json",
		"bookkeeping_agent/bookkeeping_agent/doctype/bookkeeping_agent_organization_memory/bookkeeping_agent_organization_memory.json",
		"bookkeeping_agent/bookkeeping_agent/doctype/bookkeeping_agent_playbook_memory/bookkeeping_agent_playbook_memory.json",
	]
	for metadata_file in metadata_files:
		assert read_json(metadata_file)["module"] == "Bookkeeping Agent"


def test_legacy_desk_route_redirect_asset_is_included():
	hooks = (APP_ROOT / "bookkeeping_agent" / "hooks.py").read_text(encoding="utf-8")
	assert "bookkeeping_agent_redirect.js" in hooks

	redirect_js = APP_ROOT / "bookkeeping_agent" / "public" / "js" / "bookkeeping_agent_redirect.js"
	assert redirect_js.exists()

	redirect_source = redirect_js.read_text(encoding="utf-8")
	assert "/desk/bookkeeping-agent" in redirect_source
	assert "/app/bookkeeping-agent" in redirect_source
