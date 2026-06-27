# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import json
import os
from pathlib import Path

import frappe

from bookkeeping_agent.bookkeeping_agent.page.bookkeeping_agent import bookkeeping_agent as agent
from bookkeeping_agent.bookkeeping_agent.page.bookkeeping_agent.bookkeeping_agent import (
	build_response_from_stream_state,
	clean_payload,
	extract_function_calls,
	get_response_text,
	handle_response_stream_event,
	new_response_stream_state,
	prepare_pending_action,
	run_tool,
	validate_journal_entry_balance,
)
from frappe.tests import UnitTestCase


class TestBookkeepingAgent(UnitTestCase):
	def test_streaming_text_deltas_build_final_text(self):
		state = new_response_stream_state()

		handle_response_stream_event(
			{"type": "response.output_text.delta", "delta": "Hello"},
			None,
			state,
		)
		handle_response_stream_event(
			{"type": "response.output_text.delta", "delta": " **there**"},
			None,
			state,
		)

		response = build_response_from_stream_state(state)
		self.assertEqual(get_response_text(response), "Hello **there**")

	def test_streaming_function_call_uses_output_item_name_fallback(self):
		state = new_response_stream_state()

		handle_response_stream_event(
			{
				"type": "response.output_item.added",
				"item": {
					"id": "fc_1",
					"type": "function_call",
					"name": "search_records",
					"call_id": "call_1",
				},
			},
			None,
			state,
		)
		handle_response_stream_event(
			{
				"type": "response.function_call_arguments.done",
				"item_id": "fc_1",
				"name": None,
				"arguments": '{"doctype":"Customer"}',
			},
			None,
			state,
		)

		response = build_response_from_stream_state(state)
		tool_calls = extract_function_calls(response)
		self.assertEqual(tool_calls[0]["name"], "search_records")
		self.assertEqual(tool_calls[0]["arguments"], {"doctype": "Customer"})

	def test_journal_entry_balance_validation_allows_balanced_rows(self):
		self.assertIsNone(
			validate_journal_entry_balance(
				{
					"accounts": [
						{"account": "Debtors - _TC", "debit_in_account_currency": 100},
						{"account": "Sales - _TC", "credit_in_account_currency": 100},
					]
				}
			)
		)

	def test_journal_entry_balance_validation_blocks_unbalanced_rows(self):
		self.assertEqual(
			validate_journal_entry_balance(
				{
					"accounts": [
						{"account": "Debtors - _TC", "debit_in_account_currency": 100},
						{"account": "Sales - _TC", "credit_in_account_currency": 90},
					]
				}
			),
			"Journal Entry total debit must equal total credit.",
		)

	def test_mutating_tool_returns_pending_action_before_execution(self):
		result = run_tool(
			"create_journal_entry_draft",
			{
				"fields": {
					"company": "_Test Company",
					"accounts": [
						{"account": "Debtors - _TC", "debit_in_account_currency": 100},
						{"account": "Sales - _TC", "credit_in_account_currency": 100},
					],
				}
			},
			execute_mutation=False,
		)

		self.assertEqual(result["status"], "pending_confirmation")
		self.assertEqual(result["pending_action"]["tool"], "create_journal_entry_draft")

	def test_missing_invoice_fields_request_more_input(self):
		result = prepare_pending_action(
			"create_sales_invoice_draft",
			{"fields": {"company": "_Test Company", "customer": "_Test Customer"}},
		)

		self.assertEqual(result["status"], "needs_input")
		self.assertIn("items", result["missing_fields"])

	def test_clean_payload_blocks_system_fields(self):
		with self.assertRaises(frappe.ValidationError):
			clean_payload("Customer", {"docstatus": 1}, allow_tables=False)

	def test_model_input_compacts_older_messages(self):
		messages = []
		for index in range(agent.MAX_RECENT_MESSAGES + 4):
			messages.append({"role": "user", "content": f"older question {index}"})
			messages.append({"role": "assistant", "content": f"older answer {index}"})

		original_get_enabled_user_memories = agent.get_enabled_user_memories
		try:
			agent.get_enabled_user_memories = lambda user=None: []
			input_items = agent.build_model_input(messages)
		finally:
			agent.get_enabled_user_memories = original_get_enabled_user_memories

		self.assertEqual(len(input_items), agent.MAX_RECENT_MESSAGES + 1)
		self.assertEqual(input_items[0]["role"], "user")
		self.assertIn("Earlier conversation summary", input_items[0]["content"])
		self.assertIn("older question 0", input_items[0]["content"])
		self.assertEqual(input_items[-1]["content"], f"older answer {agent.MAX_RECENT_MESSAGES + 3}")

	def test_response_token_usage_extracts_cached_tokens(self):
		usage = agent.extract_response_token_usage(
			{
				"usage": {
					"input_tokens": 2000,
					"output_tokens": 125,
					"total_tokens": 2125,
					"input_tokens_details": {"cached_tokens": 1536},
				}
			}
		)

		self.assertEqual(usage["input_tokens"], 2000)
		self.assertEqual(usage["output_tokens"], 125)
		self.assertEqual(usage["cached_tokens"], 1536)

	def test_read_tool_cache_key_is_stable_and_scoped(self):
		key_a = agent.build_read_tool_cache_key(
			"search_records",
			{"doctype": "Customer", "filters": {"company": "Demo Books LLC"}},
			{"site": "test.local", "user": "test@example.com", "roles": ["Accounts User"]},
		)
		key_b = agent.build_read_tool_cache_key(
			"search_records",
			{"filters": {"company": "Demo Books LLC"}, "doctype": "Customer"},
			{"roles": ["Accounts User"], "user": "test@example.com", "site": "test.local"},
		)
		key_c = agent.build_read_tool_cache_key(
			"search_records",
			{"doctype": "Customer", "filters": {"company": "Demo Books LLC"}},
			{"site": "test.local", "user": "other@example.com", "roles": ["Accounts User"]},
		)

		self.assertEqual(key_a, key_b)
		self.assertNotEqual(key_a, key_c)

	def test_result_mode_defaults_to_summary(self):
		self.assertEqual(agent.get_result_mode({}, {"summary", "full"}, "summary"), "summary")
		self.assertEqual(
			agent.get_result_mode({"result_mode": "full"}, {"summary", "full"}, "summary"),
			"full",
		)

	def test_doc_url_defaults_to_relative_desk_path(self):
		original_domain = os.environ.pop("DOMAIN", None)
		try:
			self.assertEqual(
				agent.get_doc_url("Sales Invoice", "ACC-SINV-2026-00010"),
				"/app/sales-invoice/ACC-SINV-2026-00010",
			)
		finally:
			if original_domain is not None:
				os.environ["DOMAIN"] = original_domain

	def test_doc_url_uses_domain_environment_variable(self):
		original_domain = os.environ.get("DOMAIN")
		try:
			os.environ["DOMAIN"] = "https://books.example.com/"
			self.assertEqual(
				agent.get_doc_url("Sales Invoice", "ACC-SINV-2026-00010"),
				"https://books.example.com/app/sales-invoice/ACC-SINV-2026-00010",
			)
		finally:
			if original_domain is None:
				os.environ.pop("DOMAIN", None)
			else:
				os.environ["DOMAIN"] = original_domain

	def test_memory_tool_requires_default_company_value(self):
		result = prepare_pending_action("remember_user_memory", {"memory_key": "default_company"})

		self.assertEqual(result["status"], "needs_input")
		self.assertIn("memory_value", result["missing_fields"])

	def test_memory_tool_returns_pending_action_before_execution(self):
		result = prepare_pending_action(
			"remember_user_memory",
			{"memory_key": "default_company", "memory_value": "Demo Books LLC"},
		)

		self.assertEqual(result["status"], "pending_confirmation")
		self.assertEqual(result["pending_action"]["tool"], "remember_user_memory")
		self.assertEqual(result["pending_action"]["summary"], "Remember default company Demo Books LLC")

	def test_memory_tool_accepts_default_currency(self):
		result = prepare_pending_action(
			"remember_user_memory",
			{"memory_key": "default_currency", "memory_value": "EUR"},
		)

		self.assertEqual(result["status"], "pending_confirmation")
		self.assertEqual(result["pending_action"]["tool"], "remember_user_memory")
		self.assertEqual(result["pending_action"]["summary"], "Remember default currency EUR")

	def test_text_memory_rejects_oversized_preference_note(self):
		result = prepare_pending_action(
			"remember_user_memory",
			{"memory_key": "bookkeeping_preference_note", "memory_value": "x" * 301},
		)

		self.assertEqual(result["status"], "needs_input")
		self.assertIn("memory_value", result["missing_fields"])

	def test_user_memory_context_formats_enabled_default_company(self):
		original_get_enabled_user_memories = agent.get_enabled_user_memories
		try:
			agent.get_enabled_user_memories = lambda user=None: [
				{
					"memory_key": "default_company",
					"memory_value": "Demo Books LLC",
					"reference_doctype": "Company",
					"reference_name": "Demo Books LLC",
				}
			]

			context = agent.build_user_memory_context("user1@example.com")
		finally:
			agent.get_enabled_user_memories = original_get_enabled_user_memories

		self.assertIn("explicit preferences for the logged-in user", context)
		self.assertIn("Default company: Demo Books LLC", context)
		self.assertIn("current user instructions override memory", context)

	def test_user_memory_context_formats_currency_and_preference_note(self):
		original_get_enabled_user_memories = agent.get_enabled_user_memories
		try:
			agent.get_enabled_user_memories = lambda user=None: [
				{
					"memory_key": "default_currency",
					"memory_value": "EUR",
					"reference_doctype": "Currency",
					"reference_name": "EUR",
				},
				{
					"memory_key": "bookkeeping_preference_note",
					"memory_value": "When possible, show reports in EUR.",
					"reference_doctype": "",
					"reference_name": "",
				},
			]

			context = agent.build_user_memory_context("user1@example.com")
		finally:
			agent.get_enabled_user_memories = original_get_enabled_user_memories

		self.assertIn("Default currency: EUR", context)
		self.assertIn("Bookkeeping preference note: When possible, show reports in EUR.", context)

	def test_admin_memory_doctypes_are_system_manager_managed(self):
		for doctype_folder in (
			"bookkeeping_agent_playbook_memory",
			"bookkeeping_agent_organization_memory",
		):
			doctype = self.load_doctype_json(doctype_folder)
			permissions = doctype["permissions"]

			write_roles = {
				permission["role"]
				for permission in permissions
				if permission.get("create") or permission.get("write") or permission.get("delete")
			}
			self.assertEqual(write_roles, {"System Manager"})

			system_manager = next(
				permission for permission in permissions if permission["role"] == "System Manager"
			)
			self.assertEqual(system_manager.get("read"), 1)
			self.assertEqual(system_manager.get("write"), 1)

	def test_admin_playbook_context_formats_relevant_rule(self):
		original_get_enabled_playbook_memories = agent.get_enabled_playbook_memories
		original_get_enabled_organization_memories = agent.get_enabled_organization_memories
		original_get_enabled_user_memories = agent.get_enabled_user_memories
		try:
			agent.get_enabled_playbook_memories = lambda company=None: [
				{
					"title": "Supplier service category correction",
					"guidance": "Supplier Type is legal form. Use Company for Contabo and Services as supplier_group.",
					"suggested_prompt": "Create a supplier named Contabo with supplier type Company and supplier group Services.",
					"trigger_terms": "supplier type, services, Contabo",
					"scope": "Global",
					"priority": 10,
				}
			]
			agent.get_enabled_organization_memories = lambda company=None: []
			agent.get_enabled_user_memories = lambda user=None: []

			context = agent.build_admin_memory_context(
				[{"role": "user", "content": "Create a supplier for hosting services called Contabo"}]
			)
		finally:
			agent.get_enabled_playbook_memories = original_get_enabled_playbook_memories
			agent.get_enabled_organization_memories = original_get_enabled_organization_memories
			agent.get_enabled_user_memories = original_get_enabled_user_memories

		self.assertIn("admin playbook", context)
		self.assertIn("Supplier service category correction", context)
		self.assertIn("supplier_group", context)
		self.assertIn("Suggested prompt", context)

	def test_admin_playbook_context_skips_irrelevant_rule(self):
		original_get_enabled_playbook_memories = agent.get_enabled_playbook_memories
		original_get_enabled_organization_memories = agent.get_enabled_organization_memories
		original_get_enabled_user_memories = agent.get_enabled_user_memories
		try:
			agent.get_enabled_playbook_memories = lambda company=None: [
				{
					"title": "Supplier service category correction",
					"guidance": "Supplier Type is legal form.",
					"trigger_terms": "supplier type, services, Contabo",
					"scope": "Global",
					"priority": 10,
				}
			]
			agent.get_enabled_organization_memories = lambda company=None: []
			agent.get_enabled_user_memories = lambda user=None: []

			context = agent.build_admin_memory_context(
				[{"role": "user", "content": "Show overdue invoices"}]
			)
		finally:
			agent.get_enabled_playbook_memories = original_get_enabled_playbook_memories
			agent.get_enabled_organization_memories = original_get_enabled_organization_memories
			agent.get_enabled_user_memories = original_get_enabled_user_memories

		self.assertEqual(context, "")

	def test_organization_memory_context_uses_default_company(self):
		original_get_enabled_playbook_memories = agent.get_enabled_playbook_memories
		original_get_enabled_organization_memories = agent.get_enabled_organization_memories
		original_get_enabled_user_memories = agent.get_enabled_user_memories
		try:
			agent.get_enabled_playbook_memories = lambda company=None: []
			agent.get_enabled_organization_memories = lambda company=None: [
				{
					"title": "Hosting vendor handling",
					"memory": "Hosting invoices for CXO Studio (Demo) should use the Hosting Services expense account.",
					"trigger_terms": "hosting, Contabo",
					"company": company,
					"priority": 10,
				}
			]
			agent.get_enabled_user_memories = lambda user=None: [
				{
					"memory_key": "default_company",
					"memory_value": "CXO Studio (Demo)",
					"reference_doctype": "Company",
					"reference_name": "CXO Studio (Demo)",
				}
			]

			context = agent.build_admin_memory_context(
				[{"role": "user", "content": "How should I book a Contabo hosting invoice?"}]
			)
		finally:
			agent.get_enabled_playbook_memories = original_get_enabled_playbook_memories
			agent.get_enabled_organization_memories = original_get_enabled_organization_memories
			agent.get_enabled_user_memories = original_get_enabled_user_memories

		self.assertIn("organization memory for CXO Studio (Demo)", context)
		self.assertIn("Hosting vendor handling", context)
		self.assertIn("Hosting Services expense account", context)

	def load_doctype_json(self, doctype_folder):
		path = Path(agent.__file__).parents[2] / "doctype" / doctype_folder / f"{doctype_folder}.json"
		with path.open(encoding="utf-8") as file:
			return json.load(file)

	def test_run_agent_allows_final_response_after_max_tool_rounds(self):
		final_text = "Draft review summary."
		responses = [
			{
				"output": [
					{
						"type": "function_call",
						"name": "search_records",
						"call_id": f"call_{index}",
						"arguments": json.dumps({"doctype": "Customer", "limit": index + 1}),
					}
				]
			}
			for index in range(agent.MAX_TOOL_STEPS)
		]
		responses.append(
			{
				"output_text": final_text,
				"output": [{"type": "message", "content": [{"type": "output_text", "text": final_text}]}],
			}
		)
		original_build_model_input = agent.build_model_input
		original_create_agent_response = agent.create_agent_response
		original_get_agent_model = agent.get_agent_model
		original_get_openai_client = agent.get_openai_client
		original_log_agent_usage = agent.log_agent_usage
		original_publish_agent_event = agent.publish_agent_event
		original_run_tool_for_agent = agent.run_tool_for_agent
		tool_calls = []

		def fake_response(client, model, input_items, run_id=None, allow_tools=True):
			return responses.pop(0)

		def fake_tool(tool_name, args):
			tool_calls.append((tool_name, args))
			return {"status": "ok", "message": "ok"}

		try:
			agent.build_model_input = lambda messages: [{"role": "user", "content": "Find drafts"}]
			agent.create_agent_response = fake_response
			agent.get_agent_model = lambda: "test-model"
			agent.get_openai_client = lambda: object()
			agent.log_agent_usage = lambda run_id, model, metrics: None
			agent.publish_agent_event = lambda run_id, event, payload=None: None
			agent.run_tool_for_agent = fake_tool

			result = agent.run_agent([{"role": "user", "content": "Find drafts"}])
		finally:
			agent.build_model_input = original_build_model_input
			agent.create_agent_response = original_create_agent_response
			agent.get_agent_model = original_get_agent_model
			agent.get_openai_client = original_get_openai_client
			agent.log_agent_usage = original_log_agent_usage
			agent.publish_agent_event = original_publish_agent_event
			agent.run_tool_for_agent = original_run_tool_for_agent

		self.assertEqual(result["text"], final_text)
		self.assertEqual(len(tool_calls), agent.MAX_TOOL_STEPS)

	def test_run_agent_forces_final_answer_when_tool_budget_is_exhausted(self):
		final_text = "CFO action plan from gathered results."
		tool_response_count = 0
		allow_tools_values = []
		original_build_model_input = agent.build_model_input
		original_create_agent_response = agent.create_agent_response
		original_get_agent_model = agent.get_agent_model
		original_get_openai_client = agent.get_openai_client
		original_log_agent_usage = agent.log_agent_usage
		original_publish_agent_event = agent.publish_agent_event
		original_run_tool_for_agent = agent.run_tool_for_agent
		tool_calls = []

		def fake_response(client, model, input_items, run_id=None, allow_tools=True):
			nonlocal tool_response_count
			allow_tools_values.append(allow_tools)
			if allow_tools:
				tool_response_count += 1
				return {
					"output": [
						{
							"type": "function_call",
							"name": "search_records",
							"call_id": f"call_{tool_response_count}",
							"arguments": json.dumps({"doctype": "Customer", "limit": tool_response_count}),
						}
					]
				}
			self.assertIn("tool budget", input_items[-1]["content"].lower())
			return {
				"output_text": final_text,
				"output": [{"type": "message", "content": [{"type": "output_text", "text": final_text}]}],
			}

		def fake_tool(tool_name, args):
			tool_calls.append((tool_name, args))
			return {"status": "ok", "message": "ok"}

		try:
			agent.build_model_input = lambda messages: [{"role": "user", "content": "Make a CFO plan"}]
			agent.create_agent_response = fake_response
			agent.get_agent_model = lambda: "test-model"
			agent.get_openai_client = lambda: object()
			agent.log_agent_usage = lambda run_id, model, metrics: None
			agent.publish_agent_event = lambda run_id, event, payload=None: None
			agent.run_tool_for_agent = fake_tool

			result = agent.run_agent([{"role": "user", "content": "Make a CFO plan"}])
		finally:
			agent.build_model_input = original_build_model_input
			agent.create_agent_response = original_create_agent_response
			agent.get_agent_model = original_get_agent_model
			agent.get_openai_client = original_get_openai_client
			agent.log_agent_usage = original_log_agent_usage
			agent.publish_agent_event = original_publish_agent_event
			agent.run_tool_for_agent = original_run_tool_for_agent

		self.assertEqual(result["text"], final_text)
		self.assertEqual(len(tool_calls), agent.MAX_TOOL_STEPS)
		self.assertEqual(allow_tools_values[-1], False)
