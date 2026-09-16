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
STANDARD_FILES = [
	("workspace_sidebar", "barcodes_print.json"),
	("desktop_icon", "barcodes_print.json"),
]


def sync_standard_files():
	for folder, filename in STANDARD_FILES:
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

# Fallback values for a fresh Display Rule row - matches the app's original
# global toggle defaults, before per-size rules existed.
DISPLAY_RULE_DEFAULTS = {
	"alignment": "Left",
	"show_item_name": 1,
	"show_item_code": 1,
	"show_barcode_number": 1,
	"show_barcode_type": 1,
	"show_uom": 1,
	"show_rate": 0,
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


def seed_barcode_print_settings():
	if frappe.db.exists("DocType", "Barcode Print Settings"):
		doc = frappe.get_single("Barcode Print Settings")
		for field, value in DEFAULTS.items():
			if doc.get(field) in (None, ""):
				doc.set(field, value)
		doc.save(ignore_permissions=True)


def ensure_display_rules():
	"""Every label size needs its own Display Rule row (what to show +
	alignment) - seed the three fixed sizes if any are missing, so this
	mandatory Table field is never left empty on a fresh install or for an
	existing site upgrading from the old global-toggle-only version."""
	if not frappe.db.exists("DocType", "Barcode Print Settings"):
		return

	doc = frappe.get_single("Barcode Print Settings")
	existing_sizes = {row.label_size for row in (doc.display_rules or [])}
	updated = False
	for size in ("Small", "Medium", "Large"):
		if size in existing_sizes:
			continue
		row = {"label_size": size}
		row.update(DISPLAY_RULE_DEFAULTS)
		doc.append("display_rules", row)
		updated = True

	if updated:
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
	"""Add a 'Barcodes Printed' counter to both Purchase Order Item and
	Purchase Receipt Item, regardless of which one Barcode Print Settings
	currently points to - so switching that setting later doesn't need a
	fresh field creation. Read-only, purely maintained by this app's own
	print flow (see api.zpl.log_print)."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	for doctype, insert_after in (("Purchase Order Item", "qty"), ("Purchase Receipt Item", "qty")):
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
