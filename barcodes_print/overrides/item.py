import frappe
from frappe import _


def validate_single_default_barcode(doc, method=None):
	"""Item is core - this runs via doc_events rather than Item's own
	controller. Only one row in the Barcodes table may be marked Default;
	block the save instead of silently picking one, so Data Import/API
	writes that bypass the client-side checkbox can't leave the Item in an
	ambiguous state."""
	default_barcodes = [row.barcode for row in doc.get("barcodes", []) if row.get("custom_default")]
	if len(default_barcodes) > 1:
		frappe.throw(
			_("Only one barcode can be marked Default. Currently marked: {0}").format(
				", ".join(default_barcodes)
			)
		)
