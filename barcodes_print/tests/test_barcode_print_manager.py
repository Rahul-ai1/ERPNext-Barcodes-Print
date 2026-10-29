# Copyright (c) 2026, Rahul Chaudhary and contributors
# See license.txt

import random
import re
import frappe
from frappe.tests.utils import FrappeTestCase

from barcodes_print.api.barcode import (
	base_size_of,
	get_barcode_print_settings,
	is_label_only_size,
	validate_barcode_compatibility,
)
from barcodes_print.api.import_items import import_items_from_file
from barcodes_print.api.item import (
	batch_belongs_to_item,
	get_item_barcode_details,
	get_item_rate_and_note,
	get_item_stock_qty,
	get_item_tax_rate_percent,
	resolve_scan,
	serial_no_item_and_batch,
)
from barcodes_print.api.printer import get_my_default_printer, set_my_default_printer
from barcodes_print.api.zpl import (
	_format_price_line,
	_get_batch_serial_map,
	build_label_zpl,
	get_label_dimensions_dots,
	get_purchase_document_items,
	get_quick_print_job,
	is_purchase_print_enabled_for,
	log_print,
	mm_to_dots,
	resolve_row,
)


def _random_ean13() -> str:
	"""Generate a unique valid 12-digit base and calculate the EAN-13 checksum."""
	digits = [random.randint(0, 9) for _ in range(12)]
	odd_sum = sum(digits[i] for i in range(0, 12, 2))
	even_sum = sum(digits[i] for i in range(1, 12, 2))
	total = odd_sum + (even_sum * 3)
	check_digit = (10 - (total % 10)) % 10
	return "".join(str(d) for d in digits) + str(check_digit)


def _random_itf() -> str:
	"""Generate a random even-length numeric string for ITF."""
	return "".join(str(random.randint(0, 9)) for _ in range(12))


def _random_code128() -> str:
	"""Generate a unique alphanumeric code for Code 128."""
	return f"CODE-{frappe.generate_hash(length=8).upper()}"


class TestBarcodePrintManager(FrappeTestCase):
	def setUp(self):
		self._ensure_core_masters()
		settings = frappe.get_single("Barcode Print Settings")
		settings.set("display_rules", [])
		for size in ("Small", "Medium", "Large"):
			settings.append("display_rules", {
				"label_size": size,
				"alignment": "Left",
				"show_item_name": 1,
				"show_item_code": 1,
				"show_uom": 1,
				"show_barcode_number": 1,
				"show_barcode_type": 1,
				"show_rate": 0,
				"show_batch_no": 1,
				"show_serial_no": 1,
			})
		settings.batch_serial_print_mode = "Both"
		settings.print_connector = "QZ Tray"
		settings.printer_dpi = "203"
		settings.small_width_mm = 50
		settings.small_height_mm = 25
		settings.medium_width_mm = 75
		settings.medium_height_mm = 50
		settings.large_width_mm = 100
		settings.large_height_mm = 75
		settings.save(ignore_permissions=True)

	def _ensure_core_masters(self):
		"""Every test item in this file hardcodes item_group="All Item
		Groups" and stock_uom="Nos". Both are normally created by
		ERPNext's setup wizard - a fresh CI site never runs that, so
		create them once if missing rather than assuming they exist."""
		if not frappe.db.exists("Item Group", "All Item Groups"):
			frappe.get_doc({
				"doctype": "Item Group",
				"item_group_name": "All Item Groups",
				"is_group": 1,
			}).insert(ignore_permissions=True)
		for uom in ("Nos", "Box"):
			if not frappe.db.exists("UOM", uom):
				frappe.get_doc({
					"doctype": "UOM",
					"uom_name": uom,
				}).insert(ignore_permissions=True)
		if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
			frappe.get_doc({
				"doctype": "Supplier Group",
				"supplier_group_name": "All Supplier Groups",
				"is_group": 1,
			}).insert(ignore_permissions=True)
		if not frappe.get_single_value("Stock Settings", "enable_serial_and_batch_no_for_item"):
			frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
		if not frappe.db.exists("Warehouse Type", "Transit"):
			# Company.create_default_warehouses() always creates a "Goods In
			# Transit" warehouse of this type - needed before any Company
			# insert, not just before a Warehouse insert.
			frappe.get_doc({
				"doctype": "Warehouse Type",
				"name": "Transit",
			}).insert(ignore_permissions=True)

	def _ensure_fiscal_year_for_company(self, company_name):
		"""A Purchase Order needs a Fiscal Year covering today's date for its
		company - normally seeded by the setup wizard. Rather than add a
		new global one (which can collide with an unrelated, even disabled,
		Fiscal Year's date range on a bench with real pre-existing data),
		scope it to just this company - that can never overlap since
		ERPNext's own overlap check only compares within the same company
		scope (or between two fully global entries)."""
		today = frappe.utils.getdate()
		candidates = frappe.get_all(
			"Fiscal Year",
			filters={"year_start_date": ["<=", today], "year_end_date": [">=", today], "disabled": 0},
			pluck="name",
		)
		if candidates:
			restrictions = frappe.get_all(
				"Fiscal Year Company",
				filters={"parent": ["in", candidates]},
				fields=["parent", "company"],
			)
			companies_by_parent = {}
			for r in restrictions:
				companies_by_parent.setdefault(r.parent, set()).add(r.company)
			usable = any(
				name not in companies_by_parent or company_name in companies_by_parent[name]
				for name in candidates
			)
		else:
			usable = False

		if not usable:
			frappe.get_doc({
				"doctype": "Fiscal Year",
				"year": f"_Test FY {today.year} {frappe.generate_hash(length=4)}",
				"year_start_date": today.replace(month=1, day=1),
				"year_end_date": today.replace(month=12, day=31),
				"companies": [{"company": company_name}],
			}).insert(ignore_permissions=True)

	def _set_display_rule(self, settings_doc, size, **overrides):
		"""Test helper: update one size's Display Rule row in place and save."""
		for row in settings_doc.display_rules:
			if row.label_size == size:
				row.update(overrides)
				break
		settings_doc.save(ignore_permissions=True)

	def _get_or_create_company(self):
		"""A dev bench that's been used for real work already has a Company;
		a fresh CI site (no setup wizard run) has none - create a minimal
		one rather than assuming it exists. Creating it also triggers
		ERPNext's own default-warehouse creation for that company."""
		existing = frappe.get_all("Company", limit=1)
		if existing:
			company_name = existing[0].name
		else:
			company_name = frappe.get_doc({
				"doctype": "Company",
				"company_name": "_Test Company",
				"abbr": "_TC",
				"default_currency": "INR",
				"country": "India",
			}).insert(ignore_permissions=True).name
		self._ensure_fiscal_year_for_company(company_name)
		self._ensure_company_cost_center(company_name)
		return company_name

	def _ensure_company_cost_center(self, company_name):
		"""A freshly-inserted Company always gets a working default Cost
		Center from ERPNext's own Company controller - but a dev bench's
		pre-existing real Company may have been set up without ever running
		the full Setup Wizard, leaving this blank. Purchase Invoice
		submission posts GL entries immediately and throws if no Cost
		Center resolves for a P&L account, unlike Purchase Order/Receipt -
		so this is only actually needed before creating a Purchase Invoice,
		but is harmless to ensure for every company regardless."""
		if frappe.db.get_value("Company", company_name, "cost_center"):
			return
		existing = frappe.get_all("Cost Center", filters={"company": company_name, "is_group": 0}, limit=1)
		if existing:
			cost_center = existing[0].name
		else:
			abbr = frappe.db.get_value("Company", company_name, "abbr")
			cost_center = frappe.get_doc({
				"doctype": "Cost Center",
				"cost_center_name": "Main",
				"company": company_name,
				"is_group": 0,
			}).insert(ignore_permissions=True).name
		frappe.db.set_value("Company", company_name, "cost_center", cost_center)

	def _get_or_create_supplier(self):
		existing = frappe.get_all("Supplier", limit=1)
		if existing:
			return existing[0].name
		supplier_group = frappe.get_all("Supplier Group", limit=1)
		return frappe.get_doc({
			"doctype": "Supplier",
			"supplier_name": "_Test Supplier",
			"supplier_group": supplier_group[0].name if supplier_group else "All Supplier Groups",
			"supplier_type": "Company",
		}).insert(ignore_permissions=True).name

	def _get_or_create_price_list(self):
		"""A dev bench that's run the setup wizard already has the standard
		Buying/Selling Price Lists; a fresh CI site has none - create a
		minimal selling one rather than assuming it exists."""
		existing = frappe.get_all("Price List", filters={"selling": 1}, limit=1)
		if existing:
			return existing[0].name
		return frappe.get_doc({
			"doctype": "Price List",
			"price_list_name": "_Test Price List",
			"selling": 1,
			"currency": "INR",
		}).insert(ignore_permissions=True).name

	def test_autofetch_case1_barcode_available(self):
		"""Case 1: Item with barcode in Item Master -> resolve_row auto-fetches
		Item Name, UOM, Barcode, Barcode Type."""
		barcode_val = _random_ean13()
		item_code = f"TEST-ITEM-AVAIL-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Available Barcode Item",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"standard_rate": 250.0,
			"barcodes": [{"barcode": barcode_val, "barcode_type": "EAN-13", "uom": "Nos"}]
		}).insert(ignore_permissions=True)

		details = get_item_barcode_details(item_code)
		self.assertEqual(details.get("item_name"), "Available Barcode Item")
		self.assertEqual(details.get("barcode"), barcode_val)
		self.assertEqual(details.get("barcode_type"), "EAN-13")

		row = resolve_row(item_code, no_of_barcodes=1)
		self.assertEqual(row.item_name, "Available Barcode Item")
		self.assertEqual(row.uom, "Nos")
		self.assertEqual(row.barcode, barcode_val)
		self.assertEqual(row.barcode_type, "EAN-13")

	def test_autofetch_case2_barcode_not_available(self):
		"""Case 2: Item without barcode in Item Master -> resolve_row blocks
		with the plan's exact business rule 5.2 message unless a barcode is
		supplied directly."""
		item_code = f"TEST-ITEM-NO-BARCODE-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Item Without Barcode",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"standard_rate": 100.0,
		}).insert(ignore_permissions=True)

		details = get_item_barcode_details(item_code)
		self.assertEqual(details.get("barcode"), "")

		with self.assertRaises(frappe.ValidationError):
			resolve_row(item_code, no_of_barcodes=1)

		row = resolve_row(item_code, barcode="MANUAL12345", barcode_type="CODE-128", no_of_barcodes=1)
		self.assertEqual(row.item, item_code)
		self.assertEqual(row.barcode, "MANUAL12345")
		self.assertEqual(row.barcode_type, "CODE-128")

	def test_mandatory_barcode_only(self):
		"""Business rule 5.2: only a missing Barcode blocks - Barcode Type is
		optional and gets auto-detected/backfilled when omitted."""
		item_code = f"TEST-ITEM-MANDATORY-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Mandatory Test Item",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			resolve_row(item_code, barcode_type="CODE-128", no_of_barcodes=1)

		row = resolve_row(item_code, barcode="12345678", no_of_barcodes=1)
		self.assertTrue((row.barcode_type or "").strip())

	# ---- Feature: "Label Only" sizes (barcodes_print.api.barcode/zpl) ----

	def _create_item_without_barcode(self):
		item_code = f"TEST-ITEM-NO-BARCODE-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "No Barcode Test Item",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)
		return item_code

	def test_is_label_only_size_and_base_size_of(self):
		self.assertTrue(is_label_only_size("Small Label Only"))
		self.assertTrue(is_label_only_size("Medium Label Only"))
		self.assertFalse(is_label_only_size("Small"))
		self.assertFalse(is_label_only_size(""))
		self.assertEqual(base_size_of("Small Label Only"), "Small")
		self.assertEqual(base_size_of("Large Label Only"), "Large")
		self.assertEqual(base_size_of("Medium"), "Medium")

	def test_get_label_dimensions_dots_label_only_reuses_base_size(self):
		"""A Label Only choice must print on the exact same physical label
		as its base size - no separate dimension fields exist for it."""
		settings = get_barcode_print_settings()
		self.assertEqual(
			get_label_dimensions_dots(settings, "Small Label Only"),
			get_label_dimensions_dots(settings, "Small"),
		)

	def test_resolve_row_label_only_skips_barcode_requirement(self):
		"""The one behavior change this feature makes: an Item with no
		barcode, which would otherwise hard-error, resolves cleanly when a
		Label Only size is explicitly chosen."""
		item_code = self._create_item_without_barcode()
		row = resolve_row(item_code, no_of_barcodes=2, size="Small Label Only")
		self.assertEqual(row.barcode, "")
		self.assertEqual(row.no_of_barcodes, 2)

	def test_resolve_row_existing_sizes_still_require_barcode(self):
		"""The hard constraint this feature must never weaken: a real
		Small/Medium/Large size (or no size at all) still throws on a
		missing barcode, exactly as before this feature existed."""
		item_code = self._create_item_without_barcode()
		with self.assertRaises(frappe.ValidationError):
			resolve_row(item_code, no_of_barcodes=1, size="Small")
		with self.assertRaises(frappe.ValidationError):
			resolve_row(item_code, no_of_barcodes=1)  # no size passed at all

	def test_resolve_row_label_only_strips_batch_and_serial(self):
		"""Batch/Serial selection isn't offered for a Label Only size -
		enforced server-side (defense in depth) by stripping it before the
		batch-ownership check even runs, not just hidden client-side."""
		item_code, _ = self._create_batch_tracked_item()
		row = resolve_row(item_code, no_of_barcodes=1, batch_no="NO-SUCH-BATCH", size="Medium Label Only")
		self.assertEqual(row.batch_no, "")
		self.assertEqual(row.barcode, "")

	def test_build_label_zpl_label_only_prints_text_without_a_barcode(self):
		settings = get_barcode_print_settings()
		settings["display_rules"]["Medium Label Only"]["show_rate"] = 1
		row = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X", barcode="", barcode_type="",
			uom="Nos", rate=42, price_note="Excl. of taxes",
		)
		zpl = build_label_zpl(row, settings, "Medium Label Only")
		self.assertIn("^XA", zpl)
		self.assertIn("^XZ", zpl)
		self.assertIn("Item X", zpl)
		self.assertIn(frappe.utils.fmt_money(42), zpl)
		# No barcode field command of any kind - ^B. is the common prefix
		# for every symbology this app emits (^BC/^BE/^B3/^BQ/^BX/etc.).
		self.assertNotRegex(zpl, r"\^B[A-Z0-9]")

	def test_get_quick_print_job_label_only_size_end_to_end(self):
		"""Full integration: an Item with no barcode, which would normally
		block printing entirely, succeeds end-to-end at a Label Only size."""
		item_code = self._create_item_without_barcode()
		job = get_quick_print_job([{"item": item_code, "no_of_barcodes": 3}], "Small Label Only")
		self.assertEqual(job["total_labels"], 3)
		self.assertEqual(job["zpl"].count("^XA"), 3)
		self.assertNotRegex(job["zpl"], r"\^B[A-Z0-9]")

	def test_item_master_barcode_types_ean_itf_code128(self):
		"""Important Requirement: Use the exact barcode type from Item Master (EAN-13, ITF, CODE-128)."""
		ean_val = _random_ean13()
		itf_val = _random_itf()
		code128_val = _random_code128()

		item_a = f"TEST-ITEM-EAN13-{frappe.generate_hash(length=6)}"
		item_b = f"TEST-ITEM-ITF-{frappe.generate_hash(length=6)}"
		item_c = f"TEST-ITEM-CODE128-{frappe.generate_hash(length=6)}"

		frappe.get_doc({
			"doctype": "Item", "item_code": item_a, "item_name": "Item A (EAN-13)",
			"item_group": "All Item Groups", "is_sales_item": 0, "stock_uom": "Nos",
			"barcodes": [{"barcode": ean_val, "barcode_type": "EAN-13"}]
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Item", "item_code": item_b, "item_name": "Item B (ITF)",
			"item_group": "All Item Groups", "is_sales_item": 0, "stock_uom": "Nos",
			"barcodes": [{"barcode": itf_val, "barcode_type": "ITF"}]
		}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Item", "item_code": item_c, "item_name": "Item C (Code 128)",
			"item_group": "All Item Groups", "is_sales_item": 0, "stock_uom": "Nos",
			"barcodes": [{"barcode": code128_val, "barcode_type": "CODE-128"}]
		}).insert(ignore_permissions=True)

		self.assertEqual(resolve_row(item_a, no_of_barcodes=1).barcode_type, "EAN-13")
		self.assertEqual(resolve_row(item_b, no_of_barcodes=1).barcode_type, "ITF")
		self.assertEqual(resolve_row(item_c, no_of_barcodes=1).barcode_type, "CODE-128")

	def test_multiple_barcodes_per_item_selection(self):
		"""Selecting a specific barcode for an item with multiple barcodes retrieves matching type and UOM."""
		ean_val = _random_ean13()
		itf_val = _random_itf()

		item_code = f"TEST-ITEM-MULTI-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Item Multiple Barcodes",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"barcodes": [
				{"barcode": ean_val, "barcode_type": "EAN-13", "uom": "Nos"},
				{"barcode": itf_val, "barcode_type": "ITF", "uom": "Box"},
			]
		}).insert(ignore_permissions=True)

		# With 2+ barcodes and none specified, the server deliberately
		# leaves barcode/barcode_type blank rather than silently guessing
		# the first one - it hands back the full list instead, so the
		# caller (the Print Barcode page) can ask the user to choose.
		details1 = get_item_barcode_details(item_code)
		self.assertEqual(details1.get("barcode"), "")
		self.assertEqual(details1.get("barcode_type"), "")
		self.assertEqual(len(details1.get("barcodes")), 2)
		self.assertEqual({b["barcode"] for b in details1["barcodes"]}, {ean_val, itf_val})

		# An explicit barcode is unambiguous - still resolves directly.
		details2 = get_item_barcode_details(item_code, barcode=itf_val)
		self.assertEqual(details2.get("barcode"), itf_val)
		self.assertEqual(details2.get("barcode_type"), "ITF")
		self.assertEqual(details2.get("barcode_uom"), "Box")

		# resolve_row must block with a distinct "select one" message when
		# an item has multiple barcodes and none was chosen - not the
		# generic "no barcode at all" message.
		with self.assertRaises(frappe.ValidationError) as ctx:
			resolve_row(item_code, no_of_barcodes=1)
		self.assertIn("2 barcodes", str(ctx.exception))

	def test_default_barcode_is_auto_selected_among_multiple(self):
		"""One row marked Default among 2+ barcodes is auto-picked instead
		of being left blank for the user to choose."""
		item_code = f"TEST-ITEM-DEFAULT-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Item With Default Barcode",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"barcodes": [
				{"barcode": "CODE-NOTDEFAULT", "barcode_type": "Code 128"},
				{"barcode": "CODE-ISDEFAULT", "barcode_type": "Code 128", "custom_default": 1},
			],
		}).insert(ignore_permissions=True)

		details = get_item_barcode_details(item_code)
		self.assertEqual(details.get("barcode"), "CODE-ISDEFAULT")
		self.assertEqual(details.get("barcode_type"), "Code 128")
		default_flags = {b["barcode"]: b["default"] for b in details["barcodes"]}
		self.assertEqual(default_flags, {"CODE-NOTDEFAULT": 0, "CODE-ISDEFAULT": 1})

	def test_item_save_blocks_more_than_one_default_barcode(self):
		"""Only one Barcodes row per Item may be marked Default - saving
		with 2+ marked throws instead of silently picking one, so a Data
		Import/API write can't leave the item in an ambiguous state."""
		item_code = f"TEST-ITEM-2DEFAULTS-{frappe.generate_hash(length=6)}"
		item = frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Item With Two Defaults",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"barcodes": [
				{"barcode": "CODE-ONE", "barcode_type": "Code 128", "custom_default": 1},
				{"barcode": "CODE-TWO", "barcode_type": "Code 128", "custom_default": 1},
			],
		})
		with self.assertRaises(frappe.ValidationError):
			item.insert(ignore_permissions=True)

	def test_get_barcode_print_settings_defaults(self):
		settings = get_barcode_print_settings()
		self.assertIn("display_rules", settings)
		self.assertIn("Small", settings["display_rules"])
		self.assertIn("Medium", settings["display_rules"])
		self.assertIn("Large", settings["display_rules"])
		self.assertIn("show_barcode_type", settings["display_rules"]["Medium"])
		self.assertIn("alignment", settings["display_rules"]["Medium"])
		self.assertIn("print_connector", settings)
		self.assertIn("default_label_printer", settings)
		self.assertIn("printer_dpi", settings)
		self.assertEqual(settings["print_connector"], "QZ Tray")
		self.assertEqual(settings["printer_dpi"], 203)
		self.assertEqual(settings["medium_width_mm"], 75)
		self.assertEqual(settings["medium_height_mm"], 50)

	def test_display_rules_are_independent_per_size(self):
		"""The whole point of the per-size table: a field can be off for
		Small but on for Medium/Large, and alignment can differ per size."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		try:
			self._set_display_rule(settings_doc, "Small", show_barcode_type=0, alignment="Left")
			self._set_display_rule(settings_doc, "Medium", show_barcode_type=1, alignment="Center")
			self._set_display_rule(settings_doc, "Large", show_barcode_type=1, alignment="Right")

			rules = get_barcode_print_settings()["display_rules"]
			self.assertEqual(rules["Small"]["show_barcode_type"], 0)
			self.assertEqual(rules["Small"]["alignment"], "Left")
			self.assertEqual(rules["Medium"]["show_barcode_type"], 1)
			self.assertEqual(rules["Medium"]["alignment"], "Center")
			self.assertEqual(rules["Large"]["alignment"], "Right")
		finally:
			self.setUp()

	def test_invalid_barcode_value_validation(self):
		"""Test that invalid barcode values produce clear validation messages and are rejected."""
		is_valid, _fmt, err_msg = validate_barcode_compatibility("INVALID_TEXT", "UPC-A")
		self.assertFalse(is_valid)
		self.assertTrue(err_msg)

		is_valid, _fmt, err_msg = validate_barcode_compatibility("1234", "EAN-8")
		self.assertFalse(is_valid)

		item_code = f"TEST-INVAL-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item", "item_code": item_code, "item_name": "Invalid Item",
			"item_group": "All Item Groups", "is_sales_item": 0, "stock_uom": "Nos",
		}).insert(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			resolve_row(item_code, barcode="ABCDE", barcode_type="UPC-A", no_of_barcodes=1)

	def test_blank_barcode_handling(self):
		"""Blank barcode value is never valid."""
		is_valid, _fmt, err_msg = validate_barcode_compatibility("", "UPC-A")
		self.assertFalse(is_valid)
		self.assertTrue(err_msg)

	def test_build_label_zpl_uses_correct_barcode_command_per_type(self):
		"""Business rule 5.3: each barcode type maps to its dedicated ZPL barcode
		field command, not a generic/plain-text render. Covers every barcode
		type this app recognizes (api.barcode.BARCODE_TYPE_MAP), not just the
		plan's 5-type priority list - including the ones that intentionally
		fall back to the universal Code 128 command (ISBN/ISSN/JAN/PZN/NW-7:
		no dedicated ZPL symbology exists for these)."""
		settings = get_barcode_print_settings()
		expectations = {
			"EAN-13": ("8901030388309", "^BE"),
			"EAN-8": ("12345670", "^B8"),
			"UPC-A": ("012345678905", "^BU"),
			"UPC-E": ("0123456", "^B9"),
			"CODE-39": ("ABC123", "^B3"),
			"CODE-128": ("SKU002-12345", "^BC"),
			"ITF": ("123456789012", "^B2"),
			"ITF-14": ("12345678901231", "^B2"),
			"GTIN-14": ("12345678901231", "^B2"),
			"CODABAR": ("A123456A", "^BK"),
			"QR Code": ("https://example.com/item/ABC123", "^BQ"),
			"Data Matrix": ("DM-TEST-PAYLOAD", "^BX"),
			"ISBN-13": ("978030640615", "^BC"),
			"ISBN-10": ("030640615", "^BC"),
			"ISSN": ("2049312", "^BC"),
			"JAN": ("490123456789", "^BC"),
			"PZN": ("123456", "^BC"),
			"NW-7": ("A123456A", "^BC"),
		}
		for barcode_type, (value, expected_command) in expectations.items():
			row = frappe._dict(
				idx=1, item="ITEM-X", item_name="Item X",
				barcode=value, barcode_type=barcode_type, uom="Nos", rate=0,
			)
			zpl = build_label_zpl(row, settings, "Medium")
			self.assertIn(expected_command, zpl, f"{barcode_type} did not use {expected_command}")
			self.assertIn("^XA", zpl)
			self.assertIn("^XZ", zpl)

	def test_fixed_length_barcode_types_reject_wrong_digit_count(self):
		"""python-barcode's classes reject too-few digits but silently accept
		(and truncate) too-many for EAN/UPC/ISBN-family symbologies - the app
		must enforce the exact expected length itself, or a barcode that's
		too long would validate as fine but print/scan as a different,
		wrong value."""
		too_long = {
			"EAN-13": "1234567890123456",
			"EAN-8": "123456789012",
			"UPC-A": "12345678901234",
			"ISBN-13": "9780306406157999",
		}
		for barcode_type, value in too_long.items():
			is_valid, _fmt, err_msg = validate_barcode_compatibility(value, barcode_type)
			self.assertFalse(is_valid, f"{barcode_type} should reject an over-length value")
			self.assertIn("digits", err_msg)

	def test_auto_detect_never_picks_itf_for_short_codes(self):
		"""Real bug found via a live scan test: a short internal code with no
		declared Barcode Type was being auto-detected as ITF purely because
		it had an even digit count. ITF (Interleaved 2 of 5) is a niche
		industrial symbology most generic/phone-camera scanners don't
		support at all, so labels silently failed to scan even though the
		text value was registered correctly. Business rule 5.3's auto-detect
		priority order (EAN-13 -> EAN-8 -> UPC -> Code 39 -> Code 128) has
		no ITF in it at all - auto-detect must never resolve to ITF; only an
		explicitly chosen Barcode Type should use it.

		Uses randomly generated values (not small fixed numbers) so this
		test can't collide with real Item Barcode records already committed
		on this shared site - validate_barcode_compatibility falls back to
		looking up any existing Item Barcode with a matching value when no
		type is given, which would make a fixed test value fragile."""
		for _ in range(5):
			value = str(random.randint(10, 999998) * 2)
			is_valid, resolved_fmt, _err = validate_barcode_compatibility(value, "")
			self.assertTrue(is_valid)
			self.assertNotEqual(resolved_fmt, "itf", f"'{value}' auto-detected as ITF")

		is_valid, resolved_fmt, _err = validate_barcode_compatibility("1234", "ITF")
		self.assertTrue(is_valid)
		self.assertEqual(resolved_fmt, "itf")

	def test_barcode_type_is_optional_and_auto_detected(self):
		"""Barcode Type is optional: when left blank, the format is
		auto-detected from the Barcode value and backfilled as a
		human-readable label, rather than blocking."""
		item_code = f"TEST-ITEM-NOTYPE-{frappe.generate_hash(length=6)}"
		ean_val = _random_ean13()
		frappe.get_doc({
			"doctype": "Item", "item_code": item_code, "item_name": "No Barcode Type Item",
			"item_group": "All Item Groups", "is_sales_item": 0, "stock_uom": "Nos",
		}).insert(ignore_permissions=True)

		row = resolve_row(item_code, barcode=ean_val, no_of_barcodes=1)
		self.assertEqual(row.barcode_type, "EAN-13")

	def test_build_label_zpl_no_duplicate_interpretation_line(self):
		"""The barcode command's own built-in human-readable line must stay
		off. With Barcode Number shown, the value should appear exactly
		twice (the ^FD data field that encodes it, plus our one text
		field) - three would mean the barcode command's own interpretation
		line is also printing it. With Barcode Number hidden, it should
		appear exactly once (only the encoding)."""
		row = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="8901030388301", barcode_type="EAN-13", uom="Nos", rate=0,
		)

		settings = get_barcode_print_settings()
		settings["display_rules"]["Medium"]["show_barcode_number"] = 1
		zpl = build_label_zpl(row, settings, "Medium")
		self.assertEqual(zpl.count(row.barcode), 2)

		settings["display_rules"]["Medium"]["show_barcode_number"] = 0
		zpl = build_label_zpl(row, settings, "Medium")
		self.assertEqual(zpl.count(row.barcode), 1)

	def test_get_quick_print_job_repeats_per_no_of_barcodes(self):
		item_code = self._create_test_item_with_barcode()
		job = get_quick_print_job([{"item": item_code, "no_of_barcodes": 3}], "Medium")
		self.assertEqual(job["total_labels"], 3)
		self.assertEqual(job["zpl"].count("^XA"), 3)
		self.assertEqual(job["zpl"].count("^XZ"), 3)
		self.assertEqual(job["connector"], "QZ Tray")
		self.assertEqual(len(job["resolved_items"]), 1)

	def test_get_quick_print_job_multiple_items(self):
		item_a = self._create_test_item_with_barcode()
		item_b = self._create_test_item_with_barcode()
		job = get_quick_print_job(
			[{"item": item_a, "no_of_barcodes": 2}, {"item": item_b, "no_of_barcodes": 1}], "Small"
		)
		self.assertEqual(job["total_labels"], 3)
		self.assertEqual(len(job["resolved_items"]), 2)

	def test_quick_print_job_prefers_users_own_printer_over_site_default(self):
		"""Fallback chain: the calling user's own saved printer (see
		api.printer) wins over Barcode Print Settings' site-wide default."""
		item_code = self._create_test_item_with_barcode()
		settings_doc = frappe.get_single("Barcode Print Settings")
		original_site_printer = settings_doc.default_label_printer
		original_my_printer = get_my_default_printer()
		settings_doc.default_label_printer = "Site-Wide-Printer"
		settings_doc.save(ignore_permissions=True)

		try:
			job = get_quick_print_job([{"item": item_code, "no_of_barcodes": 1}], "Medium")
			self.assertEqual(job["printer"], "Site-Wide-Printer")

			set_my_default_printer("My-Own-Printer")
			job = get_quick_print_job([{"item": item_code, "no_of_barcodes": 1}], "Medium")
			self.assertEqual(job["printer"], "My-Own-Printer")
		finally:
			set_my_default_printer(original_my_printer)
			settings_doc.default_label_printer = original_site_printer
			settings_doc.save(ignore_permissions=True)

	def test_get_quick_print_job_requires_at_least_one_item(self):
		with self.assertRaises(frappe.ValidationError):
			get_quick_print_job([], "Medium")

	def test_get_quick_print_job_rejects_invalid_size(self):
		item_code = self._create_test_item_with_barcode()
		with self.assertRaises(frappe.ValidationError):
			get_quick_print_job([{"item": item_code, "no_of_barcodes": 1}], "Huge")

	def test_log_print_creates_audit_entries(self):
		item_code = self._create_test_item_with_barcode()
		job = get_quick_print_job([{"item": item_code, "no_of_barcodes": 5}], "Medium")
		log_print(job["resolved_items"], "Medium")

		logs = frappe.get_all(
			"Barcode Print Log",
			filters={"item": item_code},
			fields=["item", "no_of_barcodes", "printed_count", "failed_count", "status", "label_size", "printed_by"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].no_of_barcodes, 5)
		self.assertEqual(logs[0].printed_count, 5)
		self.assertEqual(logs[0].failed_count, 0)
		self.assertEqual(logs[0].status, "Success")
		self.assertEqual(logs[0].label_size, "Medium")
		self.assertEqual(logs[0].printed_by, frappe.session.user)

	def test_log_print_records_failure_with_reason(self):
		"""QZ Tray/Zebra Browser Print send one combined job per print
		action, so failure is only knowable per batch - a failed print
		must still be logged, with none of its labels counted as printed
		and the reason captured for troubleshooting."""
		item_code = self._create_test_item_with_barcode()
		job = get_quick_print_job([{"item": item_code, "no_of_barcodes": 3}], "Medium")
		log_print(job["resolved_items"], "Medium", status="Failed", failure_reason="Could not connect to QZ Tray.")

		logs = frappe.get_all(
			"Barcode Print Log",
			filters={"item": item_code},
			fields=["status", "printed_count", "failed_count", "failure_reason"],
		)
		self.assertEqual(len(logs), 1)
		self.assertEqual(logs[0].status, "Failed")
		self.assertEqual(logs[0].printed_count, 0)
		self.assertEqual(logs[0].failed_count, 3)
		self.assertEqual(logs[0].failure_reason, "Could not connect to QZ Tray.")

	def test_qr_code_blocked_on_small_label_when_too_small_to_scan(self):
		"""A QR code needs >=10mm to scan reliably (industry standard) - a
		Small label with several display toggles on leaves less room than
		that, and must be blocked rather than silently print an
		unscannable code."""
		settings = get_barcode_print_settings()
		settings["display_rules"]["Small"].update({
			"show_item_name": 1, "show_item_code": 1, "show_barcode_number": 1,
			"show_barcode_type": 1, "show_uom": 1, "show_rate": 1,
		})
		row = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="https://example.com/x", barcode_type="QR Code", uom="Nos", rate=10,
		)
		with self.assertRaises(frappe.ValidationError):
			build_label_zpl(row, settings, "Small")

	def test_qr_code_allowed_on_large_label(self):
		settings = get_barcode_print_settings()
		row = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="https://example.com/x", barcode_type="QR Code", uom="Nos", rate=0,
		)
		zpl = build_label_zpl(row, settings, "Large")
		self.assertIn("^BQ", zpl)

	def test_alignment_left_starts_at_the_margin(self):
		settings = get_barcode_print_settings()
		settings["display_rules"]["Medium"]["alignment"] = "Left"
		row = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="ABC123", barcode_type="CODE-39", uom="Nos", rate=0,
		)
		zpl = build_label_zpl(row, settings, "Medium")
		# The barcode field's ^FO x-origin should sit at the standard
		# margin (12 dots at 203 DPI for the 1.5mm margin), not shifted.
		self.assertIn("^FO12,", zpl)

	def test_rate_line_omitted_entirely_when_no_price_found(self):
		"""show_rate=1 but no price found (rate=None) - the Rate line is
		left off the label entirely, never printed as "N/A" or "0.00"."""
		settings = get_barcode_print_settings()
		settings["display_rules"]["Medium"]["show_rate"] = 1

		row_no_price = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="ABC123", barcode_type="CODE-39", uom="Nos", rate=None,
		)
		zpl_no_price = build_label_zpl(row_no_price, settings, "Medium")
		self.assertNotIn("N/A", zpl_no_price)
		self.assertNotIn("0.00", zpl_no_price)

		row_with_price = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="ABC123", barcode_type="CODE-39", uom="Nos", rate=10,
		)
		zpl_with_price = build_label_zpl(row_with_price, settings, "Medium")
		self.assertIn(frappe.utils.fmt_money(10), zpl_with_price)

	def _ensure_leaf_tax_accounts(self, company, count):
		"""At least `count` non-group Tax accounts for `company`. A fresh
		CI site's default "Standard" Chart of Accounts (Company.chart_of_accounts
		defaults to "Standard" regardless of Country - see company.py
		validate_coa_input) creates exactly one Tax-type account, "Duties
		and Taxes", and it's a group account - so there's no leaf Tax
		account to use until one is created here."""
		existing = frappe.get_all(
			"Account",
			filters={"company": company, "is_group": 0, "account_type": "Tax"},
			fields=["name"],
			limit=count,
		)
		if len(existing) >= count:
			return [a.name for a in existing]

		parent = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Tax", "is_group": 1}, "name"
		) or frappe.db.get_value(
			"Account", {"company": company, "is_group": 1, "root_type": "Liability"}, "name"
		)

		for i in range(len(existing), count):
			acc = frappe.get_doc({
				"doctype": "Account",
				"account_name": f"Test Tax {i + 1} {frappe.generate_hash(length=4)}",
				"company": company,
				"parent_account": parent,
				"is_group": 0,
				"account_type": "Tax",
			}).insert(ignore_permissions=True)
			existing.append(frappe._dict(name=acc.name))
		return [a.name for a in existing]

	def _create_item_tax_template(self, company, total_rate):
		"""A minimal Item Tax Template for `company` whose detail rows sum
		to `total_rate` percent - split across two distinct Tax accounts
		(CGST/SGST-style) when more than one exists, otherwise one row -
		ERPNext rejects the same account appearing twice in one template."""
		accounts = self._ensure_leaf_tax_accounts(company, 2)
		if len(accounts) >= 2:
			taxes = [
				{"tax_type": accounts[0], "tax_rate": total_rate / 2},
				{"tax_type": accounts[1], "tax_rate": total_rate / 2},
			]
		else:
			taxes = [{"tax_type": accounts[0], "tax_rate": total_rate}]
		return frappe.get_doc({
			"doctype": "Item Tax Template",
			"title": f"Test Tax {total_rate}% {frappe.generate_hash(length=4)}",
			"company": company,
			"taxes": taxes,
			# india_compliance (if installed) validates this against the
			# default "Taxable" GST Treatment - harmless on a site without
			# it, since an unknown field is simply ignored.
			"gst_rate": total_rate,
		}).insert(ignore_permissions=True)

	def test_item_tax_rate_percent_sums_detail_rows(self):
		company = self._get_or_create_company()
		template = self._create_item_tax_template(company, 18)
		self.assertEqual(get_item_tax_rate_percent(template.name), 18)
		self.assertEqual(get_item_tax_rate_percent(""), 0)

	def test_get_item_rate_and_note_inclusive_vs_exclusive(self):
		company = self._get_or_create_company()
		template = self._create_item_tax_template(company, 10)
		item_code = self._create_test_item_with_barcode()
		price_list = self._get_or_create_price_list()
		# Item.after_insert() auto-creates an Item Price from standard_rate
		# for whichever Price List is the default Selling one at that
		# moment - if that's the same list resolved above, update it
		# in place instead of inserting a conflicting duplicate row.
		existing_price = frappe.db.get_value(
			"Item Price", {"item_code": item_code, "price_list": price_list}, "name"
		)
		if existing_price:
			frappe.db.set_value("Item Price", existing_price, "price_list_rate", 200)
		else:
			frappe.get_doc({
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": price_list,
				"price_list_rate": 200,
			}).insert(ignore_permissions=True)

		rate, note = get_item_rate_and_note(item_code, price_list, "")
		self.assertEqual(rate, 200)
		self.assertEqual(note, "Excl. of taxes")

		rate, note = get_item_rate_and_note(item_code, price_list, template.name)
		self.assertAlmostEqual(rate, 220)
		self.assertEqual(note, "Incl. of taxes")

	def test_build_label_zpl_never_prints_tax_percent_or_company(self):
		"""The label shows the final Rate and a short Incl./Excl. note -
		never the tax % itself or any Company/template name."""
		settings = get_barcode_print_settings()
		settings["display_rules"]["Medium"]["show_rate"] = 1

		row = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="ABC123", barcode_type="CODE-39", uom="Nos",
			rate=220, price_note="Incl. of taxes",
		)
		zpl = build_label_zpl(row, settings, "Medium")
		self.assertIn("220.00 (Incl. of taxes)", zpl)
		self.assertNotIn("10%", zpl)
		self.assertNotIn("Company", zpl)

	# ---- Feature: Print Price As Rate/MRP (barcodes_print.api.zpl) ----

	def test_format_price_line_rate_is_unchanged_from_before_this_feature(self):
		"""Default "Rate" setting must reproduce exactly what this app always
		printed - no prefix word - so nobody upgrading sees a changed label."""
		money = frappe.utils.fmt_money(220)
		self.assertEqual(_format_price_line(220, "Incl. of taxes", "Rate"), f"{money} (Incl. of taxes)")
		self.assertEqual(_format_price_line(220, "", "Rate"), money)

	def test_format_price_line_mrp_adds_prefix(self):
		money = frappe.utils.fmt_money(220)
		self.assertEqual(_format_price_line(220, "Incl. of taxes", "MRP"), f"MRP: {money} (Incl. of taxes)")
		self.assertEqual(_format_price_line(220, "Excl. of taxes", "MRP"), f"MRP: {money} (Excl. of taxes)")

	def test_build_label_zpl_prints_mrp_prefix_when_settings_say_so(self):
		settings = get_barcode_print_settings()
		settings["display_rules"]["Medium"]["show_rate"] = 1
		settings["price_label"] = "MRP"

		row = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="ABC123", barcode_type="CODE-39", uom="Nos",
			rate=220, price_note="Incl. of taxes",
		)
		zpl = build_label_zpl(row, settings, "Medium")
		self.assertIn("MRP:", zpl)
		self.assertIn("220.00 (Incl. of taxes)", zpl)

	def test_build_label_zpl_prints_mrp_even_without_a_tax_template(self):
		"""MRP wording is not gated on whether the row actually has a Tax
		Template - it follows the Settings choice regardless (approved
		decision: no forced validation)."""
		settings = get_barcode_print_settings()
		settings["display_rules"]["Medium"]["show_rate"] = 1
		settings["price_label"] = "MRP"

		row = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="ABC123", barcode_type="CODE-39", uom="Nos",
			rate=150, price_note="Excl. of taxes",
		)
		zpl = build_label_zpl(row, settings, "Medium")
		self.assertIn("MRP: " + frappe.utils.fmt_money(150) + " (Excl. of taxes)", zpl)

	def test_get_barcode_print_settings_defaults_price_label_to_rate(self):
		self.assertEqual(get_barcode_print_settings()["price_label"], "Rate")

	def test_get_purchase_document_items_fetches_company_and_tax_template_per_line(self):
		"""Company/Tax Template are auto-fetched straight from the Purchase
		Order's own document/line - never a user or site default."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.enable_purchase_document_printing = 1
		settings_doc.purchase_document_type = "Purchase Order"
		settings_doc.save(ignore_permissions=True)

		company = self._get_or_create_company()
		template = self._create_item_tax_template(company, 12)
		item_code = self._create_test_item_with_barcode()
		po = self._create_purchase_order([
			{"item_code": item_code, "qty": 2, "rate": 10, "item_tax_template": template.name},
		])

		try:
			prefill = get_purchase_document_items("Purchase Order", po.name)
			self.assertEqual(prefill["items"][0]["company"], company)
			self.assertEqual(prefill["items"][0]["tax_template"], template.name)
		finally:
			po.cancel()
			frappe.delete_doc("Purchase Order", po.name, force=True, ignore_permissions=True)
			settings_doc.enable_purchase_document_printing = 0
			settings_doc.purchase_document_type = ""
			settings_doc.save(ignore_permissions=True)

	def test_alignment_center_and_right_shift_content_without_overflow(self):
		"""Center/Right must actually move the barcode and text (not just
		accept the setting and ignore it), while never pushing content
		past the label's own printable width."""
		settings = get_barcode_print_settings()
		row = frappe._dict(
			idx=1, item="ITEM-X", item_name="Item X",
			barcode="ABC123", barcode_type="CODE-39", uom="Nos", rate=0,
		)

		settings["display_rules"]["Medium"]["alignment"] = "Left"
		left_zpl = build_label_zpl(row, settings, "Medium")

		settings["display_rules"]["Medium"]["alignment"] = "Center"
		center_zpl = build_label_zpl(row, settings, "Medium")

		settings["display_rules"]["Medium"]["alignment"] = "Right"
		right_zpl = build_label_zpl(row, settings, "Medium")

		self.assertNotEqual(left_zpl, center_zpl)
		self.assertNotEqual(center_zpl, right_zpl)

		width_dots, _height = get_label_dimensions_dots(settings, "Medium")
		for zpl in (left_zpl, center_zpl, right_zpl):
			for match in re.finditer(r"\^FO(\d+),", zpl):
				self.assertLess(int(match.group(1)), width_dots, "A field's x-origin fell outside the label width")

	def test_purchase_order_integration_enforces_max_extra_and_tracks_count(self):
		"""Full Purchase Order integration: pre-fill always defaults to the
		line's own quantity (never quantity + extra), a successful print
		bumps the line's counter, and only a deliberately-raised request
		beyond quantity + Max Extra Barcodes per Line is blocked."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.enable_purchase_document_printing = 1
		settings_doc.purchase_document_type = "Purchase Order"
		settings_doc.max_extra_barcodes_per_line = 2
		settings_doc.save(ignore_permissions=True)

		item_code = self._create_test_item_with_barcode()
		supplier = self._get_or_create_supplier()
		company = self._get_or_create_company()
		warehouse_row = frappe.get_all("Warehouse", filters={"company": company}, limit=1)
		warehouse = (warehouse_row or frappe.get_all("Warehouse", limit=1))[0].name

		po = frappe.get_doc({
			"doctype": "Purchase Order",
			"supplier": supplier,
			"company": company,
			"schedule_date": frappe.utils.nowdate(),
			"set_warehouse": warehouse,
			"tax_category": frappe.db.get_value("Tax Category", {}, "name"),
			"payment_terms_template": frappe.db.get_value("Payment Terms Template", {}, "name"),
			"tc_name": frappe.db.get_value("Terms and Conditions", {}, "name"),
			"items": [{
				"item_code": item_code, "qty": 5, "rate": 10,
				"schedule_date": frappe.utils.nowdate(), "warehouse": warehouse,
			}],
		}).insert(ignore_permissions=True)
		po.submit()

		try:
			# Defaults to the line's own quantity (5), not qty + max_extra (7).
			prefill = get_purchase_document_items("Purchase Order", po.name)
			self.assertEqual(len(prefill["items"]), 1)
			self.assertEqual(prefill["items"][0]["no_of_barcodes"], 5)
			self.assertEqual(prefill["items"][0]["max_allowed"], 7)  # ceiling, not the default
			row_name = prefill["items"][0]["source_row_name"]

			# User deliberately raises it to the full ceiling (a torn-label
			# style reprint need) - still allowed since it's within qty + extra.
			job = get_quick_print_job(
				[{"item": item_code, "no_of_barcodes": 7, "source_row_name": row_name}],
				"Medium",
				source_doctype="Purchase Order",
				source_name=po.name,
			)
			self.assertEqual(job["total_labels"], 7)
			log_print(
				job["resolved_items"], "Medium", status="Success",
				source_doctype="Purchase Order", source_name=po.name,
			)

			printed = frappe.db.get_value("Purchase Order Item", row_name, "custom_barcodes_printed")
			self.assertEqual(printed, 7)

			# Already at the max - even 1 more must be blocked.
			with self.assertRaises(frappe.ValidationError):
				get_quick_print_job(
					[{"item": item_code, "no_of_barcodes": 1, "source_row_name": row_name}],
					"Medium",
					source_doctype="Purchase Order",
					source_name=po.name,
				)
		finally:
			po.cancel()
			frappe.delete_doc("Purchase Order", po.name, force=True, ignore_permissions=True)
			settings_doc.enable_purchase_document_printing = 0
			settings_doc.purchase_document_type = ""
			settings_doc.max_extra_barcodes_per_line = 0
			settings_doc.save(ignore_permissions=True)

	def test_unlimited_reprints_removes_the_cap_but_default_stays_at_quantity(self):
		"""With No Printing Limit on, the same line can be printed past
		quantity + Max Extra any number of times - but the pre-filled
		default is still just the line's own quantity, never inflated."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.enable_purchase_document_printing = 1
		settings_doc.purchase_document_type = "Purchase Order"
		settings_doc.max_extra_barcodes_per_line = 0
		settings_doc.unlimited_reprints = 1
		settings_doc.save(ignore_permissions=True)

		item_code = self._create_test_item_with_barcode()
		po = self._create_purchase_order([{"item_code": item_code, "qty": 4, "rate": 10}])

		try:
			prefill = get_purchase_document_items("Purchase Order", po.name)
			self.assertEqual(prefill["items"][0]["no_of_barcodes"], 4)
			self.assertEqual(prefill["items"][0]["max_allowed"], 0)  # no cap to warn about
			row_name = prefill["items"][0]["source_row_name"]

			# Print well past qty(4) + no extra allowance - must not be blocked.
			job = get_quick_print_job(
				[{"item": item_code, "no_of_barcodes": 50, "source_row_name": row_name}],
				"Medium",
				source_doctype="Purchase Order",
				source_name=po.name,
			)
			self.assertEqual(job["total_labels"], 50)
			log_print(
				job["resolved_items"], "Medium", status="Success",
				source_doctype="Purchase Order", source_name=po.name,
			)

			# Reprinting the exact same document again afterwards is still
			# never blocked - the whole point of this setting.
			get_quick_print_job(
				[{"item": item_code, "no_of_barcodes": 4, "source_row_name": row_name}],
				"Medium",
				source_doctype="Purchase Order",
				source_name=po.name,
			)
		finally:
			po.cancel()
			frappe.delete_doc("Purchase Order", po.name, force=True, ignore_permissions=True)
			settings_doc.enable_purchase_document_printing = 0
			settings_doc.purchase_document_type = ""
			settings_doc.unlimited_reprints = 0
			settings_doc.save(ignore_permissions=True)

	def _create_service_item(self):
		item_code = f"TEST-ITEM-SERVICE-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Test Service Item",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"is_stock_item": 0,
			"stock_uom": "Nos",
		}).insert(ignore_permissions=True)
		return item_code

	def _create_purchase_order(self, item_rows):
		supplier = self._get_or_create_supplier()
		company = self._get_or_create_company()
		warehouse_row = frappe.get_all("Warehouse", filters={"company": company}, limit=1)
		warehouse = (warehouse_row or frappe.get_all("Warehouse", limit=1))[0].name

		for row in item_rows:
			row.setdefault("schedule_date", frappe.utils.nowdate())
			if frappe.get_cached_value("Item", row["item_code"], "is_stock_item"):
				row.setdefault("warehouse", warehouse)

		po = frappe.get_doc({
			"doctype": "Purchase Order",
			"supplier": supplier,
			"company": company,
			"schedule_date": frappe.utils.nowdate(),
			"set_warehouse": warehouse,
			"tax_category": frappe.db.get_value("Tax Category", {}, "name"),
			"payment_terms_template": frappe.db.get_value("Payment Terms Template", {}, "name"),
			"tc_name": frappe.db.get_value("Terms and Conditions", {}, "name"),
			"items": item_rows,
		}).insert(ignore_permissions=True)
		po.submit()
		return po

	def _create_purchase_invoice(self, item_rows, update_stock=0):
		supplier = self._get_or_create_supplier()
		company = self._get_or_create_company()
		if update_stock:
			# A Purchase Invoice only ever carries real batch/serial data
			# (legacy fields or a Serial and Batch Bundle) when it's also
			# updating stock - a plain billing-only invoice doesn't touch
			# them at all, so every row needs its own warehouse here.
			warehouse = frappe.get_all("Warehouse", filters={"company": company}, limit=1)[0].name
			for row in item_rows:
				row.setdefault("warehouse", warehouse)
		pi = frappe.get_doc({
			"doctype": "Purchase Invoice",
			"supplier": supplier,
			"company": company,
			"update_stock": update_stock,
			"items": item_rows,
		}).insert(ignore_permissions=True)
		pi.submit()
		return pi

	def _create_purchase_receipt_item_row(self, item_row):
		"""A single-row Purchase Receipt, left as a draft - this is only
		ever used to test _get_batch_serial_map directly, which reads
		Purchase Receipt Item rows regardless of docstatus, so submitting
		(and the stock/GL postings that would trigger) is never needed
		here. Returns the row's own name."""
		supplier = self._get_or_create_supplier()
		company = self._get_or_create_company()
		warehouse = frappe.get_all("Warehouse", filters={"company": company}, limit=1)[0].name
		item_row.setdefault("warehouse", warehouse)
		item_row.setdefault("qty", 1)
		item_row.setdefault("rate", 10)

		pr = frappe.get_doc({
			"doctype": "Purchase Receipt",
			"supplier": supplier,
			"company": company,
			"items": [item_row],
		}).insert(ignore_permissions=True)
		return pr.items[0].name

	def test_purchase_receipt_batch_serial_map_legacy_fields(self):
		"""use_serial_batch_fields=1: batch_no sits directly on the row."""
		item_code, batch_no = self._create_batch_tracked_item()
		row_name = self._create_purchase_receipt_item_row({
			"item_code": item_code,
			"use_serial_batch_fields": 1,
			"batch_no": batch_no,
		})

		result = _get_batch_serial_map("Purchase Receipt Item", [row_name])
		self.assertEqual(result[row_name], {"batch_no": batch_no, "serial_no": ""})

	def test_purchase_receipt_batch_serial_map_bundle_mode(self):
		"""use_serial_batch_fields=0 (the default): the row only links to a
		Serial and Batch Bundle - its entries carry the actual serials."""
		item_code = f"TEST-ITEM-PR-SERIAL-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Test PR Serial Item",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"has_serial_no": 1,
		}).insert(ignore_permissions=True)

		company = self._get_or_create_company()
		warehouse = frappe.get_all("Warehouse", filters={"company": company}, limit=1)[0].name
		serials = []
		for _ in range(2):
			serial = frappe.get_doc({
				"doctype": "Serial No",
				"serial_no": f"TEST-PR-SN-{frappe.generate_hash(length=8)}",
				"item_code": item_code,
				"company": company,
				"status": "Inactive",
			}).insert(ignore_permissions=True)
			serials.append(serial.name)

		bundle = frappe.get_doc({
			"doctype": "Serial and Batch Bundle",
			"item_code": item_code,
			"warehouse": warehouse,
			"company": company,
			"voucher_type": "Purchase Receipt",
			"type_of_transaction": "Inward",
			"has_serial_no": 1,
			"entries": [{"serial_no": s, "qty": 1} for s in serials],
		}).insert(ignore_permissions=True)

		row_name = self._create_purchase_receipt_item_row({
			"item_code": item_code,
			"qty": 2,
			"use_serial_batch_fields": 0,
			"serial_and_batch_bundle": bundle.name,
		})

		result = _get_batch_serial_map("Purchase Receipt Item", [row_name])
		self.assertEqual(result[row_name]["batch_no"], "")
		self.assertEqual(set(result[row_name]["serial_no"].splitlines()), set(serials))

	def test_get_purchase_document_items_excludes_service_items(self):
		"""Service/non-stock item rows never get a physical barcode label -
		only the stock item row should come back from the pre-fill."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.enable_purchase_document_printing = 1
		settings_doc.purchase_document_type = "Purchase Order"
		settings_doc.save(ignore_permissions=True)

		stock_item = self._create_test_item_with_barcode()
		service_item = self._create_service_item()
		po = self._create_purchase_order([
			{"item_code": stock_item, "qty": 3, "rate": 10},
			{"item_code": service_item, "qty": 1, "rate": 500},
		])

		try:
			prefill = get_purchase_document_items("Purchase Order", po.name)
			self.assertEqual(len(prefill["items"]), 1)
			self.assertEqual(prefill["items"][0]["item"], stock_item)
		finally:
			po.cancel()
			frappe.delete_doc("Purchase Order", po.name, force=True, ignore_permissions=True)
			settings_doc.enable_purchase_document_printing = 0
			settings_doc.purchase_document_type = ""
			settings_doc.save(ignore_permissions=True)

	def test_get_purchase_document_items_blocks_subcontracted_purchase_order(self):
		"""A subcontracted Purchase Order's own items are what's being
		bought from the subcontractor, not what's being received - the
		button should never work here, only on its Subcontracting Order."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.enable_purchase_document_printing = 1
		settings_doc.purchase_document_type = "Purchase Order"
		settings_doc.save(ignore_permissions=True)

		item_code = self._create_test_item_with_barcode()
		po = self._create_purchase_order([{"item_code": item_code, "qty": 2, "rate": 10}])
		# Bypasses ERPNext's own subcontracting document validation (BOM,
		# service items, etc.) - only the specific flag this code path
		# actually checks needs to be true for this test.
		frappe.db.set_value("Purchase Order", po.name, "is_subcontracted", 1)

		try:
			with self.assertRaises(frappe.ValidationError):
				get_purchase_document_items("Purchase Order", po.name)
		finally:
			frappe.db.set_value("Purchase Order", po.name, "is_subcontracted", 0)
			po.reload()
			po.cancel()
			frappe.delete_doc("Purchase Order", po.name, force=True, ignore_permissions=True)
			settings_doc.enable_purchase_document_printing = 0
			settings_doc.purchase_document_type = ""
			settings_doc.save(ignore_permissions=True)

	def test_get_purchase_document_items_for_purchase_invoice(self):
		"""Purchase Invoice mirrors Purchase Order/Receipt's own pattern:
		service items excluded, Company/Tax Template fetched per line from
		the invoice itself, and - since an invoice line can carry a real
		batch/serial the same way a Receipt line can - batch_no/serial_no
		resolved via the same shared _get_batch_serial_map."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.enable_purchase_document_printing = 1
		settings_doc.purchase_document_type = "Purchase Invoice"
		settings_doc.save(ignore_permissions=True)

		item_code, batch_no = self._create_batch_tracked_item()
		service_item = self._create_service_item()
		company = self._get_or_create_company()
		template = self._create_item_tax_template(company, 5)

		pi = self._create_purchase_invoice([
			{
				"item_code": item_code, "qty": 3, "rate": 10,
				"use_serial_batch_fields": 1, "batch_no": batch_no,
				"item_tax_template": template.name,
			},
			{"item_code": service_item, "qty": 1, "rate": 5},
		])

		try:
			prefill = get_purchase_document_items("Purchase Invoice", pi.name)
			self.assertEqual(len(prefill["items"]), 1)  # service line excluded
			row = prefill["items"][0]
			self.assertEqual(row["item"], item_code)
			self.assertEqual(row["company"], company)
			self.assertEqual(row["tax_template"], template.name)
			self.assertEqual(row["batch_no"], batch_no)
		finally:
			pi.cancel()
			frappe.delete_doc("Purchase Invoice", pi.name, force=True, ignore_permissions=True)
			settings_doc.enable_purchase_document_printing = 0
			settings_doc.purchase_document_type = ""
			settings_doc.save(ignore_permissions=True)

	def test_get_purchase_document_items_blocks_return_purchase_invoice(self):
		"""A Return (credit note) Purchase Invoice is explicitly out of
		scope - printing from one is blocked, matching how printing from a
		subcontracted Purchase Order is blocked."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.enable_purchase_document_printing = 1
		settings_doc.purchase_document_type = "Purchase Invoice"
		settings_doc.save(ignore_permissions=True)

		item_code = self._create_test_item_with_barcode()
		pi = self._create_purchase_invoice([{"item_code": item_code, "qty": 2, "rate": 10}])
		# Bypasses ERPNext's own Return-document validation (a real return
		# needs a linked original invoice, negative qty, etc.) - only the
		# specific flag this code path actually checks needs to be true.
		frappe.db.set_value("Purchase Invoice", pi.name, "is_return", 1)

		try:
			with self.assertRaises(frappe.ValidationError):
				get_purchase_document_items("Purchase Invoice", pi.name)
		finally:
			frappe.db.set_value("Purchase Invoice", pi.name, "is_return", 0)
			pi.reload()
			pi.cancel()
			frappe.delete_doc("Purchase Invoice", pi.name, force=True, ignore_permissions=True)
			settings_doc.enable_purchase_document_printing = 0
			settings_doc.purchase_document_type = ""
			settings_doc.save(ignore_permissions=True)

	def test_is_purchase_print_enabled_for_purchase_invoice(self):
		"""Purchase Invoice behaves as a normal alternative choice alongside
		Purchase Order/Purchase Receipt - on only when Settings points at it,
		not unconditional like Subcontracting Order."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.enable_purchase_document_printing = 1
		settings_doc.purchase_document_type = "Purchase Invoice"
		settings_doc.save(ignore_permissions=True)

		try:
			self.assertTrue(is_purchase_print_enabled_for("Purchase Invoice"))
			self.assertFalse(is_purchase_print_enabled_for("Purchase Order"))
			self.assertFalse(is_purchase_print_enabled_for("Purchase Receipt"))
			self.assertTrue(is_purchase_print_enabled_for("Subcontracting Order"))
		finally:
			settings_doc.enable_purchase_document_printing = 0
			settings_doc.purchase_document_type = ""
			settings_doc.save(ignore_permissions=True)

	def test_purchase_invoice_batch_serial_map_bundle_mode(self):
		"""Purchase Invoice Item shares the identical batch/serial recording
		fields with Purchase Receipt Item, so the same _get_batch_serial_map
		resolves a Serial and Batch Bundle on an invoice line too."""
		item_code = f"TEST-ITEM-PI-SERIAL-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Test PI Serial Item",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"has_serial_no": 1,
		}).insert(ignore_permissions=True)

		company = self._get_or_create_company()
		warehouse = frappe.get_all("Warehouse", filters={"company": company}, limit=1)[0].name
		serial = frappe.get_doc({
			"doctype": "Serial No",
			"serial_no": f"TEST-PI-SN-{frappe.generate_hash(length=8)}",
			"item_code": item_code,
			"company": company,
			"status": "Inactive",
		}).insert(ignore_permissions=True)

		bundle = frappe.get_doc({
			"doctype": "Serial and Batch Bundle",
			"item_code": item_code,
			"warehouse": warehouse,
			"company": company,
			"voucher_type": "Purchase Invoice",
			"type_of_transaction": "Inward",
			"has_serial_no": 1,
			"entries": [{"serial_no": serial.name, "qty": 1}],
		}).insert(ignore_permissions=True)

		pi = self._create_purchase_invoice([{
			"item_code": item_code, "qty": 1, "rate": 10,
			"use_serial_batch_fields": 0, "serial_and_batch_bundle": bundle.name,
		}], update_stock=1)
		try:
			row_name = pi.items[0].name
			result = _get_batch_serial_map("Purchase Invoice Item", [row_name])
			self.assertEqual(result[row_name]["serial_no"], serial.name)
		finally:
			pi.cancel()
			frappe.delete_doc("Purchase Invoice", pi.name, force=True, ignore_permissions=True)

	def test_is_purchase_print_enabled_for_subcontracting_order_is_unconditional(self):
		"""Subcontracting Order isn't an alternative choice alongside
		Purchase Order/Purchase Receipt in the settings - it's always on
		once purchase-document printing is enabled at all."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.enable_purchase_document_printing = 1
		settings_doc.purchase_document_type = "Purchase Receipt"
		settings_doc.save(ignore_permissions=True)

		try:
			self.assertTrue(is_purchase_print_enabled_for("Subcontracting Order"))
			self.assertFalse(is_purchase_print_enabled_for("Purchase Order"))
			self.assertTrue(is_purchase_print_enabled_for("Purchase Receipt"))
		finally:
			settings_doc.enable_purchase_document_printing = 0
			settings_doc.purchase_document_type = ""
			settings_doc.save(ignore_permissions=True)

	def test_mm_to_dots_conversion(self):
		# 25.4mm = 1 inch, so at 203 DPI that's exactly 203 dots.
		self.assertEqual(mm_to_dots(25.4, 203), 203)
		self.assertEqual(mm_to_dots(25.4, 300), 300)

	def _create_test_item_with_barcode(self):
		item_code = f"TEST-ITEM-BARCODE-{frappe.generate_hash(length=6)}"
		barcode_val = _random_ean13()
		doc = frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Test Item with Barcode",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"standard_rate": 150.0,
			"barcodes": [{"barcode": barcode_val, "barcode_type": "EAN-13"}]
		}).insert(ignore_permissions=True)
		return doc.name

	def _create_batch_tracked_item(self):
		item_code = f"TEST-ITEM-BATCH-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Test Batch-Tracked Item",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"has_batch_no": 1,
			"create_new_batch": 1,
		}).insert(ignore_permissions=True)
		batch = frappe.get_doc({
			"doctype": "Batch",
			"batch_id": f"TEST-BATCH-{frappe.generate_hash(length=6)}",
			"item": item_code,
		}).insert(ignore_permissions=True)
		return item_code, batch.name

	def _create_serial_tracked_item(self, batch_no=None):
		item_code = f"TEST-ITEM-SERIAL-{frappe.generate_hash(length=6)}"
		frappe.get_doc({
			"doctype": "Item",
			"item_code": item_code,
			"item_name": "Test Serial-Tracked Item",
			"item_group": "All Item Groups",
			"is_sales_item": 0,
			"stock_uom": "Nos",
			"has_serial_no": 1,
		}).insert(ignore_permissions=True)
		serials = []
		for _ in range(2):
			serial = frappe.get_doc({
				"doctype": "Serial No",
				"serial_no": f"TEST-SN-{frappe.generate_hash(length=8)}",
				"item_code": item_code,
				"batch_no": batch_no,
				"company": self._get_or_create_company(),
				"status": "Active",
			}).insert(ignore_permissions=True)
			serials.append(serial.name)
		return item_code, serials

	def test_resolve_row_encodes_batch_no_as_barcode(self):
		"""Printing for a specific Batch No encodes that Batch No as the
		label's own barcode (Code 128) instead of the item's generic
		barcode - scanning it anywhere in ERPNext resolves the Item via
		erpnext.stock.utils.scan_barcode's own Batch lookup, no core
		changes needed."""
		item_code, batch_no = self._create_batch_tracked_item()
		row = resolve_row(item_code, no_of_barcodes=5, batch_no=batch_no)
		self.assertEqual(row.barcode, batch_no)
		self.assertEqual(row.barcode_type, "Code 128")
		self.assertEqual(row.batch_no, batch_no)
		self.assertEqual(row.no_of_barcodes, 5)  # batches can print multiple copies

	def test_resolve_row_rejects_batch_no_for_wrong_item(self):
		_item_code, batch_no = self._create_batch_tracked_item()
		other_item = self._create_test_item_with_barcode()
		with self.assertRaises(frappe.ValidationError):
			resolve_row(other_item, no_of_barcodes=1, batch_no=batch_no)

	def test_resolve_row_encodes_serial_no_and_inherits_its_batch(self):
		"""One Serial No always means exactly one physical unit - no_of_barcodes
		is forced to 1 regardless of what was requested, and the serial's own
		Batch No is carried onto the row even when not separately supplied."""
		_batch_item, batch_no = self._create_batch_tracked_item()
		serial_item_code, serials = self._create_serial_tracked_item(batch_no=batch_no)
		row = resolve_row(serial_item_code, no_of_barcodes=10, serial_no=serials[0])
		self.assertEqual(row.barcode, serials[0])
		self.assertEqual(row.barcode_type, "Code 128")
		self.assertEqual(row.serial_no, serials[0])
		self.assertEqual(row.batch_no, batch_no)  # inherited from the serial itself
		self.assertEqual(row.no_of_barcodes, 1)

	def test_resolve_row_rejects_serial_no_for_wrong_item(self):
		_item_code, serials = self._create_serial_tracked_item()
		other_item = self._create_test_item_with_barcode()
		with self.assertRaises(frappe.ValidationError):
			resolve_row(other_item, no_of_barcodes=1, serial_no=serials[0])

	def test_build_label_zpl_prints_batch_and_serial_lines(self):
		settings_doc = frappe.get_single("Barcode Print Settings")
		settings_doc.batch_serial_print_mode = "Both"
		settings_doc.save(ignore_permissions=True)
		settings = get_barcode_print_settings()

		item_code, batch_no = self._create_batch_tracked_item()
		row = resolve_row(item_code, no_of_barcodes=1, batch_no=batch_no)
		zpl = build_label_zpl(row, settings, "Medium")
		self.assertIn(f"Batch: {batch_no}", zpl)

		serial_item_code, serials = self._create_serial_tracked_item(batch_no=batch_no)
		row2 = resolve_row(serial_item_code, no_of_barcodes=1, serial_no=serials[0])
		zpl2 = build_label_zpl(row2, settings, "Medium")
		self.assertIn(f"Serial: {serials[0]}", zpl2)
		self.assertIn(f"Batch: {batch_no}", zpl2)

	def test_batch_serial_print_mode_gates_encoding_globally(self):
		"""Batch/Serial Printing (global, site-wide) controls whether
		printing BY Batch/Serial is available at all - not just whether its
		text line is shown. With Serial off, resolve_row must ignore a
		serial_no it's given and fall back to the Item's own barcode,
		exactly as if neither had been passed."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		item_code, batch_no = self._create_batch_tracked_item()
		serial_item_code, serials = self._create_serial_tracked_item(batch_no=batch_no)

		try:
			settings_doc.batch_serial_print_mode = "Batch Only"
			settings_doc.save(ignore_permissions=True)

			# Batch is still allowed - encodes normally.
			row = resolve_row(item_code, no_of_barcodes=1, batch_no=batch_no)
			self.assertEqual(row.barcode, batch_no)

			# Serial is off globally - a serial_no passed anyway is ignored,
			# not honoured, so this falls through to "no barcode available"
			# for an item with none registered (same as passing nothing).
			with self.assertRaises(frappe.ValidationError):
				resolve_row(serial_item_code, no_of_barcodes=1, serial_no=serials[0])

			settings_doc.batch_serial_print_mode = "Neither"
			settings_doc.save(ignore_permissions=True)
			with self.assertRaises(frappe.ValidationError):
				resolve_row(item_code, no_of_barcodes=1, batch_no=batch_no)
		finally:
			settings_doc.batch_serial_print_mode = "Both"
			settings_doc.save(ignore_permissions=True)

	def test_show_batch_serial_no_is_per_size_display_only(self):
		"""Each label size's own Show Batch No/Show Serial No checkbox
		(Display Rules) controls only whether the resolved text line shows
		on that size's label - independent of the global Batch/Serial
		Printing setting, which stays "Both" (encoding always allowed)."""
		settings_doc = frappe.get_single("Barcode Print Settings")
		item_code, batch_no = self._create_batch_tracked_item()

		try:
			self._set_display_rule(settings_doc, "Medium", show_batch_no=0)
			self._set_display_rule(settings_doc, "Large", show_batch_no=1)
			settings = get_barcode_print_settings()

			row = resolve_row(item_code, no_of_barcodes=1, batch_no=batch_no)
			zpl_medium = build_label_zpl(row, settings, "Medium")
			self.assertNotIn("Batch:", zpl_medium)

			zpl_large = build_label_zpl(row, settings, "Large")
			self.assertIn(f"Batch: {batch_no}", zpl_large)
		finally:
			self.setUp()

	def test_get_quick_print_job_expands_one_row_per_serial_no(self):
		"""Selecting multiple Serial Nos for one grid row must produce one
		distinct label per serial - never repeats of the same barcode."""
		item_code, serials = self._create_serial_tracked_item()
		items = [{
			"item": item_code,
			"no_of_barcodes": 1,
			"serial_nos": "\n".join(serials),
		}]
		job = get_quick_print_job(items=items, size="Medium")
		self.assertEqual(job["total_labels"], len(serials))
		self.assertEqual(len(job["resolved_items"]), len(serials))
		self.assertEqual({r["barcode"] for r in job["resolved_items"]}, set(serials))

	# ---- Feature: Scan-to-fill + Stock Qty (barcodes_print.api.item) ----

	def test_get_item_stock_qty_none_for_non_stock_item(self):
		"""Service items don't track stock at all - None, not 0, so the
		page can tell "zero in stock" apart from "doesn't track stock"."""
		service_item = self._create_service_item()
		self.assertIsNone(get_item_stock_qty(service_item)["stock_qty"])

	def test_get_item_stock_qty_overall_for_stock_item(self):
		"""A real stock Item with no stock ledger entries yet is still 0.0
		(a known, tracked quantity), never None."""
		item_code = self._create_test_item_with_barcode()
		self.assertEqual(get_item_stock_qty(item_code)["stock_qty"], 0.0)

	def test_get_item_stock_qty_works_for_a_plain_system_manager(self):
		"""Regression test for a real bug caught only by live browser
		testing, never by a test run as Administrator (who bypasses every
		permission check by design): erpnext.stock.utils.get_latest_stock_qty()
		enforces its own explicit "Item: read" permission, which a System
		Manager role does not automatically carry for every doctype - this
		app's own convention (get_item_barcode_details and friends) reads
		Item data directly instead, trusting the page-level permission gate,
		and get_item_stock_qty must do the same rather than going through
		that core function."""
		item_code = self._create_test_item_with_barcode()
		user_email = f"test-stock-qty-{frappe.generate_hash(length=6)}@example.com"
		frappe.get_doc({
			"doctype": "User", "email": user_email, "first_name": "Stock Qty Test",
			"send_welcome_email": 0, "roles": [{"role": "System Manager"}],
		}).insert(ignore_permissions=True)
		original_user = frappe.session.user
		try:
			frappe.set_user(user_email)
			result = get_item_stock_qty(item_code)
			self.assertEqual(result["stock_qty"], 0.0)
		finally:
			frappe.set_user(original_user)
			frappe.delete_doc("User", user_email, force=True, ignore_permissions=True)

	def test_get_item_stock_qty_scoped_to_batch(self):
		"""A row with a Batch No picked gets that batch's own quantity, not
		the item's overall figure."""
		item_code, batch_no = self._create_batch_tracked_item()
		frappe.db.set_value("Batch", batch_no, "batch_qty", 7)
		self.assertEqual(get_item_stock_qty(item_code, batch_no)["stock_qty"], 7.0)
		self.assertEqual(get_item_stock_qty(item_code)["stock_qty"], 0.0)

	def test_resolve_scan_by_registered_barcode(self):
		item_code = self._create_test_item_with_barcode()
		barcode = frappe.db.get_value("Item Barcode", {"parent": item_code}, "barcode")
		data = resolve_scan(barcode)
		self.assertEqual(data["item"], item_code)
		self.assertEqual(data["barcode"], barcode)

	def test_resolve_scan_falls_back_to_item_code(self):
		"""A value that isn't a registered barcode/Serial No/Batch No, but
		is a real Item Code, still resolves - the scan box also accepts a
		typed Item Code directly."""
		item_code = self._create_test_item_with_barcode()
		data = resolve_scan(item_code)
		self.assertEqual(data["item"], item_code)

	def test_resolve_scan_by_serial_no(self):
		item_code, serials = self._create_serial_tracked_item()
		data = resolve_scan(serials[0])
		self.assertEqual(data["item"], item_code)
		self.assertEqual(data["serial_no"], serials[0])

	def test_resolve_scan_blocks_disabled_item(self):
		item_code = self._create_test_item_with_barcode()
		frappe.db.set_value("Item", item_code, "disabled", 1)
		with self.assertRaises(frappe.ValidationError):
			resolve_scan(item_code)

	def test_resolve_scan_unknown_value_throws(self):
		with self.assertRaises(frappe.ValidationError):
			resolve_scan("NO-SUCH-BARCODE-OR-ITEM-XYZ")

	# ---- Feature: Load from Excel/CSV (barcodes_print.api.import_items) ----

	def _create_csv_file(self, rows: list) -> str:
		"""A real "File" doctype record holding CSV content - exercises the
		actual frappe.get_doc("File", {"file_url": ...}).get_content() path
		import_items_from_file uses, not a mocked file read."""
		content = "\n".join(",".join(str(c) for c in row) for row in rows)
		file_doc = frappe.get_doc({
			"doctype": "File",
			"file_name": f"test-import-{frappe.generate_hash(length=6)}.csv",
			"content": content.encode("utf-8"),
			"is_private": 1,
		}).insert(ignore_permissions=True)
		return file_doc.file_url

	def test_batch_belongs_to_item_and_serial_lookup_helpers(self):
		"""The shared helpers resolve_row and the CSV importer both rely on
		for batch/serial ownership - single source of truth for this check."""
		item_code, batch_no = self._create_batch_tracked_item()
		other_item_code = self._create_test_item_with_barcode()
		self.assertTrue(batch_belongs_to_item(batch_no, item_code))
		self.assertFalse(batch_belongs_to_item(batch_no, other_item_code))
		self.assertFalse(batch_belongs_to_item("NO-SUCH-BATCH", item_code))

		serial_item_code, serials = self._create_serial_tracked_item()
		found_item, found_batch = serial_no_item_and_batch(serials[0])
		self.assertEqual(found_item, serial_item_code)
		self.assertEqual(found_batch, "")
		missing_item, missing_batch = serial_no_item_and_batch("NO-SUCH-SERIAL")
		self.assertIsNone(missing_item)
		self.assertIsNone(missing_batch)

	def test_import_items_from_file_valid_rows(self):
		item_a = self._create_test_item_with_barcode()
		item_b = self._create_test_item_with_barcode()
		file_url = self._create_csv_file([
			["item_code", "qty"],
			[item_a, 3],
			[item_b, 1],
		])
		result = import_items_from_file(file_url)
		self.assertEqual(result["errors"], [])
		self.assertEqual(len(result["rows"]), 2)
		self.assertEqual(result["rows"][0]["item"], item_a)
		self.assertEqual(result["rows"][0]["no_of_barcodes"], 3)
		self.assertEqual(result["rows"][1]["item"], item_b)

	def test_import_items_from_file_accepts_header_synonyms(self):
		"""sku/quantity/copies are all accepted as synonyms for
		item_code/no_of_barcodes, matching the flexible header mapping."""
		item_code = self._create_test_item_with_barcode()
		file_url = self._create_csv_file([
			["SKU", "Quantity"],
			[item_code, 7],
		])
		result = import_items_from_file(file_url)
		self.assertEqual(result["errors"], [])
		self.assertEqual(result["rows"][0]["item"], item_code)
		self.assertEqual(result["rows"][0]["no_of_barcodes"], 7)

	def test_import_items_from_file_reports_bad_rows_without_failing_the_file(self):
		"""A row with an unknown Item Code is skipped and reported - the
		other, valid row in the same file still comes through."""
		item_code = self._create_test_item_with_barcode()
		file_url = self._create_csv_file([
			["item_code", "qty"],
			[item_code, 2],
			["NO-SUCH-ITEM-CODE", 1],
		])
		result = import_items_from_file(file_url)
		self.assertEqual(len(result["rows"]), 1)
		self.assertEqual(result["rows"][0]["item"], item_code)
		self.assertEqual(len(result["errors"]), 1)
		self.assertIn("NO-SUCH-ITEM-CODE", result["errors"][0])

	def test_import_items_from_file_validates_batch_and_serial_ownership(self):
		item_code, batch_no = self._create_batch_tracked_item()
		other_item_code, other_batch_no = self._create_batch_tracked_item()
		serial_item_code, serials = self._create_serial_tracked_item()

		file_url = self._create_csv_file([
			["item_code", "qty", "batch_no", "serial_nos"],
			[item_code, 1, batch_no, ""],  # valid batch for this item
			[item_code, 1, other_batch_no, ""],  # batch belongs to a different item
			[serial_item_code, 1, "", serials[0]],  # valid serial for this item
			[item_code, 1, "", serials[0]],  # serial belongs to a different item
		])
		result = import_items_from_file(file_url)
		self.assertEqual(len(result["rows"]), 2)
		self.assertEqual(result["rows"][0]["batch_no"], batch_no)
		self.assertEqual(result["rows"][1]["serial_nos"], serials[0])
		self.assertEqual(len(result["errors"]), 2)
		self.assertTrue(any(other_batch_no in e for e in result["errors"]))
		self.assertTrue(any(serials[0] in e and "belongs to Item" in e for e in result["errors"]))

	def test_import_items_from_file_rejects_disabled_items_and_bad_quantities(self):
		item_code = self._create_test_item_with_barcode()
		frappe.db.set_value("Item", item_code, "disabled", 1)
		zero_qty_item = self._create_test_item_with_barcode()

		file_url = self._create_csv_file([
			["item_code", "qty"],
			[item_code, 1],
			[zero_qty_item, 0],
		])
		result = import_items_from_file(file_url)
		self.assertEqual(result["rows"], [])
		self.assertEqual(len(result["errors"]), 2)

	def test_import_items_from_file_rejects_file_over_row_limit(self):
		import barcodes_print.api.import_items as import_items_module

		item_code = self._create_test_item_with_barcode()
		original_max = import_items_module.MAX_IMPORT_ROWS
		import_items_module.MAX_IMPORT_ROWS = 2
		try:
			file_url = self._create_csv_file([
				["item_code", "qty"],
				[item_code, 1],
				[item_code, 1],
				[item_code, 1],
			])
			with self.assertRaises(frappe.ValidationError):
				import_items_from_file(file_url)
		finally:
			import_items_module.MAX_IMPORT_ROWS = original_max

	def test_import_items_from_file_blank_rows_are_skipped_silently(self):
		"""A fully blank spreadsheet row (common trailing-rows artifact from
		spreadsheet software) is skipped without being reported as an error,
		unlike a row with real but invalid data."""
		item_code = self._create_test_item_with_barcode()
		file_url = self._create_csv_file([
			["item_code", "qty"],
			[item_code, 1],
			["", ""],
		])
		result = import_items_from_file(file_url)
		self.assertEqual(len(result["rows"]), 1)
		self.assertEqual(result["errors"], [])

	def test_import_items_from_file_skips_downloaded_template_preamble(self):
		"""The Download Template button writes an instructional preamble
		(title, warnings, a "------" line) before the real header row,
		matching Frappe's own native grid bulk-edit download - re-uploading
		that exact file must find the real header by content, not assume
		row 1 is it."""
		item_code = self._create_test_item_with_barcode()
		file_url = self._create_csv_file([
			["Barcode Print - Bulk Item Template"],
			[],
			["The CSV format is case sensitive"],
			["Do not edit the header row below"],
			["------"],
			["item_code", "qty"],
			[item_code, 4],
		])
		result = import_items_from_file(file_url)
		self.assertEqual(result["errors"], [])
		self.assertEqual(len(result["rows"]), 1)
		self.assertEqual(result["rows"][0]["item"], item_code)
		self.assertEqual(result["rows"][0]["no_of_barcodes"], 4)
