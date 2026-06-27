# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr


MAX_USER_TEXT_MEMORY_CHARS = 300


SUPPORTED_MEMORY_KEYS = {
	"default_company": {
		"reference_doctype": "Company",
	},
	"default_currency": {
		"reference_doctype": "Currency",
	},
	"bookkeeping_preference_note": {
		"value_type": "text",
		"max_length": MAX_USER_TEXT_MEMORY_CHARS,
	},
}
MEMORY_SOURCE_EXPLICIT = "Explicit User Request"


class BookkeepingAgentMemory(Document):
	def autoname(self):
		self.memory_name = get_memory_name(self.user, self.memory_key)
		self.name = self.memory_name

	def validate(self):
		if self.memory_key not in SUPPORTED_MEMORY_KEYS:
			frappe.throw(_("Memory Key must be one of: {0}").format(", ".join(sorted(SUPPORTED_MEMORY_KEYS))))

		config = SUPPORTED_MEMORY_KEYS[self.memory_key]
		self.memory_value = require_text(self.memory_value, _("Memory Value"))

		max_length = cint(config.get("max_length") or 0)
		if max_length and len(self.memory_value) > max_length:
			frappe.throw(_("Memory Value must be {0} characters or fewer.").format(max_length))

		reference_doctype = cstr(config.get("reference_doctype")).strip()
		if reference_doctype:
			self.reference_doctype = reference_doctype
			if not cstr(self.reference_name).strip():
				self.reference_name = self.memory_value
			if self.memory_value != self.reference_name:
				frappe.throw(_("Memory Value must match Reference Name."))

			if not frappe.db.exists(self.reference_doctype, self.reference_name):
				frappe.throw(
					_("{0} {1} does not exist.").format(self.reference_doctype, frappe.bold(self.reference_name))
				)
		else:
			self.reference_doctype = ""
			self.reference_name = ""

		if not self.source:
			self.source = MEMORY_SOURCE_EXPLICIT
		if self.enabled is None:
			self.enabled = 1


def get_memory_name(user, memory_key):
	user = cstr(user).strip()
	memory_key = cstr(memory_key).strip()
	digest = hashlib.sha1(f"{user}:{memory_key}".encode("utf-8")).hexdigest()[:12]
	return f"BAM-{digest}"


def require_text(value, label):
	value = cstr(value).strip()
	if not value:
		frappe.throw(_("{0} is required.").format(label))
	return value
