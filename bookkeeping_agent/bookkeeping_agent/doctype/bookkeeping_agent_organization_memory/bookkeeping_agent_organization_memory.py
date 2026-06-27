# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


MEMORY_SOURCE_ADMIN_REVIEW = "Admin Review"


class BookkeepingAgentOrganizationMemory(Document):
	def autoname(self):
		self.memory_name = get_organization_memory_name(self.company, self.title)
		self.name = self.memory_name

	def validate(self):
		self.company = require_text(self.company, _("Company"))
		self.title = require_text(self.title, _("Title"))
		self.memory = require_text(self.memory, _("Memory"))

		if not frappe.db.exists("Company", self.company):
			frappe.throw(_("Company {0} does not exist.").format(frappe.bold(self.company)))

		if not cint(self.priority):
			self.priority = 100
		if self.enabled is None:
			self.enabled = 1
		if not self.source:
			self.source = MEMORY_SOURCE_ADMIN_REVIEW
		if not self.memory_name:
			self.memory_name = get_organization_memory_name(self.company, self.title)


def require_text(value, label):
	value = cstr(value).strip()
	if not value:
		frappe.throw(_("{0} is required.").format(label))
	return value


def get_organization_memory_name(company, title):
	company = cstr(company).strip()
	title = cstr(title).strip()
	digest = hashlib.sha1(f"{company}:{title}".encode("utf-8")).hexdigest()[:12]
	return f"BAOM-{digest}"
