# Copyright (c) 2026, Rahul Chaudhary and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class BarcodePrintSettings(Document):
	def validate(self):
		columns = cint(self.number_of_columns)
		if columns < 1 or columns > 4:
			frappe.throw(_("Number of Columns must be between 1 and 4."))
		if flt(self.column_gap_mm) < 0:
			frappe.throw(_("Gap Between Columns cannot be negative."))
		if flt(self.line_spacing_mm) < 0:
			frappe.throw(_("Line Spacing cannot be negative."))
		from barcodes_print.install.after_install import (
			sync_display_rule_rows,
			sync_item_display_field_rows,
		)

		sync_display_rule_rows(self)
		sync_item_display_field_rows(self)
