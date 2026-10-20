# Copyright (c) 2026, Rahul Chaudhary and contributors
# For license information, please see license.txt

import os

import frappe
from frappe.modules.import_file import import_file_by_path

# Frappe does not auto-import "Workspace Sidebar" / "Desktop Icon" standard
# files the way it does for DocType/Report/Page/Workspace - both are built
# dynamically from Workspace.shortcuts on install instead, which ignores
# these files entirely. Import them explicitly so the sidebar/app-icon this
# app ships are always in place, on install and on every migrate.
#
# Both doctypes are Frappe v16-only - v15 has no "Workspace Sidebar"/"Desktop
# Icon" DocType at all, so importing these files there doesn't just fail
# gracefully, it crashes with an ImportError trying to load a Python module
# that was never shipped, taking down the whole install-app/migrate. Each
# entry is paired with its DocType name so it can be skipped on a site where
# that DocType doesn't exist, rather than assumed to always be there.
STANDARD_FILES = [
	("workspace_sidebar", "barcodes_print.json", "Workspace Sidebar"),
	("desktop_icon", "barcodes_print.json", "Desktop Icon"),
]


def sync_standard_files():
	for folder, filename, doctype in STANDARD_FILES:
		if not frappe.db.exists("DocType", doctype):
			continue
		path = frappe.get_app_path("barcodes_print", folder, filename)
		if os.path.exists(path):
			import_file_by_path(path, force=True)


DEFAULTS = {
	"print_connector": "QZ Tray",
	"printer_dpi": "203",
	"small_width_mm": 50,
	"small_height_mm": 25,
	"medium_width_mm": 75,
	"medium_height_mm": 50,
	"large_width_mm": 100,
	"large_height_mm": 75,
}



def after_install():
	sync_standard_files()
	seed_barcode_print_settings()
	ensure_item_barcode_types()
	ensure_purchase_barcode_tracking_fields()
	ensure_display_rules()


def after_migrate():
	sync_standard_files()
	seed_barcode_print_settings()
	ensure_item_barcode_types()
	ensure_purchase_barcode_tracking_fields()
	ensure_display_rules()
	refresh_print_barcode_page_cache()


def refresh_print_barcode_page_cache():
	"""Desk stores the Print Barcode script in the browser until Page.modified changes.

	A git pull updates the file, but an already-opened desk keeps the old
	script. Bumping modified makes the next reload fetch the file again.
	"""
	if not frappe.db.exists("Page", "print-barcode"):
		return
	frappe.db.set_value(
		"Page",
		"print-barcode",
		"modified",
		frappe.utils.now_datetime(),
		update_modified=False,
	)


def seed_barcode_print_settings():
	if frappe.db.exists("DocType", "Barcode Print Settings"):
		doc = frappe.get_single("Barcode Print Settings")
		for field, value in DEFAULTS.items():
			if doc.get(field) in (None, ""):
				doc.set(field, value)
		# Existing sites gained Number of Columns after the Single was
		# created, so the column is 0 until the JSON default is applied.
		columns = frappe.utils.cint(doc.number_of_columns)
		if columns < 1 or columns > 4:
			doc.number_of_columns = 1
		if frappe.utils.flt(doc.column_gap_mm) < 0:
			doc.column_gap_mm = 0
		if doc.get("line_spacing_mm") in (None, ""):
			doc.line_spacing_mm = 0.6
		if doc.get("item_name_character_limit") in (None, ""):
			doc.item_name_character_limit = 0
		doc.save(ignore_permissions=True)


def sync_display_rule_rows(doc) -> bool:
	"""Keep Small, Medium, and Large, plus a Label Only row for each size.
	Returns True when the document changed. Does not save."""
	from frappe.utils import cint

	from barcodes_print.api.barcode import (
		CANONICAL_DISPLAY_RULES,
		default_display_rule,
		is_label_only_rule,
	)

	updated = False
	seen = set()
	for row in list(doc.display_rules or []):
		if row.label_size not in CANONICAL_DISPLAY_RULES or row.label_size in seen:
			doc.remove(row)
			updated = True
			continue
		seen.add(row.label_size)
		if is_label_only_rule(row.label_size) and (
			cint(row.show_barcode_number) or cint(row.show_barcode_type)
		):
			row.show_barcode_number = 0
			row.show_barcode_type = 0
			updated = True
		# Batch No/Serial No toggles were added after this table already had
		# rows - a new column is NULL until backfilled, not merely unset.
		defaults = default_display_rule(row.label_size)
		if row.get("show_batch_no") is None:
			row.show_batch_no = defaults["show_batch_no"]
			updated = True
		if row.get("show_serial_no") is None:
			row.show_serial_no = defaults["show_serial_no"]
			updated = True

	for label in CANONICAL_DISPLAY_RULES:
		if label in seen:
			continue
		row = {"label_size": label}
		row.update(default_display_rule(label))
		doc.append("display_rules", row)
		updated = True

	return updated


def sync_item_display_field_rows(doc) -> bool:
	"""Keep extra Item fields someone added. Does not list every Item field.
	Does not save."""
	from barcodes_print.api.item import item_master_field_map

	updated = False
	allowed = item_master_field_map()
	seen = set()
	for row in list(doc.item_display_fields or []):
		fieldname = row.item_field
		if not fieldname or fieldname in seen or fieldname not in allowed:
			doc.remove(row)
			updated = True
			continue
		seen.add(fieldname)
		df = allowed[fieldname]
		if not (row.field_label or "").strip():
			row.field_label = (df.label or fieldname).strip()
			updated = True

	return updated


def ensure_display_rules():
	"""Seed Small, Medium, Large, and one Label Only row per size."""
	if not frappe.db.exists("DocType", "Barcode Print Settings"):
		return

	doc = frappe.get_single("Barcode Print Settings")
	if sync_display_rule_rows(doc):
		doc.save(ignore_permissions=True)


def ensure_item_barcode_types():
	if not frappe.db.exists("DocType", "Item Barcode"):
		return

	meta = frappe.get_meta("Item Barcode")
	current_options = (meta.get_options("barcode_type") or "").split("\n")
	needed_options = [
		"ITF", "ITF-14", "CODE-128", "Code 128", "Code 39",
		"UPC-E", "QR Code", "Data Matrix", "Custom",
	]
	updated = False
	new_options = list(current_options)
	for opt in needed_options:
		if opt not in new_options:
			new_options.append(opt)
			updated = True

	if updated:
		from frappe.custom.doctype.property_setter.property_setter import make_property_setter
		make_property_setter(
			"Item Barcode",
			"barcode_type",
			"options",
			"\n".join(new_options),
			"Text",
			validate_fields_for_doctype=False,
		)


def ensure_purchase_barcode_tracking_fields():
	"""Add a 'Barcodes Printed' counter to Purchase Order Item, Purchase
	Receipt Item, and Subcontracting Order Item, regardless of which one
	Barcode Print Settings currently points to - so switching that setting
	later doesn't need a fresh field creation. Read-only, purely maintained
	by this app's own print flow (see api.zpl.log_print)."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	for doctype, insert_after in (
		("Purchase Order Item", "qty"),
		("Purchase Receipt Item", "qty"),
		("Subcontracting Order Item", "qty"),
	):
		if not frappe.db.exists("DocType", doctype):
			continue
		if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": "custom_barcodes_printed"}):
			continue
		create_custom_field(
			doctype,
			{
				"fieldname": "custom_barcodes_printed",
				"label": "Barcodes Printed",
				"fieldtype": "Int",
				"default": "0",
				"read_only": 1,
				"no_copy": 1,
				"print_hide": 1,
				"insert_after": insert_after,
				"description": "Cumulative barcode labels printed for this line via Barcodes Print.",
			},
		)
