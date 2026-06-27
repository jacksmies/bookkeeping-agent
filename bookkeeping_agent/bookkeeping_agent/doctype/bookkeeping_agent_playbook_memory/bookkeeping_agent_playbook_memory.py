# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


PLAYBOOK_SCOPES = {"Global", "Company"}
MEMORY_SOURCE_ADMIN_REVIEW = "Admin Review"


class BookkeepingAgentPlaybookMemory(Document):
	def autoname(self):
		self.memory_name = get_playbook_memory_name(self.title, self.scope, self.company)
		self.name = self.memory_name

	def validate(self):
		self.title = require_text(self.title, _("Title"))
		self.guidance = require_text(self.guidance, _("Guidance"))
		self.scope = cstr(self.scope).strip() or "Global"

		if self.scope not in PLAYBOOK_SCOPES:
			frappe.throw(_("Scope must be one of: {0}").format(", ".join(sorted(PLAYBOOK_SCOPES))))

		if self.scope == "Company":
			self.company = require_text(self.company, _("Company"))
			if not frappe.db.exists("Company", self.company):
				frappe.throw(_("Company {0} does not exist.").format(frappe.bold(self.company)))
		else:
			self.company = ""

		if not cint(self.priority):
			self.priority = 100
		if self.enabled is None:
			self.enabled = 1
		if not self.source:
			self.source = MEMORY_SOURCE_ADMIN_REVIEW
		if not self.memory_name:
			self.memory_name = get_playbook_memory_name(self.title, self.scope, self.company)


def require_text(value, label):
	value = cstr(value).strip()
	if not value:
		frappe.throw(_("{0} is required.").format(label))
	return value


def get_playbook_memory_name(title, scope=None, company=None):
	title = cstr(title).strip()
	scope = cstr(scope).strip() or "Global"
	company = cstr(company).strip() if scope == "Company" else ""
	digest = hashlib.sha1(f"{scope}:{company}:{title}".encode("utf-8")).hexdigest()[:12]
	return f"BAPM-{digest}"
