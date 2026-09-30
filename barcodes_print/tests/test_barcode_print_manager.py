# Copyright (c) 2026, Rahul Chaudhary and contributors
# See license.txt

import random
import re
import frappe
from frappe.tests.utils import FrappeTestCase

from barcodes_print.api.barcode import get_barcode_print_settings, validate_barcode_compatibility
from barcodes_print.api.item import get_item_barcode_details
from barcodes_print.api.zpl import (
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
			})
		settings.print_connector = "QZ Tray"
		settings.printer_dpi = "203"
		settings.small_width_mm = 50
		settings.small_height_mm = 25
		settings.medium_width_mm = 75
		settings.medium_height_mm = 50
		settings.large_width_mm = 100
		settings.large_height_mm = 75
		settings.save(ignore_permissions=True)

	def _set_display_rule(self, settings_doc, size, **overrides):
		"""Test helper: update one size's Display Rule row in place and save."""
		for row in settings_doc.display_rules:
			if row.label_size == size:
				row.update(overrides)
				break
		settings_doc.save(ignore_permissions=True)

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
		supplier = frappe.get_all("Supplier", limit=1)[0].name
		company = frappe.get_all("Company", limit=1)[0].name
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
		supplier = frappe.get_all("Supplier", limit=1)[0].name
		company = frappe.get_all("Company", limit=1)[0].name
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
				"company": "_Test Company" if frappe.db.exists("Company", "_Test Company") else None,
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
		self._set_display_rule(settings_doc, "Medium", show_batch_no=1, show_serial_no=1)
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
