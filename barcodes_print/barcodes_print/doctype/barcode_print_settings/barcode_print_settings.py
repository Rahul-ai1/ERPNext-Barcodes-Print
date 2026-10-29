# Copyright (c) 2026, Rahul Chaudhary and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from barcodes_print.api.barcode import validate_two_column_widths


class BarcodePrintSettings(Document):
	def validate(self):
		validate_two_column_widths(self)
