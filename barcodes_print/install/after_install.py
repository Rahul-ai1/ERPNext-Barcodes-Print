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
	"batch_serial_print_mode": "Both",
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
	"show_batch_no": 1,
	"show_serial_no": 1,
}


def after_install():
	sync_standard_files()
	seed_barcode_print_settings()
	ensure_item_barcode_types()
	ensure_item_barcode_default_field()
	ensure_purchase_barcode_tracking_fields()
	ensure_user_default_printer_field()
	ensure_display_rules()


def after_migrate():
	sync_standard_files()
	seed_barcode_print_settings()
	ensure_item_barcode_types()
	ensure_item_barcode_default_field()
	ensure_purchase_barcode_tracking_fields()
	ensure_user_default_printer_field()
	ensure_display_rules()


def ensure_user_default_printer_field():
	"""Each user's own saved label printer (see api.printer) - a Custom
	Field on core User, hidden from the standard User form since it's only
	ever set via the Print Barcode page's own picker, never edited there
	directly."""
	if frappe.db.exists("Custom Field", {"dt": "User", "fieldname": "custom_default_label_printer"}):
		return

	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	create_custom_field(
		"User",
		{
			"fieldname": "custom_default_label_printer",
			"label": "Default Label Printer (Barcodes Print)",
			"fieldtype": "Data",
			"hidden": 1,
			"description": "Set from the Print Barcode page's own \"My Printer\" picker - not editable here.",
		},
	)


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

	# Batch No/Serial No per-size toggles were added, removed, then
	# re-added as their own fields - a NULL column needs explicit
	# backfilling on an existing row, not just "left unset".
	for row in doc.display_rules or []:
		if row.get("show_batch_no") is None:
			row.show_batch_no = DISPLAY_RULE_DEFAULTS["show_batch_no"]
			updated = True
		if row.get("show_serial_no") is None:
			row.show_serial_no = DISPLAY_RULE_DEFAULTS["show_serial_no"]
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
		# EAN-13/GTIN-14 are vanilla v16 options, but Frappe v15 calls the
		# first one "EAN-12" and never shipped the second at all - setting
		# either as a barcode_type on v15 would otherwise silently blank
		# back to "" on save, since it isn't a valid Select option there.
		"EAN-13", "GTIN-14",
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


def ensure_item_barcode_default_field():
	"""Adds the 'Default' checkbox used by the Print Barcode page to
	auto-select a barcode when an item has more than one (see
	overrides.item.validate_single_default_barcode for the exclusivity
	check - this only creates the field, no backfill)."""
	if not frappe.db.exists("DocType", "Item Barcode"):
		return
	if frappe.db.exists("Custom Field", {"dt": "Item Barcode", "fieldname": "custom_default"}):
		return

	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	create_custom_field(
		"Item Barcode",
		{
			"fieldname": "custom_default",
			"label": "Default",
			"fieldtype": "Check",
			"insert_after": "barcode_type",
			"description": "Only one barcode per item can be marked Default. Used by Barcodes Print to auto-select a barcode when an item has more than one.",
		},
	)


def ensure_purchase_barcode_tracking_fields():
	"""Add a 'Barcodes Printed' counter to Purchase Order Item, Purchase
	Receipt Item, Purchase Invoice Item, and Subcontracting Order Item,
	regardless of which one Barcode Print Settings currently points to - so
	switching that setting later doesn't need a fresh field creation.
	Read-only, purely maintained by this app's own print flow (see
	api.zpl.log_print)."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_field

	for doctype, insert_after in (
		("Purchase Order Item", "qty"),
		("Purchase Receipt Item", "qty"),
		("Purchase Invoice Item", "qty"),
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
