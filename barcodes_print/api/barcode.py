# Copyright (c) 2026, Auriga IT and contributors
# For license information, please see license.txt

import re

import frappe
from frappe.utils.data import cint

BARCODE_TYPE_MAP = {
	"EAN-13": "ean13",
	"EAN13": "ean13",
	"EAN": "ean13",
	"EAN-8": "ean8",
	"EAN8": "ean8",
	"EAN-14": "ean14",
	"EAN14": "ean14",
	"UPC-A": "upca",
	"UPCA": "upca",
	"UPC": "upca",
	"UPC-E": "upce",
	"UPCE": "upce",
	"CODE-39": "code39",
	"CODE39": "code39",
	"CODE 39": "code39",
	"CODE-128": "code128",
	"CODE128": "code128",
	"CODE 128": "code128",
	"GS1-128": "code128",
	"GS1_128": "code128",
	"GS1 128": "code128",
	"GS1": "code128",
	"GTIN": "ean14",
	"GTIN-14": "ean14",
	"GTIN14": "ean14",
	"ITF": "itf",
	"ITF-14": "itf14",
	"ITF14": "itf14",
	"INTERLEAVED 2 OF 5": "itf",
	"INTERLEAVED 2OF5": "itf",
	"I25": "itf",
	"CODABAR": "codabar",
	"NW-7": "nw-7",
	"NW7": "nw-7",
	"ISBN-13": "isbn13",
	"ISBN13": "isbn13",
	"ISBN-10": "isbn10",
	"ISBN10": "isbn10",
	"ISBN": "isbn",
	"ISSN": "issn",
	"JAN": "jan",
	"PZN": "pzn",
	"QR CODE": "qrcode",
	"QR-CODE": "qrcode",
	"QRCODE": "qrcode",
	"QR": "qrcode",
	"DATA MATRIX": "datamatrix",
	"DATA-MATRIX": "datamatrix",
	"DATAMATRIX": "datamatrix",
	"CUSTOM": "code128",
}

# Symbologies rendered as 2D matrix codes rather than 1D linear barcodes.
MATRIX_FORMATS = {"qrcode", "datamatrix"}

# Business rule 5.3: auto-detect fallback tries formats in this exact order.
FALLBACK_FORMATS = ["ean13", "ean8", "upca", "code39", "code128"]

# Standard payload digit-count (min, max) for fixed-length numeric
# symbologies. UPC-E and ITF-14 have their own explicit checks below since
# they also need special value handling (expansion / zero-padding).
FIXED_LENGTH_DIGITS = {
	"ean13": (12, 13),
	"ean8": (7, 8),
	"ean14": (13, 14),
	"upca": (11, 12),
	"isbn13": (12, 13),
	"isbn10": (9, 10),
	"issn": (7, 8),
	"jan": (12, 13),
	"pzn": (6, 7),
}


_DEFAULT_DISPLAY_RULE = {
	"alignment": "Left",
	"show_item_name": 1,
	"show_item_code": 1,
	"show_barcode_number": 1,
	"show_barcode_type": 1,
	"show_uom": 1,
	"show_rate": 0,
}


@frappe.whitelist()
def get_print_barcode_page_defaults() -> dict:
	"""Small, read-permission-light subset of Barcode Print Settings the
	Print Barcode page's own JS needs up front (e.g. to default each new
	row's Price List) - the full settings dict is read-restricted to
	System Manager, but anyone allowed to use this page needs this much."""
	frappe.has_permission("Barcode Print Log", "read", throw=True)
	return {"default_price_list": frappe.db.get_single_value("Barcode Print Settings", "default_price_list") or ""}


def get_barcode_print_settings() -> dict:
	settings = frappe.get_single("Barcode Print Settings")

	display_rules = {size: dict(_DEFAULT_DISPLAY_RULE) for size in ("Small", "Medium", "Large")}
	for row in settings.get("display_rules") or []:
		if row.label_size not in display_rules:
			continue
		display_rules[row.label_size] = {
			"alignment": row.alignment or "Left",
			"show_item_name": cint(row.show_item_name),
			"show_item_code": cint(row.show_item_code),
			"show_barcode_number": cint(row.show_barcode_number),
			"show_barcode_type": cint(row.show_barcode_type),
			"show_uom": cint(row.show_uom),
			"show_rate": cint(row.show_rate),
		}

	return {
		"display_rules": display_rules,
		"print_connector": settings.get("print_connector") or "QZ Tray",
		"default_label_printer": settings.get("default_label_printer") or "",
		"printer_dpi": cint(settings.get("printer_dpi") or 203),
		"small_width_mm": settings.get("small_width_mm") or 50,
		"small_height_mm": settings.get("small_height_mm") or 25,
		"medium_width_mm": settings.get("medium_width_mm") or 75,
		"medium_height_mm": settings.get("medium_height_mm") or 50,
		"large_width_mm": settings.get("large_width_mm") or 100,
		"large_height_mm": settings.get("large_height_mm") or 75,
		"enable_purchase_document_printing": cint(settings.get("enable_purchase_document_printing")),
		"purchase_document_type": settings.get("purchase_document_type") or "",
		"max_extra_barcodes_per_line": cint(settings.get("max_extra_barcodes_per_line") or 0),
		"default_price_list": settings.get("default_price_list") or "",
	}


def get_item_barcode_type(item_code: str, barcode: str = "") -> str:
	"""Fetch barcode_type dynamically from Item Master -> Barcodes table."""
	if not item_code:
		return ""

	try:
		if barcode:
			# Look for exact matching barcode in child table
			btype = frappe.db.get_value(
				"Item Barcode",
				{"parent": item_code, "barcode": str(barcode).strip()},
				"barcode_type",
			)
			if btype:
				return btype

		# Fallback to first barcode entry for item
		btype = frappe.db.get_value(
			"Item Barcode",
			{"parent": item_code},
			"barcode_type",
		)
		if btype:
			return btype

		# Check if item itself has legacy barcode_type
		if frappe.db.has_column("Item", "barcode_type"):
			btype = frappe.db.get_value("Item", item_code, "barcode_type")
			if btype:
				return btype
	except Exception:
		pass

	return ""


def get_display_barcode_type(value: str = "", barcode_type: str = "", item_code: str = "") -> str:
	"""Return human-readable barcode type for display (e.g. UPC-A, EAN-13, Code 128, ITF-14, etc.)."""
	if barcode_type:
		raw_upper = str(barcode_type).strip().upper()
		norm = re.sub(r"[\s\-_]+", "", raw_upper)
		canonical = {
			"EAN13": "EAN-13",
			"EAN8": "EAN-8",
			"EAN14": "EAN-14",
			"EAN": "EAN-13",
			"UPCA": "UPC-A",
			"UPC": "UPC-A",
			"UPCE": "UPC-E",
			"CODE128": "Code 128",
			"CODE39": "Code 39",
			"ITF": "ITF",
			"ITF14": "ITF-14",
			"GTIN14": "GTIN-14",
			"GTIN": "GTIN",
			"GS1128": "GS1-128",
			"GS1": "GS1",
			"ISBN13": "ISBN-13",
			"ISBN10": "ISBN-10",
			"ISBN": "ISBN",
			"ISSN": "ISSN",
			"JAN": "JAN",
			"PZN": "PZN",
			"CODABAR": "Codabar",
			"NW7": "NW-7",
			"QRCODE": "QR Code",
			"QR": "QR Code",
			"DATAMATRIX": "Data Matrix",
			"CUSTOM": "Custom",
		}
		if norm in canonical:
			return canonical[norm]
		return str(barcode_type).strip()

	if item_code:
		dynamic_type = get_item_barcode_type(item_code, value)
		if dynamic_type:
			return get_display_barcode_type(value, dynamic_type)

	if value:
		try:
			btype = frappe.db.get_value("Item Barcode", {"barcode": str(value).strip()}, "barcode_type")
			if btype:
				return get_display_barcode_type(value, btype)
		except Exception:
			pass

	fmt = _resolve_barcode_format(value, barcode_type, item_code)
	display_names = {
		"ean13": "EAN-13",
		"ean8": "EAN-8",
		"ean14": "EAN-14",
		"upca": "UPC-A",
		"upce": "UPC-E",
		"code128": "Code 128",
		"code39": "Code 39",
		"itf": "ITF",
		"itf14": "ITF-14",
		"isbn13": "ISBN-13",
		"isbn10": "ISBN-10",
		"issn": "ISSN",
		"jan": "JAN",
		"pzn": "PZN",
		"codabar": "Codabar",
		"qrcode": "QR Code",
		"datamatrix": "Data Matrix",
	}
	return display_names.get(fmt, (fmt or "").upper())


def validate_barcode_compatibility(value: str, barcode_type: str = "") -> tuple[bool, str, str]:
	"""
	Validates whether the barcode value is compatible with the selected barcode type.
	Returns (is_valid: bool, resolved_format: str, error_message: str).
	"""
	if not value or not str(value).strip():
		return False, "", "Barcode value cannot be blank"

	val_str = str(value).strip()
	resolved_format = _resolve_barcode_format(val_str, barcode_type)

	if not resolved_format:
		return False, "", f"Unsupported or unknown barcode type '{barcode_type}'"

	import barcode
	from barcode.errors import BarcodeError, BarcodeNotFoundError, IllegalCharacterError, NumberOfDigitsError

	if resolved_format in MATRIX_FORMATS:
		# QR Code / Data Matrix are 2D matrix symbols rendered natively by the
		# printer itself (ZPL ^BQ / ^BX) - any non-empty text/data payload is valid.
		return True, resolved_format, ""

	if resolved_format in FIXED_LENGTH_DIGITS:
		# python-barcode's classes reject too-few digits but silently accept
		# (and truncate) too-many - e.g. a 12-digit value handed to its EAN-8
		# class is quietly cut down to 7 digits internally instead of being
		# rejected, which would print/scan as the wrong barcode entirely. The
		# exact digit count is enforced here instead of relying on that.
		clean_val = re.sub(r"\D", "", val_str)
		min_len, max_len = FIXED_LENGTH_DIGITS[resolved_format]
		if len(clean_val) < min_len or len(clean_val) > max_len:
			expected = str(min_len) if min_len == max_len else f"{min_len} or {max_len}"
			return (
				False,
				resolved_format,
				f"{resolved_format.upper()} requires {expected} digits, got {len(clean_val)}",
			)

	# ITF-14 is a fixed-length (14 digit) variant of ITF. python-barcode has no
	# dedicated "itf14" symbology, so validate the length here and render via
	# the real "itf" class (zero-padded) in _generate_svg.
	if resolved_format == "itf14":
		clean_val = re.sub(r"\D", "", val_str)
		if len(clean_val) > 14:
			return False, resolved_format, f"ITF-14 requires at most 14 digits, got {len(clean_val)}"
		lookup_format = "itf"
	elif resolved_format == "upce":
		# UPC-E has no dedicated python-barcode class; it is expanded to its
		# full 12-digit UPC-A equivalent and rendered via the "upca" class.
		clean_val = re.sub(r"\D", "", val_str)
		if len(clean_val) not in (6, 7, 8):
			return False, resolved_format, f"UPC-E requires 6, 7, or 8 digits, got {len(clean_val)}"
		expanded = _expand_upce_to_upca(clean_val)
		if not expanded:
			return False, resolved_format, "Invalid UPC-E value: could not expand to UPC-A"
		lookup_format = "upca"
		val_str = expanded
	else:
		lookup_format = resolved_format

	if lookup_format not in barcode.PROVIDED_BARCODES:
		return False, "", f"Symbology '{resolved_format}' is not provided by the generator"

	try:
		check_val = val_str
		if resolved_format == "code39":
			check_val = check_val.upper()
		elif resolved_format == "itf14":
			check_val = re.sub(r"\D", "", check_val).zfill(14)
		cls = barcode.get_barcode_class(lookup_format)
		cls(check_val)
		return True, resolved_format, ""
	except NumberOfDigitsError as err:
		return False, resolved_format, f"Incorrect number of digits: {err}"
	except IllegalCharacterError as err:
		return False, resolved_format, f"Illegal characters: {err}"
	except BarcodeNotFoundError as err:
		return False, resolved_format, f"Barcode format '{resolved_format}' not found: {err}"
	except (BarcodeError, ValueError, TypeError) as err:
		return False, resolved_format, str(err)
	except Exception as err:
		return False, resolved_format, str(err)


def _resolve_barcode_format(value: str, barcode_type: str = "", item_code: str = "") -> str:
	if not barcode_type and item_code:
		barcode_type = get_item_barcode_type(item_code, value)

	if not barcode_type and value:
		try:
			btype = frappe.db.get_value("Item Barcode", {"barcode": str(value).strip()}, "barcode_type")
			if btype:
				barcode_type = btype
		except Exception:
			pass

	if barcode_type:
		raw_type = str(barcode_type).strip().upper()
		norm_type = re.sub(r"[\s\-_]+", "", raw_type)

		# Special handling for generic EAN (auto-detect EAN-8 vs EAN-13 vs EAN-14 based on length)
		if raw_type == "EAN" or norm_type == "EAN":
			clean_val = re.sub(r"\D", "", str(value))
			if len(clean_val) in (7, 8):
				return "ean8"
			if len(clean_val) == 14:
				return "ean14"
			return "ean13"

		# Special handling for generic ISBN (auto-detect ISBN-10 vs ISBN-13)
		if raw_type == "ISBN" or norm_type == "ISBN":
			clean_val = re.sub(r"[^\dX]", "", str(value).upper())
			if len(clean_val) == 10:
				return "isbn10"
			return "isbn13"

		# Special handling for generic UPC (UPC-A)
		if raw_type == "UPC" or norm_type == "UPC":
			return "upca"

		if raw_type in BARCODE_TYPE_MAP:
			return BARCODE_TYPE_MAP[raw_type]
		if norm_type in BARCODE_TYPE_MAP:
			return BARCODE_TYPE_MAP[norm_type]

		fmt_lower = str(barcode_type).strip().lower()
		import barcode
		if fmt_lower in barcode.PROVIDED_BARCODES:
			return fmt_lower

	# Auto-detect format from value if no barcode_type specified
	clean_digits = re.sub(r"\D", "", str(value))
	if clean_digits == str(value).strip():
		val_len = len(clean_digits)
		if val_len in (12, 13) and _can_encode(value, "ean13"):
			return "ean13"
		if val_len in (7, 8) and _can_encode(value, "ean8"):
			return "ean8"
		if val_len in (11, 12) and _can_encode(value, "upca"):
			return "upca"
		if val_len == 14 and _can_encode(value, "ean14"):
			return "ean14"
		# NOTE: no length-based auto-detect for ITF here (business rule 5.3
		# only lists EAN-13 -> EAN-8 -> UPC -> Code 39 -> Code 128 as the
		# auto-detect priority order). ITF (Interleaved 2 of 5) is a niche
		# industrial symbology most generic/phone-camera scanners don't
		# support at all, unlike the formats below - so a short internal
		# code with no declared type should fall through to Code 39 or
		# Code 128 instead, which are universally scannable.

	# Fallback formats for alphanumeric / general values
	for fallback in FALLBACK_FORMATS:
		if _can_encode(value, fallback):
			return fallback

	return "code128"


def _can_encode(value: str, barcode_format: str) -> bool:
	import barcode
	from barcode.errors import BarcodeError

	if barcode_format in MATRIX_FORMATS:
		return bool(str(value).strip())

	lookup_format = "itf" if barcode_format == "itf14" else barcode_format
	val_str = str(value).strip()

	if barcode_format == "upce":
		clean_val = re.sub(r"\D", "", val_str)
		expanded = _expand_upce_to_upca(clean_val)
		if not expanded:
			return False
		val_str = expanded
		lookup_format = "upca"

	if lookup_format not in barcode.PROVIDED_BARCODES:
		return False

	try:
		if barcode_format == "code39":
			val_str = val_str.upper()
		elif barcode_format == "itf14":
			val_str = re.sub(r"\D", "", val_str).zfill(14)
		barcode.get_barcode_class(lookup_format)(val_str)
		return True
	except (BarcodeError, ValueError, TypeError, Exception):
		return False


def _expand_upce_to_upca(digits: str) -> str:
	"""Expand a 6, 7, or 8-digit UPC-E (zero-suppressed) code into its full
	12-digit UPC-A equivalent, following the standard UPC-E expansion rules.
	Returns "" if the input is not a valid UPC-E payload.
	"""
	digits = re.sub(r"\D", "", digits or "")

	# Strip an optional leading number-system digit (0 or 1) and/or a
	# trailing check digit so we are left with the 6 core UPC-E digits.
	if len(digits) == 8:
		digits = digits[1:7]
	elif len(digits) == 7:
		digits = digits[:6]
	elif len(digits) != 6:
		return ""

	if not digits.isdigit():
		return ""

	last = digits[5]
	manufacturer, product = "", ""

	if last in ("0", "1", "2"):
		manufacturer = digits[0:2] + last
		product = "0000" + digits[2:5]
	elif last == "3":
		manufacturer = digits[0:3]
		product = "00000" + digits[3:5]
	elif last == "4":
		manufacturer = digits[0:4]
		product = "00000" + digits[4:5]
	else:
		manufacturer = digits[0:5]
		product = "0000" + last

	upc_a_body = "0" + manufacturer + product
	if len(upc_a_body) != 11:
		return ""

	odd_sum = sum(int(upc_a_body[i]) for i in range(0, 11, 2))
	even_sum = sum(int(upc_a_body[i]) for i in range(1, 11, 2))
	check_digit = (10 - ((odd_sum * 3 + even_sum) % 10)) % 10
	return upc_a_body + str(check_digit)

