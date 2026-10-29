# Copyright (c) 2026, Rahul Chaudhary and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint


def get_item_price(item_code: str, price_list: str):
	"""Return this Item's rate in the given Price List, or None if no Item
	Price row exists for that combination - the caller leaves the Rate line
	off the label entirely rather than a silently wrong/zero rate."""
	if not item_code or not price_list:
		return None
	rate = frappe.db.get_value(
		"Item Price", {"item_code": item_code, "price_list": price_list}, "price_list_rate"
	)
	return rate


def get_item_tax_rate_percent(tax_template: str) -> float:
	"""Total tax % for an Item Tax Template - the sum of its own detail
	rows' tax_rate, exactly as the template defines it. No transaction
	(customer/order) is involved, so this is a static, context-free lookup;
	Nil-Rated/Exempted/Non-GST templates naturally sum to 0."""
	if not tax_template:
		return 0.0
	rows = frappe.get_all(
		"Item Tax Template Detail",
		filters={"parent": tax_template, "not_applicable": 0},
		fields=["tax_rate"],
	)
	return sum(frappe.utils.flt(r.tax_rate) for r in rows)


def get_item_rate_and_note(item_code: str, price_list: str, tax_template: str = ""):
	"""The Rate to print and the short note that must go with it. A Tax
	Template resolved (selected on the Print Barcode page, or auto-fetched
	from a Purchase Order/Receipt line) means the price is marked up by
	that template's own tax % and printed "Incl. of taxes"; none resolved
	means the plain Price List rate, printed "Excl. of taxes" - the label
	always says which, never just a bare number. Neither the tax % nor any
	Company/template name is ever printed, only the final Rate and this
	short note."""
	base_rate = get_item_price(item_code, price_list) if price_list else None
	if base_rate is None:
		return None, ""

	if tax_template:
		tax_percent = get_item_tax_rate_percent(tax_template)
		rate = base_rate * (1 + tax_percent / 100.0)
		return rate, _("Incl. of taxes")

	return base_rate, _("Excl. of taxes")


@frappe.whitelist()
def get_item_barcode_details(
	item_code: str, barcode: str = "", price_list: str = "", tax_template: str = ""
) -> dict:
	if not item_code:
		return {}

	if not frappe.db.exists("Item", item_code):
		return {}

	from barcodes_print.api.barcode import get_barcode_print_settings

	mode = get_barcode_print_settings()["batch_serial_print_mode"]
	batch_printing_enabled = mode in ("Both", "Batch Only")
	serial_printing_enabled = mode in ("Both", "Serial Only")

	item = frappe.get_cached_doc("Item", item_code)
	rate, price_note = get_item_rate_and_note(item_code, price_list, tax_template)
	details = {
		"item_name": item.item_name or "",
		"uom": item.stock_uom or "",
		# Gated by the site-wide Batch/Serial Printing setting - when a
		# type is off there, the Print Barcode page's picker for it never
		# even appears, regardless of whether this Item is actually
		# tracked that way.
		"has_batch_no": cint(item.has_batch_no) if batch_printing_enabled else 0,
		"has_serial_no": cint(item.has_serial_no) if serial_printing_enabled else 0,
		# None (not 0.0) means "no price found" - kept distinct from a
		# genuine zero price so the label leaves the Rate line off entirely
		# instead of printing a misleading "0.00". Only ever resolved from
		# the given Price List's own Item Price row - never
		# Item.standard_rate, which isn't necessarily the rate anyone
		# actually sells/buys at. Marked up by tax_template's own tax %
		# when one is given - see get_item_rate_and_note.
		"rate": rate,
		"price_note": price_note,
		"item_group": item.item_group or "",
		"brand": item.brand or "",
		"description": item.description or "",
		"barcode": "",
		"barcode_type": "",
		"barcode_uom": "",
		# Every barcode this Item has, so a caller with more than one can
		# offer the user an explicit choice instead of silently guessing.
		"barcodes": [],
	}

	# Check child barcodes table first (Item Master -> Barcodes table)
	barcodes = getattr(item, "barcodes", []) or []
	if barcodes:
		details["barcodes"] = [
			{
				"barcode": (b.barcode or "").strip(),
				"barcode_type": (b.barcode_type or "").strip(),
				"uom": (getattr(b, "uom", "") or "").strip(),
				"default": cint(getattr(b, "custom_default", 0)),
			}
			for b in barcodes
			if (b.barcode or "").strip()
		]

		target_row = None
		if barcode:
			# Find specific matching barcode in child table
			for b in barcodes:
				if (b.barcode or "").strip() == str(barcode).strip():
					target_row = b
					break

		if not target_row and not barcode and len(details["barcodes"]) == 1:
			# Only auto-pick when there's no ambiguity - one barcode, one
			# obvious choice. With two or more, leave it blank so the
			# caller has to ask the user which one they mean, rather than
			# silently guessing the first row in the child table.
			target_row = barcodes[0]

		if not target_row and not barcode:
			# Two or more barcodes, but exactly one is marked Default -
			# use it instead of asking. (overrides.item.py blocks saving
			# an Item with more than one Default, so this list is never
			# longer than 1 in practice.)
			default_rows = [b for b in barcodes if cint(getattr(b, "custom_default", 0))]
			if len(default_rows) == 1:
				target_row = default_rows[0]

		if target_row:
			details["barcode"] = (target_row.barcode or "").strip()
			details["barcode_type"] = (target_row.barcode_type or "").strip()
			details["barcode_uom"] = (getattr(target_row, "uom", "") or "").strip()

	elif getattr(item, "barcode", None):
		details["barcode"] = (item.barcode or "").strip()
		details["barcode_type"] = (getattr(item, "barcode_type", "") or "").strip()
		details["barcode_uom"] = (getattr(item, "barcode_uom", "") or item.stock_uom or "").strip()
		details["barcodes"] = [
			{"barcode": details["barcode"], "barcode_type": details["barcode_type"], "uom": details["barcode_uom"]}
		]

	# If barcode is not in Item Master, leave blank ("")
	return details


def batch_belongs_to_item(batch_no: str, item_code: str) -> bool:
	"""Whether `batch_no` is a real Batch recorded against `item_code` -
	the single source of truth for this check, used both when actually
	resolving a print row and when validating a bulk-uploaded file before
	any of its rows reach the grid."""
	return bool(frappe.db.exists("Batch", {"name": batch_no, "item": item_code}))


def serial_no_item_and_batch(serial_no: str) -> tuple:
	"""The Item and Batch a Serial No actually belongs to, or (None, None)
	if it doesn't exist - the single source of truth for this lookup, used
	both when actually resolving a print row and when validating a
	bulk-uploaded file before any of its rows reach the grid."""
	row = frappe.db.get_value("Serial No", serial_no, ["item_code", "batch_no"], as_dict=True)
	if not row:
		return None, None
	return row.item_code, row.batch_no or ""


@frappe.whitelist()
def get_available_batches(item_code: str) -> list:
	"""Existing Batches for this Item, newest first - lets the Print Barcode
	page's user pick which specific batch a label run is for, rather than
	typing a Batch No from memory. Disabled batches are excluded; nothing
	here creates or reserves stock, this is a read-only lookup for printing."""
	if not item_code:
		return []
	return frappe.get_all(
		"Batch",
		filters={"item": item_code, "disabled": 0},
		fields=["name", "expiry_date"],
		order_by="creation desc",
		limit_page_length=100,
	)


@frappe.whitelist()
def get_available_serial_nos(item_code: str, batch_no: str = "") -> list:
	"""Existing, still-Active Serial Nos for this Item (optionally narrowed to
	one Batch), so the user can multi-select exactly which physical units a
	label run covers instead of typing serial numbers from memory."""
	if not item_code:
		return []
	filters = {"item_code": item_code, "status": "Active"}
	if batch_no:
		filters["batch_no"] = batch_no
	return frappe.get_all(
		"Serial No",
		filters=filters,
		fields=["name", "batch_no"],
		order_by="creation desc",
		limit_page_length=500,
	)
