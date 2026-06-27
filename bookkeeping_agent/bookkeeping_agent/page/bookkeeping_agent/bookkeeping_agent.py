# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import importlib
import hashlib
import json
import os
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt


AGENT_ROLES = {"Accounts User", "Accounts Manager", "System Manager"}

BOOKKEEPING_DOCTYPES = {
	"Customer",
	"Supplier",
	"Sales Invoice",
	"Purchase Invoice",
	"Payment Entry",
	"Journal Entry",
}

SUPPORT_READ_DOCTYPES = {
	"Company",
	"Account",
	"Item",
	"Cost Center",
	"Mode of Payment",
}

READ_DOCTYPES = BOOKKEEPING_DOCTYPES | SUPPORT_READ_DOCTYPES
DRAFT_DOCUMENT_DOCTYPES = {"Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry"}
PARTY_DOCTYPES = {"Customer", "Supplier"}

SYSTEM_FIELDS = {
	"amended_from",
	"creation",
	"docstatus",
	"doctype",
	"idx",
	"modified",
	"modified_by",
	"name",
	"owner",
	"parent",
	"parentfield",
	"parenttype",
}

BLOCKED_FIELDTYPES = {"Password", "Read Only", "HTML", "Button"}
ALLOWED_FILTER_OPERATORS = {"=", "!=", ">", "<", ">=", "<=", "like", "in", "not in", "between"}
MAX_SEARCH_LIMIT = 20
MAX_REPORT_ROWS = 50
MAX_TOOL_STEPS = 8
MAX_MESSAGES = 20
MAX_RECENT_MESSAGES = 8
MAX_CONVERSATION_SUMMARY_CHARS = 1600
MAX_CONVERSATION_SUMMARY_HEAD = 4
MAX_CONVERSATION_SUMMARY_TAIL = 8
SUMMARY_REPORT_ROWS = 20
READ_TOOL_CACHE_TTL_SECONDS = 60
MAX_USER_MEMORIES = 8
MAX_USER_TEXT_MEMORY_CHARS = 300
MAX_PLAYBOOK_MEMORIES = 5
MAX_ORGANIZATION_MEMORIES = 5
ADMIN_MEMORY_FETCH_LIMIT = 50

DEFAULT_MODEL = "gpt-5.5"
MEMORY_DOCTYPE = "Bookkeeping Agent Memory"
PLAYBOOK_MEMORY_DOCTYPE = "Bookkeeping Agent Playbook Memory"
ORGANIZATION_MEMORY_DOCTYPE = "Bookkeeping Agent Organization Memory"
MEMORY_SOURCE_EXPLICIT = "Explicit User Request"

USER_MEMORY_KEYS = {
	"default_company": {
		"label": "default company",
		"context_label": "Default company",
		"reference_doctype": "Company",
	},
	"default_currency": {
		"label": "default currency",
		"context_label": "Default currency",
		"reference_doctype": "Currency",
	},
	"bookkeeping_preference_note": {
		"label": "bookkeeping preference note",
		"context_label": "Bookkeeping preference note",
		"value_type": "text",
		"max_length": MAX_USER_TEXT_MEMORY_CHARS,
	},
}

DEFAULT_LIST_FIELDS = {
	"Customer": ["name", "customer_name", "customer_group", "territory", "disabled"],
	"Supplier": ["name", "supplier_name", "supplier_group", "disabled"],
	"Sales Invoice": ["name", "customer", "posting_date", "due_date", "grand_total", "outstanding_amount", "status", "docstatus"],
	"Purchase Invoice": ["name", "supplier", "posting_date", "due_date", "grand_total", "outstanding_amount", "status", "docstatus"],
	"Payment Entry": ["name", "payment_type", "party_type", "party", "posting_date", "paid_amount", "received_amount", "status", "docstatus"],
	"Journal Entry": ["name", "voucher_type", "posting_date", "total_debit", "total_credit", "status", "docstatus"],
	"Company": ["name", "company_name", "default_currency"],
	"Account": ["name", "account_name", "account_type", "root_type", "is_group", "company"],
	"Item": ["name", "item_name", "stock_uom", "disabled"],
	"Cost Center": ["name", "cost_center_name", "company", "is_group"],
	"Mode of Payment": ["name", "type", "enabled"],
}

SEARCH_FIELDS = {
	"Customer": ["name", "customer_name"],
	"Supplier": ["name", "supplier_name"],
	"Sales Invoice": ["name", "customer"],
	"Purchase Invoice": ["name", "supplier"],
	"Payment Entry": ["name", "party"],
	"Journal Entry": ["name", "voucher_type"],
	"Company": ["name", "company_name"],
	"Account": ["name", "account_name"],
	"Item": ["name", "item_name"],
	"Cost Center": ["name", "cost_center_name"],
	"Mode of Payment": ["name"],
}

REPORTS = {
	"General Ledger": {
		"module": "erpnext.accounts.report.general_ledger.general_ledger",
		"permission_doctype": "GL Entry",
		"required_filters": ["company", "from_date", "to_date"],
	},
	"Accounts Receivable": {
		"module": "erpnext.accounts.report.accounts_receivable.accounts_receivable",
		"permission_doctype": "Sales Invoice",
		"required_filters": ["company"],
	},
	"Accounts Payable": {
		"module": "erpnext.accounts.report.accounts_payable.accounts_payable",
		"permission_doctype": "Purchase Invoice",
		"required_filters": ["company"],
	},
	"Trial Balance": {
		"module": "erpnext.accounts.report.trial_balance.trial_balance",
		"permission_doctype": "GL Entry",
		"required_filters": ["company", "fiscal_year"],
	},
}

READ_TOOLS = {}
MUTATING_TOOLS = {}


@frappe.whitelist(methods=["POST"])
def chat(messages: str | None = None, run_id: str | None = None):
	"""Run one bookkeeping chat turn and return assistant text plus optional pending actions."""
	ensure_agent_access()

	normalized_messages = normalize_messages(parse_json_value(messages, []))
	if not normalized_messages:
		frappe.throw(_("At least one user message is required."))

	return run_agent(normalized_messages, cstr(run_id).strip()[:80])


@frappe.whitelist(methods=["POST"])
def confirm_action(action: str | None = None):
	"""Execute a previously proposed mutating action after explicit user confirmation."""
	ensure_agent_access()

	action = parse_json_value(action, {})
	if not isinstance(action, dict):
		frappe.throw(_("Invalid action payload."))

	tool_name = action.get("tool") or action.get("tool_name")
	args = action.get("args") or action.get("arguments") or {}

	if tool_name not in MUTATING_TOOLS:
		frappe.throw(_("This action is not available for confirmation."))
	if not isinstance(args, dict):
		frappe.throw(_("Invalid action arguments."))

	result = run_tool(tool_name, args, execute_mutation=True)
	frappe.db.commit()

	return {
		"status": "completed",
		"text": result.get("message") or _("Action completed."),
		"result": result,
	}


def run_agent(messages, run_id=None):
	client = get_openai_client()
	model = get_agent_model()
	input_items = build_model_input(messages)
	pending_actions = []
	tool_results = []
	usage_metrics = new_agent_usage_metrics()
	response = None
	seen_tool_calls = set()

	try:
		for step in range(MAX_TOOL_STEPS):
			publish_agent_event(
				run_id,
				"status",
				{
					"label": _("Planning"),
					"message": _("Planning step {0} of {1}.").format(step + 1, MAX_TOOL_STEPS),
					"status": "running",
				},
			)
			response = create_agent_response(client, model, input_items, run_id)
			record_response_usage(usage_metrics, response)

			tool_calls = extract_function_calls(response)
			if not tool_calls:
				break

			input_items.extend(response_output_as_input(response))

			for tool_call in tool_calls:
				signature = get_tool_call_signature(tool_call["name"], tool_call["arguments"])
				if signature in seen_tool_calls:
					message = _(
						"I already ran {0} with the same inputs. Please narrow the request or provide missing details."
					).format(get_tool_label(tool_call["name"]))
					tool_event = build_tool_event(
						tool_call["name"],
						tool_call["arguments"],
						"needs_input",
						{"message": message},
					)
					tool_results.append(tool_event)
					publish_agent_event(run_id, "tool_finished", tool_event)
					publish_agent_event(
						run_id,
						"done",
						{
							"label": _("Needs input"),
							"message": message,
							"status": "needs_input",
						},
					)
					publish_agent_event(run_id, "assistant_done", {"text": message})
					log_agent_usage(run_id, model, usage_metrics)
					return {
						"text": message,
						"pending_actions": pending_actions,
						"tool_results": tool_results,
						"token_usage": usage_metrics,
					}

				seen_tool_calls.add(signature)
				tool_event = build_tool_event(tool_call["name"], tool_call["arguments"], "running")
				publish_agent_event(run_id, "tool_started", tool_event)
				result = run_tool_for_agent(tool_call["name"], tool_call["arguments"])
				record_tool_usage(usage_metrics, result)
				tool_event = build_tool_event(tool_call["name"], tool_call["arguments"], result.get("status"), result)
				tool_results.append(tool_event)
				publish_agent_event(run_id, "tool_finished", tool_event)

				if result.get("pending_action"):
					pending_actions.append(result["pending_action"])

				input_items.append(
					{
						"type": "function_call_output",
						"call_id": tool_call["call_id"],
						"output": to_json(result),
					}
				)
		else:
			publish_agent_event(
				run_id,
				"status",
				{
					"label": _("Finalizing"),
					"message": _("Tool budget reached. Preparing the best answer from gathered results."),
					"status": "running",
				},
			)
			input_items.append(
				{
					"role": "user",
					"content": (
						"Tool budget reached. Do not call more tools. Use only the completed tool results above "
						"to give the best concise answer. If some details are unknown, say what remains unknown."
					),
				}
			)
			response = create_agent_response(client, model, input_items, run_id, allow_tools=False)
			record_response_usage(usage_metrics, response)
	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), _("Bookkeeping Agent OpenAI Error"))
		publish_agent_event(
			run_id,
			"error",
			{
				"label": _("Request failed"),
				"message": get_safe_agent_error_message(exc),
				"status": "error",
			},
		)
		frappe.throw(get_safe_agent_error_message(exc))

	text = get_response_text(response)
	if not text and pending_actions:
		text = _("I prepared an action for your review.")
	elif not text:
		text = _("I could not produce a response for that request.")

	result = {
		"text": text,
		"pending_actions": pending_actions,
		"tool_results": tool_results,
		"token_usage": usage_metrics,
	}
	log_agent_usage(run_id, model, usage_metrics)
	publish_agent_event(
		run_id,
		"done",
		{
			"label": _("Done"),
			"message": _("Response ready."),
			"status": "ok",
		},
	)
	publish_agent_event(run_id, "assistant_done", {"text": text})
	return result


def create_agent_response(client, model, input_items, run_id=None, allow_tools=True):
	"""Create a Responses API result while forwarding text deltas over Frappe realtime."""
	request = {
		"model": model,
		"instructions": get_system_prompt(),
		"input": input_items,
	}
	if allow_tools:
		request["tools"] = get_openai_tool_schemas()
		request["parallel_tool_calls"] = False

	try:
		stream = client.responses.create(**request, stream=True)
	except TypeError:
		return client.responses.create(**request)

	state = new_response_stream_state()
	for event in stream:
		handle_response_stream_event(event, run_id, state)

	response = state.get("response") or build_response_from_stream_state(state)
	if response:
		return response

	frappe.throw(_("The OpenAI response stream ended before a response was available."))


def new_response_stream_state():
	return {
		"response": None,
		"text_parts": [],
		"text_started": False,
		"items": {},
		"item_order": [],
	}


def handle_response_stream_event(event, run_id, state):
	event_type = cstr(get_stream_value(event, "type"))

	if event_type == "response.output_text.delta":
		delta = cstr(get_stream_value(event, "delta") or get_stream_value(event, "text"))
		if delta:
			if not state["text_started"]:
				state["text_started"] = True
				publish_agent_event(run_id, "assistant_started", {})
			state["text_parts"].append(delta)
			publish_agent_event(run_id, "assistant_delta", {"delta": delta})
		return

	if event_type in {"response.completed", "response.done"}:
		state["response"] = get_stream_value(event, "response") or state.get("response")
		return

	if event_type in {"response.failed", "error"}:
		error = get_stream_value(event, "error") or {}
		message = get_stream_value(error, "message") or _("The OpenAI response stream failed.")
		frappe.throw(cstr(message))

	if event_type in {"response.output_item.added", "response.output_item.done"}:
		item = normalize_stream_item(get_stream_value(event, "item"))
		if item:
			remember_stream_item(state, item)
		return

	if event_type == "response.function_call_arguments.done":
		item_id = cstr(get_stream_value(event, "item_id"))
		item = state["items"].get(item_id, {}) if item_id else {}
		name = cstr(get_stream_value(event, "name") or item.get("name"))
		call_id = cstr(item.get("call_id") or get_stream_value(event, "call_id") or item_id)
		arguments = cstr(get_stream_value(event, "arguments") or item.get("arguments") or "{}")

		if item_id:
			remember_stream_item(
				state,
				{
					"id": item_id,
					"type": "function_call",
					"name": name,
					"call_id": call_id,
					"arguments": arguments,
				},
			)


def remember_stream_item(state, item):
	item_id = cstr(item.get("id") or item.get("item_id") or item.get("call_id") or len(state["item_order"]))
	if not item_id:
		return
	item["id"] = item_id
	if item_id not in state["items"]:
		state["item_order"].append(item_id)
	state["items"][item_id] = {**state["items"].get(item_id, {}), **item}


def normalize_stream_item(item):
	if not item:
		return {}
	if isinstance(item, dict):
		return dict(item)
	if hasattr(item, "model_dump"):
		return item.model_dump(exclude_none=True)

	result = {}
	for key in ("id", "type", "name", "call_id", "arguments", "content"):
		value = getattr(item, key, None)
		if value is not None:
			result[key] = value
	return result


def build_response_from_stream_state(state):
	output = []
	for item_id in state.get("item_order", []):
		item = state["items"].get(item_id) or {}
		if item.get("type") == "function_call":
			output.append(
				{
					"type": "function_call",
					"name": item.get("name") or "",
					"call_id": item.get("call_id") or item_id,
					"arguments": item.get("arguments") or "{}",
				}
			)

	text = "".join(state.get("text_parts") or []).strip()
	if text:
		output.append({"type": "message", "content": [{"type": "output_text", "text": text}]})

	if not output:
		return None

	return {"output": output, "output_text": text}


def run_tool(tool_name, args, execute_mutation=False):
	if not isinstance(args, dict):
		return {"status": "error", "message": _("Tool arguments must be an object.")}

	if tool_name in READ_TOOLS:
		return run_cached_read_tool(tool_name, args)

	if tool_name in MUTATING_TOOLS:
		if not execute_mutation:
			return prepare_pending_action(tool_name, args)
		return MUTATING_TOOLS[tool_name](args)

	return {"status": "error", "message": _("Unknown tool: {0}").format(frappe.bold(tool_name))}


def run_tool_for_agent(tool_name, args):
	try:
		return run_tool(tool_name, args, execute_mutation=False)
	except Exception as exc:
		return {
			"status": "error",
			"message": cstr(exc) or _("Tool failed."),
		}


def run_cached_read_tool(tool_name, args):
	cache_key = build_read_tool_cache_key(tool_name, args)
	cached_result = get_cached_read_tool_result(cache_key)
	if cached_result:
		cached_result["cache_status"] = "hit"
		return cached_result

	result = READ_TOOLS[tool_name](args)
	if should_cache_read_tool_result(result):
		set_cached_read_tool_result(cache_key, result)
	return result


def should_cache_read_tool_result(result):
	return isinstance(result, dict) and result.get("status") == "ok"


def build_read_tool_cache_key(tool_name, args, scope=None):
	payload = {
		"scope": scope or get_read_tool_cache_scope(),
		"tool": tool_name,
		"args": normalize_value(args or {}),
	}
	digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
	return f"bookkeeping_agent:read_tool:v1:{digest}"


def get_read_tool_cache_scope():
	site = ""
	user = ""
	roles = []

	try:
		site = cstr(getattr(frappe.local, "site", ""))
	except Exception:
		site = ""

	try:
		user = cstr(getattr(frappe.session, "user", ""))
	except Exception:
		user = ""

	try:
		roles = sorted(frappe.get_roles(user) if user else [])
	except Exception:
		roles = []

	return {"site": site, "user": user, "roles": roles}


def get_cached_read_tool_result(cache_key):
	try:
		value = frappe.cache().get_value(cache_key)
	except Exception:
		return None

	if not value:
		return None

	if isinstance(value, bytes):
		value = value.decode("utf-8")

	try:
		result = frappe.parse_json(value) if isinstance(value, str) else value
	except Exception:
		return None

	return result if isinstance(result, dict) else None


def set_cached_read_tool_result(cache_key, result):
	try:
		frappe.cache().set_value(cache_key, to_json(result), expires_in_sec=READ_TOOL_CACHE_TTL_SECONDS)
	except Exception:
		return


def new_agent_usage_metrics():
	return {
		"response_count": 0,
		"input_tokens": 0,
		"output_tokens": 0,
		"total_tokens": 0,
		"cached_tokens": 0,
		"reasoning_tokens": 0,
		"tool_calls": 0,
		"tool_cache_hits": 0,
		"tool_result_bytes": 0,
	}


def record_response_usage(metrics, response):
	usage = extract_response_token_usage(response)
	if not usage:
		return

	metrics["response_count"] += 1
	for field in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"):
		metrics[field] += cint(usage.get(field) or 0)


def record_tool_usage(metrics, result):
	metrics["tool_calls"] += 1
	if isinstance(result, dict) and result.get("cache_status") == "hit":
		metrics["tool_cache_hits"] += 1
	metrics["tool_result_bytes"] += len(to_json(result))


def extract_response_token_usage(response):
	response = normalize_model_object(response)
	usage = normalize_model_object(response.get("usage") if isinstance(response, dict) else None)
	if not isinstance(usage, dict) or not usage:
		return {}

	input_details = normalize_model_object(
		usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
	)
	output_details = normalize_model_object(
		usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
	)

	input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
	output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))
	total_tokens = usage.get("total_tokens") or cint(input_tokens) + cint(output_tokens)

	return {
		"input_tokens": cint(input_tokens),
		"output_tokens": cint(output_tokens),
		"total_tokens": cint(total_tokens),
		"cached_tokens": cint(input_details.get("cached_tokens") or 0),
		"reasoning_tokens": cint(output_details.get("reasoning_tokens") or 0),
	}


def normalize_model_object(value):
	if hasattr(value, "model_dump"):
		return value.model_dump(exclude_none=True)
	return value


def log_agent_usage(run_id, model, metrics):
	try:
		frappe.logger("bookkeeping_agent").info(
			to_json({"run_id": run_id, "model": model, "usage": metrics})
		)
	except Exception:
		return


def stable_json(value):
	return json.dumps(normalize_value(value), sort_keys=True, separators=(",", ":"), default=str)


def ensure_agent_access():
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to use the Bookkeeping Agent."), frappe.PermissionError)

	if not (set(frappe.get_roles(frappe.session.user)) & AGENT_ROLES):
		frappe.throw(_("You need an Accounts role to use the Bookkeeping Agent."), frappe.PermissionError)


def get_openai_client():
	api_key = get_conf_value("openai_api_key", "OPENAI_API_KEY")
	if not api_key:
		frappe.throw(_("Set openai_api_key in site_config.json or OPENAI_API_KEY in the environment."))

	try:
		from openai import OpenAI
	except ImportError:
		frappe.throw(_("The openai Python package is required for the Bookkeeping Agent."))

	options = {"api_key": api_key}
	base_url = get_conf_value("openai_base_url", "OPENAI_BASE_URL")
	if base_url:
		options["base_url"] = base_url

	return OpenAI(**options)


def get_agent_model():
	return get_conf_value("erpnext_ai_agent_model", "ERPNEXT_AI_AGENT_MODEL") or DEFAULT_MODEL


def get_conf_value(site_config_key, env_key):
	value = frappe.conf.get(site_config_key) or os.environ.get(env_key)
	return cstr(value).strip() if value else None


def get_system_prompt():
	return """
You are an embedded ERPNext bookkeeping assistant.

Use tools for ERPNext data. Never invent records, IDs, account names, or amounts.
Read tools may run immediately. Write tools only prepare pending actions for user confirmation.
For invoices, payments, journal entries, submit, or cancel, ask for missing company, party, item, account, date, or amount instead of guessing.
When the user explicitly asks you to remember a stable preference, such as default company, default currency, or a short bookkeeping preference note, prepare remember_user_memory for confirmation. Never infer or store memory from repeated behavior alone.
Use provided user memory only when the current request omits that value. Current user instructions override memory.
Use provided admin playbook and organization memory as admin-reviewed guidance when relevant. Current user instructions and fresh ERPNext records override stale memory.
Prefer compact read results: use ids or summary result_mode first, and use full only when a requested field is missing or the user asks for details.
Do not repeat the same tool call with identical arguments.
After a tool returns needs_input or a concise result, answer the user instead of continuing to call tools.
For broad questions, prefer one search or report tool call, then summarize.
Keep responses concise and include document links only when the tool result provides them.
Do not expose implementation details, Python tracebacks, or raw oversized payloads.
"""


def build_model_input(messages):
	input_items = []
	memory_context = build_user_memory_context()
	if memory_context:
		input_items.append({"role": "user", "content": memory_context})

	admin_memory_context = build_admin_memory_context(messages)
	if admin_memory_context:
		input_items.append({"role": "user", "content": admin_memory_context})

	for message in compact_messages_for_model(messages):
		role = message["role"]
		if role == "assistant":
			role = "assistant"
		else:
			role = "user"
		input_items.append({"role": role, "content": message["content"]})

	return input_items


def build_user_memory_context(user=None):
	memories = get_enabled_user_memories(user)
	lines = [
		"Bookkeeping Agent user memory. These are explicit preferences for the logged-in user. Use them only when the current request omits the value; current user instructions override memory.",
	]

	for memory in memories:
		memory_key = memory.get("memory_key")
		memory_value = cstr(memory.get("memory_value") or memory.get("reference_name")).strip()
		config = USER_MEMORY_KEYS.get(memory_key)
		if not config or not memory_value:
			continue

		lines.append(f"- {config['context_label']}: {memory_value}")

	return "\n".join(lines) if len(lines) > 1 else ""


def get_enabled_user_memories(user=None):
	user = cstr(user or getattr(frappe.session, "user", "")).strip()
	if not user or user == "Guest":
		return []

	try:
		if not frappe.db.table_exists(MEMORY_DOCTYPE):
			return []
		return frappe.get_all(
			MEMORY_DOCTYPE,
			filters={"user": user, "enabled": 1},
			fields=[
				"name",
				"memory_key",
				"memory_value",
				"reference_doctype",
				"reference_name",
			],
			limit=MAX_USER_MEMORIES,
			order_by="modified desc",
		)
	except Exception:
		return []


def build_admin_memory_context(messages, user=None):
	request_text = get_current_user_request_text(messages)
	default_company = get_default_company_from_user_memory(user)
	playbook_memories = select_relevant_admin_memories(
		get_enabled_playbook_memories(default_company),
		request_text,
		"guidance",
		MAX_PLAYBOOK_MEMORIES,
	)
	organization_memories = []
	if default_company:
		organization_memories = select_relevant_admin_memories(
			get_enabled_organization_memories(default_company),
			request_text,
			"memory",
			MAX_ORGANIZATION_MEMORIES,
		)

	lines = []
	if playbook_memories:
		lines.append(
			"Bookkeeping Agent admin playbook. These are admin-reviewed product and accounting rules. Apply them when relevant; current user instructions and ERPNext records remain authoritative."
		)
		for memory in playbook_memories:
			lines.extend(format_admin_memory(memory, "guidance"))

	if organization_memories:
		if lines:
			lines.append("")
		lines.append(
			f"Bookkeeping Agent organization memory for {default_company}. These are admin-reviewed facts about this organization. Use them when relevant; current user instructions and fresh ERPNext records override stale memory."
		)
		for memory in organization_memories:
			lines.extend(format_admin_memory(memory, "memory"))

	return "\n".join(lines).strip()


def get_current_user_request_text(messages):
	for message in reversed(messages or []):
		if message.get("role") != "user":
			continue
		content = message.get("content")
		if isinstance(content, str):
			return content
		return to_json(content)
	return ""


def get_default_company_from_user_memory(user=None):
	for memory in get_enabled_user_memories(user):
		if memory.get("memory_key") != "default_company":
			continue
		company = cstr(memory.get("memory_value") or memory.get("reference_name")).strip()
		if company:
			return company
	return ""


def get_enabled_playbook_memories(company=None):
	try:
		if not frappe.db.table_exists(PLAYBOOK_MEMORY_DOCTYPE):
			return []
		memories = frappe.get_all(
			PLAYBOOK_MEMORY_DOCTYPE,
			filters={"enabled": 1},
			fields=[
				"name",
				"title",
				"guidance",
				"suggested_prompt",
				"trigger_terms",
				"applies_to_doctype",
				"scope",
				"company",
				"priority",
			],
			limit=ADMIN_MEMORY_FETCH_LIMIT,
			order_by="priority asc, modified desc",
		)
	except Exception:
		return []

	company = cstr(company).strip()
	filtered_memories = []
	for memory in memories:
		scope = cstr(memory.get("scope")).strip() or "Global"
		if scope == "Global" or (scope == "Company" and company and memory.get("company") == company):
			filtered_memories.append(memory)
	return filtered_memories


def get_enabled_organization_memories(company=None):
	company = cstr(company).strip()
	if not company:
		return []

	try:
		if not frappe.db.table_exists(ORGANIZATION_MEMORY_DOCTYPE):
			return []
		return frappe.get_all(
			ORGANIZATION_MEMORY_DOCTYPE,
			filters={"enabled": 1, "company": company},
			fields=[
				"name",
				"company",
				"title",
				"memory",
				"trigger_terms",
				"applies_to_doctype",
				"priority",
			],
			limit=ADMIN_MEMORY_FETCH_LIMIT,
			order_by="priority asc, modified desc",
		)
	except Exception:
		return []


def select_relevant_admin_memories(memories, request_text, body_field, limit):
	scored_memories = []
	for index, memory in enumerate(memories or []):
		score = get_admin_memory_relevance_score(memory, request_text, body_field)
		if score <= 0:
			continue
		scored_memories.append((cint(memory.get("priority") or 100), -score, index, memory))

	scored_memories.sort()
	return [memory for _priority, _score, _index, memory in scored_memories[:limit]]


def get_admin_memory_relevance_score(memory, request_text, body_field):
	request = normalize_memory_search_text(request_text)
	if not request:
		return 0

	score = 0
	terms = split_memory_terms(memory.get("trigger_terms"))
	applies_to_doctype = cstr(memory.get("applies_to_doctype")).strip()
	if applies_to_doctype:
		terms.append(applies_to_doctype)

	for term in terms:
		normalized_term = normalize_memory_search_text(term)
		if normalized_term and normalized_term in request:
			score += 2

	if terms:
		return score

	request_words = {word for word in request.split() if len(word) >= 4}
	memory_words = {
		word
		for word in normalize_memory_search_text(
			" ".join([cstr(memory.get("title")), cstr(memory.get(body_field))])
		).split()
		if len(word) >= 4
	}
	return len(request_words & memory_words)


def split_memory_terms(raw_terms):
	terms = []
	for term in cstr(raw_terms).replace("\n", ",").split(","):
		term = cstr(term).strip()
		if term:
			terms.append(term)
	return terms


def normalize_memory_search_text(value):
	return " ".join(cstr(value).lower().split())


def format_admin_memory(memory, body_field):
	title = cstr(memory.get("title")).strip() or cstr(memory.get("name")).strip()
	body = compact_text(memory.get(body_field), 700)
	lines = [f"- {title}: {body}"]
	suggested_prompt = compact_text(memory.get("suggested_prompt"), 260)
	if suggested_prompt:
		lines.append(f"  Suggested prompt: {suggested_prompt}")
	return lines


def compact_messages_for_model(messages):
	if len(messages) <= MAX_RECENT_MESSAGES:
		return messages

	older_messages = messages[:-MAX_RECENT_MESSAGES]
	recent_messages = messages[-MAX_RECENT_MESSAGES:]
	summary = summarize_conversation_messages(older_messages)
	if not summary:
		return recent_messages

	return [{"role": "user", "content": summary}, *recent_messages]


def summarize_conversation_messages(messages):
	if not messages:
		return ""

	selected_messages = select_messages_for_summary(messages)
	lines = [
		"Earlier conversation summary for context only. Prefer current user instructions and fresh ERPNext tool results.",
	]
	for message in selected_messages:
		if message.get("role") == "summary_separator":
			lines.append("- ...")
			continue
		label = "Assistant" if message.get("role") == "assistant" else "User"
		content = compact_text(message.get("content"), 220)
		if content:
			lines.append(f"- {label}: {content}")

	summary = "\n".join(lines)
	return compact_text(summary, MAX_CONVERSATION_SUMMARY_CHARS)


def select_messages_for_summary(messages):
	limit = MAX_CONVERSATION_SUMMARY_HEAD + MAX_CONVERSATION_SUMMARY_TAIL
	if len(messages) <= limit:
		return messages

	return [
		*messages[:MAX_CONVERSATION_SUMMARY_HEAD],
		{"role": "summary_separator", "content": ""},
		*messages[-MAX_CONVERSATION_SUMMARY_TAIL:],
	]


def compact_text(value, limit):
	text = " ".join(cstr(value).split())
	if len(text) <= limit:
		return text
	return f"{text[: limit - 3]}..."


def normalize_messages(messages):
	if not isinstance(messages, list):
		frappe.throw(_("Messages must be a list."))

	normalized = []
	for message in messages:
		if not isinstance(message, dict):
			continue
		role = message.get("role")
		content = cstr(message.get("content")).strip()
		if role not in {"user", "assistant"} or not content:
			continue
		normalized.append({"role": role, "content": content[:8000]})

	return normalized


def get_stream_value(obj, key, default=None):
	if isinstance(obj, dict):
		return obj.get(key, default)
	return getattr(obj, key, default)


def get_response_output(response):
	if isinstance(response, dict):
		return response.get("output") or []
	return getattr(response, "output", []) or []


def extract_function_calls(response):
	tool_calls = []
	for item in get_response_output(response):
		if get_stream_value(item, "type") != "function_call":
			continue
		arguments = get_stream_value(item, "arguments", "{}") or "{}"
		try:
			arguments = json.loads(arguments)
		except Exception:
			arguments = {}

		tool_calls.append(
			{
				"name": get_stream_value(item, "name", ""),
				"call_id": get_stream_value(item, "call_id", ""),
				"arguments": arguments,
			}
		)

	return tool_calls


def response_output_as_input(response):
	output = []
	for item in get_response_output(response):
		if hasattr(item, "model_dump"):
			output.append(item.model_dump(exclude_none=True))
		elif isinstance(item, dict):
			output.append(item)
	return output


def get_response_text(response):
	if not response:
		return ""

	output_text = cstr(getattr(response, "output_text", "") or "").strip()
	if isinstance(response, dict):
		output_text = cstr(response.get("output_text") or "").strip()
	if output_text:
		return output_text

	parts = []
	for item in get_response_output(response):
		for content in get_stream_value(item, "content", []) or []:
			text = get_stream_value(content, "text")
			if text:
				parts.append(cstr(text))
	return "\n".join(parts).strip()


def get_safe_agent_error_message(exc):
	message = cstr(getattr(exc, "message", None) or exc).strip()
	if not message:
		return _("Unable to complete the AI request. Check the OpenAI configuration and try again.")

	message = message.replace("\n", " ")
	if len(message) > 350:
		message = f"{message[:350]}..."

	return _("Unable to complete the AI request: {0}").format(message)


def publish_agent_event(run_id, event, payload=None):
	run_id = cstr(run_id).strip()
	if not run_id:
		return

	data = dict(payload or {})
	data["event"] = event

	try:
		frappe.publish_realtime(
			f"bookkeeping_agent_run:{run_id}",
			data,
			user=frappe.session.user,
			after_commit=False,
		)
	except Exception:
		pass


def build_tool_event(tool_name, args, status, result=None):
	result = result or {}
	status = cstr(status or "ok")
	message = cstr(result.get("message") or (_("Running") if status == "running" else status.replace("_", " ").title()))

	return {
		"id": get_tool_call_signature(tool_name, args),
		"tool": tool_name,
		"label": get_tool_label(tool_name),
		"status": status,
		"message": message,
		"arguments": summarize_tool_arguments(args),
		"pending_action": bool(result.get("pending_action")),
	}


def get_tool_call_signature(tool_name, args):
	return "{0}:{1}".format(
		cstr(tool_name),
		json.dumps(normalize_value(args or {}), sort_keys=True, default=str),
	)


def get_tool_label(tool_name):
	labels = {
		"search_records": _("Search records"),
		"get_record": _("Get record"),
		"run_bookkeeping_report": _("Run report"),
		"upsert_party": _("Prepare party"),
		"update_draft_document": _("Prepare draft update"),
		"create_sales_invoice_draft": _("Prepare sales invoice"),
		"create_purchase_invoice_draft": _("Prepare purchase invoice"),
		"create_payment_entry_draft": _("Prepare payment entry"),
		"create_journal_entry_draft": _("Prepare journal entry"),
		"submit_or_cancel_document": _("Prepare submit/cancel"),
	}
	return labels.get(tool_name, cstr(tool_name).replace("_", " ").title())


def summarize_tool_arguments(args):
	if not isinstance(args, dict):
		return {}

	summary = {}
	key_map = {
		"report_name": "report",
	}
	safe_keys = [
		"doctype",
		"name",
		"report_name",
		"query",
		"company",
		"party_type",
		"party",
		"customer",
		"supplier",
		"action",
		"from_date",
		"to_date",
		"fiscal_year",
		"limit",
		"posting_date",
		"due_date",
		"payment_type",
		"paid_amount",
		"received_amount",
		"mode_of_payment",
	]

	for key in safe_keys:
		if args.get(key) not in (None, ""):
			summary[key_map.get(key, key)] = summarize_preview_value(args.get(key))

	fields = args.get("fields")
	if isinstance(fields, dict):
		for key in safe_keys:
			if fields.get(key) not in (None, "") and key_map.get(key, key) not in summary:
				summary[key_map.get(key, key)] = summarize_preview_value(fields.get(key))
		for table_field in ("items", "accounts", "references"):
			if isinstance(fields.get(table_field), list):
				summary[table_field] = _("{0} row(s)").format(len(fields.get(table_field)))

	filters = args.get("filters")
	if isinstance(filters, dict):
		summary["filters"] = {
			cstr(key): summarize_preview_value(value) for key, value in list(filters.items())[:6]
		}

	return summary


def summarize_preview_value(value):
	value = normalize_value(value)
	if isinstance(value, list):
		if len(value) > 4:
			return [_("List with {0} item(s)").format(len(value))]
		return [summarize_preview_value(item) for item in value]
	if isinstance(value, dict):
		return {cstr(key): summarize_preview_value(item) for key, item in list(value.items())[:6]}

	text = cstr(value)
	if len(text) > 120:
		return f"{text[:120]}..."
	return value


def get_openai_tool_schemas():
	return [
		{
			"type": "function",
			"name": "search_records",
			"description": "Search allowed ERPNext bookkeeping and support records.",
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string", "enum": sorted(READ_DOCTYPES)},
					"query": {"type": "string"},
					"filters": {"type": "object"},
					"fields": {"type": "array", "items": {"type": "string"}},
					"limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_LIMIT},
					"result_mode": {"type": "string", "enum": ["ids", "summary", "full"]},
				},
				"required": ["doctype"],
				"additionalProperties": False,
			},
		},
		{
			"type": "function",
			"name": "get_record",
			"description": "Get one allowed ERPNext record by DocType and name.",
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string", "enum": sorted(READ_DOCTYPES)},
					"name": {"type": "string"},
					"result_mode": {"type": "string", "enum": ["summary", "full"]},
				},
				"required": ["doctype", "name"],
				"additionalProperties": False,
			},
		},
		{
			"type": "function",
			"name": "run_bookkeeping_report",
			"description": "Run a supported ERPNext bookkeeping report.",
			"parameters": {
				"type": "object",
				"properties": {
					"report_name": {"type": "string", "enum": sorted(REPORTS)},
					"filters": {"type": "object"},
					"limit": {"type": "integer", "minimum": 1, "maximum": MAX_REPORT_ROWS},
					"result_mode": {"type": "string", "enum": ["summary", "full"]},
				},
				"required": ["report_name", "filters"],
				"additionalProperties": False,
			},
		},
		{
			"type": "function",
			"name": "upsert_party",
			"description": "Prepare creation or update of a Customer or Supplier.",
			"parameters": mutation_schema(
				{
					"party_type": {"type": "string", "enum": sorted(PARTY_DOCTYPES)},
					"name": {"type": "string"},
					"fields": {"type": "object"},
				},
				["party_type", "fields"],
			),
		},
		{
			"type": "function",
			"name": "remember_user_memory",
			"description": "Prepare storing an explicit, stable preference for the logged-in user. Use only when the user explicitly asks you to remember it.",
			"parameters": mutation_schema(
				{
					"memory_key": {"type": "string", "enum": sorted(USER_MEMORY_KEYS)},
					"memory_value": {"type": "string"},
				},
				["memory_key", "memory_value"],
			),
		},
		{
			"type": "function",
			"name": "update_draft_document",
			"description": "Prepare an update to a draft bookkeeping document only.",
			"parameters": mutation_schema(
				{
					"doctype": {"type": "string", "enum": sorted(DRAFT_DOCUMENT_DOCTYPES)},
					"name": {"type": "string"},
					"fields": {"type": "object"},
				},
				["doctype", "name", "fields"],
			),
		},
		{
			"type": "function",
			"name": "create_sales_invoice_draft",
			"description": "Prepare a draft Sales Invoice.",
			"parameters": mutation_schema({"fields": {"type": "object"}}, ["fields"]),
		},
		{
			"type": "function",
			"name": "create_purchase_invoice_draft",
			"description": "Prepare a draft Purchase Invoice.",
			"parameters": mutation_schema({"fields": {"type": "object"}}, ["fields"]),
		},
		{
			"type": "function",
			"name": "create_payment_entry_draft",
			"description": "Prepare a draft Payment Entry.",
			"parameters": mutation_schema({"fields": {"type": "object"}}, ["fields"]),
		},
		{
			"type": "function",
			"name": "create_journal_entry_draft",
			"description": "Prepare a balanced draft Journal Entry.",
			"parameters": mutation_schema({"fields": {"type": "object"}}, ["fields"]),
		},
		{
			"type": "function",
			"name": "submit_or_cancel_document",
			"description": "Prepare explicit submit or cancel for an allowed bookkeeping document.",
			"parameters": mutation_schema(
				{
					"doctype": {"type": "string", "enum": sorted(DRAFT_DOCUMENT_DOCTYPES)},
					"name": {"type": "string"},
					"action": {"type": "string", "enum": ["submit", "cancel"]},
				},
				["doctype", "name", "action"],
			),
		},
	]


def mutation_schema(properties, required):
	return {
		"type": "object",
		"properties": properties,
		"required": required,
		"additionalProperties": False,
	}


def get_result_mode(args, allowed_modes, default):
	mode = cstr(args.get("result_mode")).strip().lower()
	if not mode:
		return default
	if mode not in allowed_modes:
		frappe.throw(_("Result mode must be one of: {0}").format(", ".join(sorted(allowed_modes))))
	return mode


def get_search_result_fields(doctype, args, result_mode):
	if result_mode == "ids":
		return ["name"]
	if result_mode == "summary":
		return clean_requested_fields(doctype, DEFAULT_LIST_FIELDS.get(doctype, ["name"]))
	return clean_requested_fields(doctype, args.get("fields") or DEFAULT_LIST_FIELDS.get(doctype, ["name"]))


def shape_search_records(doctype, records, result_mode):
	if result_mode == "ids":
		return [
			{"name": record.get("name"), "url": get_doc_url(doctype, record.get("name"))}
			for record in records
			if record.get("name")
		]
	return [normalize_value(record) for record in records]


def get_report_row_limit(args, result_mode):
	default_limit = SUMMARY_REPORT_ROWS if result_mode == "summary" else MAX_REPORT_ROWS
	return min(max(cint(args.get("limit") or default_limit), 1), MAX_REPORT_ROWS)


def tool_search_records(args):
	doctype = require_choice(args.get("doctype"), READ_DOCTYPES, _("DocType"))
	ensure_doctype_permission(doctype, "read")

	limit = min(max(cint(args.get("limit") or 10), 1), MAX_SEARCH_LIMIT)
	result_mode = get_result_mode(args, {"ids", "summary", "full"}, "summary")
	fields = get_search_result_fields(doctype, args, result_mode)
	filters = clean_filters(doctype, args.get("filters") or {})
	or_filters = build_search_filters(doctype, args.get("query"))

	records = frappe.get_list(
		doctype,
		filters=filters,
		or_filters=or_filters,
		fields=fields,
		page_length=limit,
		order_by=get_default_order_by(doctype),
	)

	return {
		"status": "ok",
		"message": _("Found {0} {1} record(s).").format(len(records), doctype),
		"doctype": doctype,
		"result_mode": result_mode,
		"records": shape_search_records(doctype, records, result_mode),
	}


def tool_get_record(args):
	doctype = require_choice(args.get("doctype"), READ_DOCTYPES, _("DocType"))
	name = require_string(args.get("name"), _("Name"))
	result_mode = get_result_mode(args, {"summary", "full"}, "summary")

	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")

	result = {
		"status": "ok",
		"message": _("Loaded {0} {1}.").format(doctype, name),
		"result_mode": result_mode,
		"summary": summarize_doc(doc),
	}
	if result_mode == "full":
		result["record"] = sanitize_doc(doc)

	return result


def tool_run_bookkeeping_report(args):
	report_name = require_choice(args.get("report_name"), set(REPORTS), _("Report"))
	filters = parse_json_value(args.get("filters"), {})
	if not isinstance(filters, dict):
		frappe.throw(_("Report filters must be an object."))

	report_config = REPORTS[report_name]
	missing = [field for field in report_config["required_filters"] if not filters.get(field)]
	if missing:
		return needs_input(_("Missing required report filter(s): {0}.").format(", ".join(missing)), missing)

	ensure_doctype_permission(report_config["permission_doctype"], "read")

	module = importlib.import_module(report_config["module"])
	result = module.execute(frappe._dict(filters))
	columns = result[0] if len(result) > 0 else []
	rows = list(result[1] or []) if len(result) > 1 else []
	result_mode = get_result_mode(args, {"summary", "full"}, "summary")
	limit = get_report_row_limit(args, result_mode)

	return {
		"status": "ok",
		"message": _("Ran {0}.").format(report_name),
		"report_name": report_name,
		"result_mode": result_mode,
		"columns": normalize_columns(columns),
		"rows": normalize_rows(rows[:limit]),
		"row_count": len(rows),
		"truncated": len(rows) > limit,
	}


def prepare_pending_action(tool_name, args):
	validation = validate_pending_action(tool_name, args)
	if validation:
		return validation

	return {
		"status": "pending_confirmation",
		"message": _("Prepared {0} for confirmation.").format(tool_name.replace("_", " ")),
		"pending_action": {
			"tool": tool_name,
			"args": args,
			"summary": summarize_action(tool_name, args),
			"impact": get_action_impact(tool_name),
		},
	}


def validate_pending_action(tool_name, args):
	if tool_name == "upsert_party":
		party_type = args.get("party_type")
		fields = args.get("fields") or {}
		if party_type not in PARTY_DOCTYPES:
			return needs_input(_("Choose Customer or Supplier."), ["party_type"])
		if not isinstance(fields, dict):
			return needs_input(_("Provide party fields."), ["fields"])
		if party_type == "Customer" and not (args.get("name") or fields.get("customer_name")):
			return needs_input(_("Provide the customer name."), ["customer_name"])
		if party_type == "Supplier" and not (args.get("name") or fields.get("supplier_name")):
			return needs_input(_("Provide the supplier name."), ["supplier_name"])

	if tool_name == "remember_user_memory":
		memory_key = args.get("memory_key")
		if memory_key not in USER_MEMORY_KEYS:
			return needs_input(_("Choose a supported memory key."), ["memory_key"])
		memory_value = cstr(args.get("memory_value")).strip()
		if not memory_value:
			return needs_input(_("Provide the value to remember."), ["memory_value"])
		error = get_user_memory_value_error(memory_key, memory_value)
		if error:
			return needs_input(error, ["memory_value"])

	if tool_name == "update_draft_document":
		missing = [field for field in ["doctype", "name", "fields"] if not args.get(field)]
		if missing:
			return needs_input(_("Missing draft update field(s): {0}.").format(", ".join(missing)), missing)

	if tool_name in {
		"create_sales_invoice_draft",
		"create_purchase_invoice_draft",
		"create_payment_entry_draft",
		"create_journal_entry_draft",
	}:
		fields = args.get("fields") or {}
		if not isinstance(fields, dict):
			return needs_input(_("Provide document fields."), ["fields"])
		missing = get_missing_create_fields(tool_name, fields)
		if missing:
			return needs_input(_("Missing required field(s): {0}.").format(", ".join(missing)), missing)
		if tool_name == "create_journal_entry_draft":
			balance_error = validate_journal_entry_balance(fields)
			if balance_error:
				return needs_input(balance_error, ["accounts"])

	if tool_name == "submit_or_cancel_document":
		missing = [field for field in ["doctype", "name", "action"] if not args.get(field)]
		if missing:
			return needs_input(_("Missing submit/cancel field(s): {0}.").format(", ".join(missing)), missing)

	return None


def get_missing_create_fields(tool_name, fields):
	required = {
		"create_sales_invoice_draft": ["company", "customer", "items"],
		"create_purchase_invoice_draft": ["company", "supplier", "items"],
		"create_payment_entry_draft": ["company", "payment_type", "paid_from", "paid_to", "paid_amount", "received_amount"],
		"create_journal_entry_draft": ["company", "accounts"],
	}
	return [field for field in required.get(tool_name, []) if not fields.get(field)]


def tool_upsert_party(args):
	party_type = require_choice(args.get("party_type"), PARTY_DOCTYPES, _("Party Type"))
	fields = require_dict(args.get("fields"), _("Fields"))
	name = cstr(args.get("name")).strip()
	fields = clean_payload(party_type, fields, allow_tables=False)

	title_field = "customer_name" if party_type == "Customer" else "supplier_name"
	if not fields.get(title_field) and name:
		fields[title_field] = name

	if name and frappe.db.exists(party_type, name):
		doc = frappe.get_doc(party_type, name)
		doc.check_permission("write")
		doc.update(fields)
		doc.save()
		message = _("Updated {0} {1}.").format(party_type, doc.name)
	else:
		ensure_doctype_permission(party_type, "create")
		doc = frappe.get_doc({"doctype": party_type, **fields})
		doc.insert()
		message = _("Created {0} {1}.").format(party_type, doc.name)

	return completed_doc_result(message, doc)


def tool_remember_user_memory(args):
	memory_key, memory_value, config = normalize_user_memory_args(args)
	reference_doctype = cstr(config.get("reference_doctype")).strip()
	reference_name = ""

	if reference_doctype:
		ensure_doctype_permission(reference_doctype, "read")
		if not frappe.db.exists(reference_doctype, memory_value):
			frappe.throw(_("{0} {1} does not exist.").format(reference_doctype, frappe.bold(memory_value)))
		reference_name = memory_value

	user = cstr(getattr(frappe.session, "user", "")).strip()
	if not user or user == "Guest":
		frappe.throw(_("Please log in to use the Bookkeeping Agent."), frappe.PermissionError)

	memory_name = build_user_memory_name(user, memory_key)
	fields = {
		"memory_name": memory_name,
		"user": user,
		"memory_key": memory_key,
		"memory_value": memory_value,
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"enabled": 1,
		"source": MEMORY_SOURCE_EXPLICIT,
	}

	if frappe.db.exists(MEMORY_DOCTYPE, memory_name):
		doc = frappe.get_doc(MEMORY_DOCTYPE, memory_name)
		doc.update(fields)
		doc.save(ignore_permissions=True)
		message = _("Updated remembered {0}: {1}.").format(config["label"], memory_value)
	else:
		doc = frappe.get_doc({"doctype": MEMORY_DOCTYPE, **fields})
		doc.insert(ignore_permissions=True)
		message = _("Remembered {0}: {1}.").format(config["label"], memory_value)

	return {"status": "completed", "message": message, "summary": summarize_memory_doc(doc)}


def normalize_user_memory_args(args):
	memory_key = require_choice(args.get("memory_key"), set(USER_MEMORY_KEYS), _("Memory Key"))
	memory_value = require_string(args.get("memory_value"), _("Memory Value"))
	error = get_user_memory_value_error(memory_key, memory_value)
	if error:
		frappe.throw(error)
	return memory_key, memory_value, USER_MEMORY_KEYS[memory_key]


def get_user_memory_value_error(memory_key, memory_value):
	config = USER_MEMORY_KEYS.get(memory_key)
	if not config:
		return _("Choose a supported memory key.")

	max_length = cint(config.get("max_length") or 0)
	if max_length and len(cstr(memory_value)) > max_length:
		return _("Memory value must be {0} characters or fewer.").format(max_length)

	return None


def build_user_memory_name(user, memory_key):
	digest = hashlib.sha1(f"{user}:{memory_key}".encode("utf-8")).hexdigest()[:12]
	return f"BAM-{digest}"


def summarize_memory_doc(doc):
	return {
		"doctype": doc.doctype,
		"name": doc.name,
		"user": doc.user,
		"memory_key": doc.memory_key,
		"memory_value": doc.memory_value,
		"reference_doctype": doc.reference_doctype,
		"reference_name": doc.reference_name,
	}


def tool_update_draft_document(args):
	doctype = require_choice(args.get("doctype"), DRAFT_DOCUMENT_DOCTYPES, _("DocType"))
	name = require_string(args.get("name"), _("Name"))
	fields = clean_payload(doctype, require_dict(args.get("fields"), _("Fields")), allow_tables=True)

	doc = frappe.get_doc(doctype, name)
	doc.check_permission("write")
	if cint(doc.docstatus) != 0:
		frappe.throw(_("Only draft documents can be updated by the Bookkeeping Agent."))

	doc.update(fields)
	doc.save()

	return completed_doc_result(_("Updated draft {0} {1}.").format(doctype, doc.name), doc)


def tool_create_sales_invoice_draft(args):
	return create_draft_document("Sales Invoice", args)


def tool_create_purchase_invoice_draft(args):
	return create_draft_document("Purchase Invoice", args)


def tool_create_payment_entry_draft(args):
	return create_draft_document("Payment Entry", args)


def tool_create_journal_entry_draft(args):
	fields = require_dict(args.get("fields"), _("Fields"))
	balance_error = validate_journal_entry_balance(fields)
	if balance_error:
		frappe.throw(balance_error)
	return create_draft_document("Journal Entry", args)


def create_draft_document(doctype, args):
	ensure_doctype_permission(doctype, "create")

	fields = clean_payload(doctype, require_dict(args.get("fields"), _("Fields")), allow_tables=True)
	doc = frappe.get_doc({"doctype": doctype, **fields})
	doc.docstatus = 0
	doc.insert()

	return completed_doc_result(_("Created draft {0} {1}.").format(doctype, doc.name), doc)


def tool_submit_or_cancel_document(args):
	doctype = require_choice(args.get("doctype"), DRAFT_DOCUMENT_DOCTYPES, _("DocType"))
	name = require_string(args.get("name"), _("Name"))
	action = require_choice(args.get("action"), {"submit", "cancel"}, _("Action"))

	doc = frappe.get_doc(doctype, name)
	doc.check_permission(action)

	if action == "submit":
		if cint(doc.docstatus) != 0:
			frappe.throw(_("Only draft documents can be submitted."))
		doc.submit()
		message = _("Submitted {0} {1}.").format(doctype, doc.name)
	else:
		if cint(doc.docstatus) != 1:
			frappe.throw(_("Only submitted documents can be cancelled."))
		doc.cancel()
		message = _("Cancelled {0} {1}.").format(doctype, doc.name)

	return completed_doc_result(message, doc)


def completed_doc_result(message, doc):
	return {
		"status": "completed",
		"message": message,
		"summary": summarize_doc(doc),
	}


def validate_journal_entry_balance(fields):
	accounts = fields.get("accounts") or []
	if not isinstance(accounts, list) or not accounts:
		return _("Journal Entry needs at least one account row.")

	total_debit = 0
	total_credit = 0
	for row in accounts:
		if not isinstance(row, dict):
			return _("Journal Entry account rows must be objects.")
		total_debit += flt(row.get("debit_in_account_currency") or row.get("debit") or 0)
		total_credit += flt(row.get("credit_in_account_currency") or row.get("credit") or 0)

	if flt(total_debit, 2) != flt(total_credit, 2):
		return _("Journal Entry total debit must equal total credit.")

	return None


def clean_payload(doctype, payload, allow_tables=False):
	if not isinstance(payload, dict):
		frappe.throw(_("Expected an object for {0}.").format(doctype))

	meta = frappe.get_meta(doctype)
	field_map = {field.fieldname: field for field in meta.fields if field.fieldname}
	cleaned = {}

	for fieldname, value in payload.items():
		if fieldname in SYSTEM_FIELDS or cstr(fieldname).startswith("_"):
			frappe.throw(_("Field {0} cannot be set by the Bookkeeping Agent.").format(frappe.bold(fieldname)))

		field = field_map.get(fieldname)
		if not field:
			frappe.throw(_("Field {0} is not valid for {1}.").format(frappe.bold(fieldname), doctype))

		if field.fieldtype in BLOCKED_FIELDTYPES:
			frappe.throw(_("Field {0} cannot be updated by the Bookkeeping Agent.").format(frappe.bold(fieldname)))

		if field.fieldtype == "Table":
			if not allow_tables:
				frappe.throw(_("Child table field {0} is not allowed here.").format(frappe.bold(fieldname)))
			if not isinstance(value, list):
				frappe.throw(_("Child table field {0} must be a list.").format(frappe.bold(fieldname)))
			cleaned[fieldname] = [clean_payload(field.options, row, allow_tables=False) for row in value]
		else:
			cleaned[fieldname] = value

	return cleaned


def clean_requested_fields(doctype, fields):
	if not isinstance(fields, list):
		fields = DEFAULT_LIST_FIELDS.get(doctype, ["name"])

	meta = frappe.get_meta(doctype)
	valid_fields = {"name", "docstatus", "owner", "creation", "modified"}
	valid_fields.update(field.fieldname for field in meta.fields if field.fieldname and field.fieldtype not in BLOCKED_FIELDTYPES)

	cleaned = []
	for field in fields[:12]:
		field = cstr(field).strip()
		if field in valid_fields and field not in cleaned:
			cleaned.append(field)

	if "name" not in cleaned:
		cleaned.insert(0, "name")

	return cleaned


def clean_filters(doctype, filters):
	if not filters:
		return {}

	meta = frappe.get_meta(doctype)
	valid_fields = {"name", "docstatus"}
	valid_fields.update(field.fieldname for field in meta.fields if field.fieldname)

	if isinstance(filters, dict):
		cleaned = {}
		for field, value in filters.items():
			if field not in valid_fields:
				frappe.throw(_("Filter field {0} is not valid for {1}.").format(frappe.bold(field), doctype))
			cleaned[field] = clean_filter_value(value)
		return cleaned

	if isinstance(filters, list):
		cleaned = []
		for entry in filters:
			if not isinstance(entry, list) or len(entry) not in {3, 4}:
				frappe.throw(_("Filters must be field/operator/value entries."))
			if len(entry) == 4:
				_filter_doctype, field, operator, value = entry
			else:
				field, operator, value = entry
			if field not in valid_fields:
				frappe.throw(_("Filter field {0} is not valid for {1}.").format(frappe.bold(field), doctype))
			operator = cstr(operator).lower()
			if operator not in ALLOWED_FILTER_OPERATORS:
				frappe.throw(_("Filter operator {0} is not allowed.").format(operator))
			cleaned.append([doctype, field, operator, clean_filter_value(value)])
		return cleaned

	frappe.throw(_("Filters must be an object or a list."))


def clean_filter_value(value):
	if isinstance(value, list):
		return [clean_filter_value(item) for item in value]
	if isinstance(value, dict):
		frappe.throw(_("Nested filter objects are not allowed."))
	return value


def build_search_filters(doctype, query):
	query = cstr(query).strip()
	if not query:
		return None

	meta = frappe.get_meta(doctype)
	valid_fields = {"name"}
	valid_fields.update(field.fieldname for field in meta.fields if field.fieldname)
	fields = [field for field in SEARCH_FIELDS.get(doctype, ["name"]) if field in valid_fields]
	return [[doctype, field, "like", f"%{query}%"] for field in fields]


def get_default_order_by(doctype):
	if doctype in {"Customer", "Supplier", "Company", "Account", "Item", "Cost Center", "Mode of Payment"}:
		return "modified desc"
	return "posting_date desc, modified desc"


def ensure_doctype_permission(doctype, permission_type):
	if not frappe.has_permission(doctype, ptype=permission_type):
		frappe.throw(
			_("You do not have {0} permission for {1}.").format(permission_type, frappe.bold(doctype)),
			frappe.PermissionError,
		)


def require_choice(value, choices, label):
	value = cstr(value).strip()
	if value not in choices:
		frappe.throw(_("{0} must be one of: {1}").format(label, ", ".join(sorted(choices))))
	return value


def require_string(value, label):
	value = cstr(value).strip()
	if not value:
		frappe.throw(_("{0} is required.").format(label))
	return value


def require_dict(value, label):
	value = parse_json_value(value, {})
	if not isinstance(value, dict) or not value:
		frappe.throw(_("{0} must be a non-empty object.").format(label))
	return value


def parse_json_value(value, fallback):
	if value is None:
		return fallback
	if isinstance(value, str):
		if not value.strip():
			return fallback
		return frappe.parse_json(value)
	return value


def needs_input(message, missing_fields):
	return {
		"status": "needs_input",
		"message": message,
		"missing_fields": missing_fields,
	}


def summarize_action(tool_name, args):
	labels = {
		"upsert_party": _("Create or update {0}").format(args.get("party_type") or _("party")),
		"remember_user_memory": summarize_memory_action(args),
		"update_draft_document": _("Update draft {0} {1}").format(args.get("doctype"), args.get("name")),
		"create_sales_invoice_draft": _("Create draft Sales Invoice"),
		"create_purchase_invoice_draft": _("Create draft Purchase Invoice"),
		"create_payment_entry_draft": _("Create draft Payment Entry"),
		"create_journal_entry_draft": _("Create draft Journal Entry"),
		"submit_or_cancel_document": _("{0} {1} {2}").format(cstr(args.get("action")).title(), args.get("doctype"), args.get("name")),
	}
	return labels.get(tool_name, tool_name.replace("_", " ").title())


def summarize_memory_action(args):
	memory_key = cstr(args.get("memory_key")).strip()
	memory_value = cstr(args.get("memory_value")).strip()
	config = USER_MEMORY_KEYS.get(memory_key)
	if config and memory_value:
		return _("Remember {0} {1}").format(config["label"], memory_value)
	return _("Remember user memory")


def get_action_impact(tool_name):
	if tool_name == "remember_user_memory":
		return _("This stores an explicit preference for the logged-in user. It does not change accounting records.")
	if tool_name == "submit_or_cancel_document":
		return _("This can post or reverse ledger impact through ERPNext document lifecycle validation.")
	if tool_name.startswith("create_"):
		return _("This creates a draft document only. It does not submit or post ledger entries.")
	return _("This updates ERPNext data using your current permissions.")


def summarize_doc(doc):
	fields = [
		"name",
		"docstatus",
		"status",
		"company",
		"posting_date",
		"customer",
		"supplier",
		"party_type",
		"party",
		"grand_total",
		"outstanding_amount",
		"paid_amount",
		"received_amount",
		"total_debit",
		"total_credit",
	]
	summary = {"doctype": doc.doctype, "name": doc.name, "url": get_doc_url(doc.doctype, doc.name)}
	for field in fields:
		if doc.get(field) not in (None, ""):
			summary[field] = normalize_value(doc.get(field))
	return summary


def sanitize_doc(doc):
	result = {}
	for fieldname, value in doc.as_dict(no_nulls=True).items():
		if fieldname in SYSTEM_FIELDS and fieldname not in {"name", "docstatus"}:
			continue
		if isinstance(value, list):
			result[fieldname] = [sanitize_child_row(row) for row in value[:20]]
		else:
			result[fieldname] = normalize_value(value)
	return result


def sanitize_child_row(row):
	cleaned = {}
	for fieldname, value in row.items():
		if fieldname in SYSTEM_FIELDS or cstr(fieldname).startswith("_"):
			continue
		cleaned[fieldname] = normalize_value(value)
	return cleaned


def normalize_columns(columns):
	normalized = []
	for column in columns:
		if isinstance(column, dict):
			normalized.append(
				{
					"label": column.get("label") or column.get("fieldname"),
					"fieldname": column.get("fieldname"),
					"fieldtype": column.get("fieldtype"),
				}
			)
		else:
			normalized.append({"label": cstr(column), "fieldname": cstr(column)})
	return normalized


def normalize_rows(rows):
	return [normalize_value(row) for row in rows]


def normalize_value(value):
	if isinstance(value, dict):
		return {key: normalize_value(item) for key, item in value.items()}
	if isinstance(value, list):
		return [normalize_value(item) for item in value]
	if hasattr(value, "as_dict"):
		return normalize_value(value.as_dict())
	if hasattr(value, "isoformat"):
		return value.isoformat()
	return value


def get_doc_url(doctype, name):
	path = "/app/{0}/{1}".format(frappe.scrub(doctype).replace("_", "-"), quote(name, safe=""))
	domain = cstr(os.environ.get("DOMAIN")).strip().rstrip("/")
	if not domain:
		return path
	return f"{domain}{path}"


def to_json(value):
	return json.dumps(normalize_value(value), default=str)


READ_TOOLS = {
	"search_records": tool_search_records,
	"get_record": tool_get_record,
	"run_bookkeeping_report": tool_run_bookkeeping_report,
}

MUTATING_TOOLS = {
	"upsert_party": tool_upsert_party,
	"remember_user_memory": tool_remember_user_memory,
	"update_draft_document": tool_update_draft_document,
	"create_sales_invoice_draft": tool_create_sales_invoice_draft,
	"create_purchase_invoice_draft": tool_create_purchase_invoice_draft,
	"create_payment_entry_draft": tool_create_payment_entry_draft,
	"create_journal_entry_draft": tool_create_journal_entry_draft,
	"submit_or_cancel_document": tool_submit_or_cancel_document,
}
