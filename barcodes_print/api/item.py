# Copyright (c) 2026, Auriga IT and contributors
# For license information, please see license.txt

import frappe


def get_item_price(item_code: str, price_list: str):
	"""Return this Item's rate in the given Price List, or None if no Item
	Price row exists for that combination - the caller prints "N/A" on the
	label rather than a silently wrong/zero rate."""
	if not item_code or not price_list:
		return None
	rate = frappe.db.get_value(
		"Item Price", {"item_code": item_code, "price_list": price_list}, "price_list_rate"
	)
	return rate


@frappe.whitelist()
def get_item_barcode_details(item_code: str, barcode: str = "", price_list: str = "") -> dict:
	if not item_code:
		return {}

	if not frappe.db.exists("Item", item_code):
		return {}

	item = frappe.get_cached_doc("Item", item_code)
	details = {
		"item_name": item.item_name or "",
		"uom": item.stock_uom or "",
		# None (not 0.0) means "no price found" - kept distinct from a
		# genuine zero price so the label can print "N/A" instead of a
		# misleading "0.00". Only ever resolved from the given Price List's
		# own Item Price row - never Item.standard_rate, which isn't
		# necessarily the rate anyone actually sells/buys at.
		"rate": get_item_price(item_code, price_list) if price_list else None,
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
