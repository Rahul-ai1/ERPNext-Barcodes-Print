# Copyright (c) 2026, Rahul Chaudhary and contributors
# For license information, please see license.txt

import frappe


@frappe.whitelist()
def get_my_default_printer() -> str:
	"""Always the calling user's own preference - never accepts a user
	argument, so one user can never read or overwrite another's."""
	return frappe.db.get_value("User", frappe.session.user, "custom_default_label_printer") or ""


@frappe.whitelist()
def set_my_default_printer(printer_name: str = "") -> None:
	frappe.db.set_value(
		"User",
		frappe.session.user,
		"custom_default_label_printer",
		(printer_name or "").strip(),
		update_modified=False,
	)
