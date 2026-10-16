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
		from barcodes_print.api.item import item_master_field_map
		from barcodes_print.install.after_install import sync_display_rule_rows

		sync_display_rule_rows(self)
		self._validate_item_display_fields(item_master_field_map())

	def _validate_item_display_fields(self, allowed: dict):
		seen = set()
		for row in self.item_display_fields or []:
			df = allowed.get(row.item_field)
			if not df:
				frappe.throw(
					_("Row #{0}: {1} is not a printable field on Item.").format(row.idx, row.item_field)
				)
			if row.item_field in seen:
				frappe.throw(_("Item field {0} is already in Item Master Fields.").format(row.item_field))
			seen.add(row.item_field)
			row.field_label = df.label or row.item_field
