# Copyright (c) 2026, Yash and contributors
# For license information, please see license.txt

"""
Builds raw ZPL (Zebra Programming Language) label content for the
Print Barcode page. No image/SVG rendering is used anywhere in the print
path - the printer itself renders the barcode from these text commands,
which is what makes ZPL sharp at full printer resolution and orders of
magnitude smaller than an image-based label.

This is a one-time-use utility, not a business document: nothing is saved
to the database except a lightweight Barcode Print Log entry per item
after a successful print, for audit purposes.

The generated ZPL is handed to the browser, which sends it unmodified to
either QZ Tray or Zebra Browser Print (whichever is configured in
Barcode Print Settings) - see public/js/qz_tray_print.js and
public/js/zebra_browser_print.js.
"""

import re

import frappe
from frappe import _

from barcode_print_manager.api.barcode import (
	MATRIX_FORMATS,
	get_barcode_print_settings,
	get_display_barcode_type,
	validate_barcode_compatibility,
)
from barcode_print_manager.api.item import get_item_barcode_details

LABEL_SIZES = ("Small", "Medium", "Large")

# ISO/best-practice QR/Data Matrix scanning thresholds: 10mm is the absolute
# floor for a standard smartphone/handheld scanner to read reliably at all;
# below that, printing one is a real scan-failure risk, not a preference,
# so it's blocked rather than merely discouraged.
MIN_MATRIX_SCAN_HEIGHT_MM = 10.0

LABEL_SIZE_SETTINGS_FIELDS = {
	"Small": ("small_width_mm", "small_height_mm"),
	"Medium": ("medium_width_mm", "medium_height_mm"),
	"Large": ("large_width_mm", "large_height_mm"),
}


def mm_to_dots(value_mm: float, dpi: int) -> int:
	return max(1, round((value_mm or 0) * dpi / 25.4))


def get_label_dimensions_dots(settings: dict, size: str) -> tuple[int, int]:
	width_field, height_field = LABEL_SIZE_SETTINGS_FIELDS[size]
	dpi = settings["printer_dpi"]
	return mm_to_dots(settings[width_field], dpi), mm_to_dots(settings[height_field], dpi)


def _zpl_escape(value: str) -> str:
	"""Escape characters that are special to ZPL field data (^FD...^FS)."""
	value = str(value or "")
	return value.replace("\\", "\\5C").replace("^", "\\5E").replace("~", "\\7E")


def _barcode_field_zpl(resolved_fmt: str, value: str, height_dots: int) -> str:
	"""
	Return the ZPL barcode field command block (^B.. + ^FD..^FS) for one
	barcode, for the resolved python-barcode-style format name used
	elsewhere in this app (see api/barcode.py _resolve_barcode_format).
	"""
	value = str(value).strip()
	h = max(10, height_dots)

	# Every barcode field below has its own built-in "interpretation line"
	# (human-readable text under the bars) turned OFF ("N"). Whether the
	# number/type shows at all is controlled solely by the Barcode Print
	# Settings toggles, rendered as separate text fields in build_label_zpl
	# - having both on at once double-prints the number and overlaps it.
	if resolved_fmt == "ean13":
		return f"^BEN,{h},N,N\n^FD{_zpl_escape(value)}^FS"
	if resolved_fmt == "ean8":
		return f"^B8N,{h},N,N\n^FD{_zpl_escape(value)}^FS"
	if resolved_fmt in ("ean14", "itf14"):
		# No dedicated EAN/ITF-14 ZPL command: render as Interleaved 2 of 5,
		# zero-padded to 14 digits, matching the previous SVG-era convention.
		clean_val = re.sub(r"\D", "", value).zfill(14)
		return f"^B2N,{h},N,N,N\n^FD{_zpl_escape(clean_val)}^FS"
	if resolved_fmt == "upca":
		return f"^BUN,{h},N,N,N\n^FD{_zpl_escape(value)}^FS"
	if resolved_fmt == "upce":
		return f"^B9N,{h},N,N,N\n^FD{_zpl_escape(value)}^FS"
	if resolved_fmt == "code39":
		return f"^B3N,N,{h},N,N\n^FD{_zpl_escape(value.upper())}^FS"
	if resolved_fmt == "itf":
		return f"^B2N,{h},N,N,N\n^FD{_zpl_escape(value)}^FS"
	if resolved_fmt == "codabar":
		return f"^BKN,N,{h},N,N,A,A\n^FD{_zpl_escape(value)}^FS"
	if resolved_fmt == "qrcode":
		magnification = max(1, min(10, round(h / 30)))
		return f"^BQN,2,{magnification}\n^FDQA,{_zpl_escape(value)}^FS"
	if resolved_fmt == "datamatrix":
		return f"^BXN,{max(1, round(h / 20))},200,0,0\n^FD{_zpl_escape(value)}^FS"

	# isbn13/isbn10/isbn/issn/jan/pzn and anything else without a clean
	# dedicated symbology: Code 128 safely encodes any text/alphanumeric
	# payload and is broadly scannable, so it's the universal fallback
	# (matches api.barcode.FALLBACK_FORMATS' final entry).
	return f"^BCN,{h},N,N,N,N\n^FD{_zpl_escape(value)}^FS"


# Rough element-count-per-character estimates for each symbology, used only
# to position a barcode for Center/Right alignment (ZPL has no native
# "justify this barcode" command the way ^FB does for text). These are
# textbook figures (EAN-13/UPC-A's 95 modules, EAN-8's 67, etc.) good enough
# to visually align correctly; exact rendered width can vary by a module or
# two depending on the printer, which is why the offset is clamped to never
# push a label past its own boundaries.
_FIXED_BARCODE_MODULES = {"ean13": 95, "upca": 95, "ean8": 67, "upce": 51}
_PER_CHAR_MODULES = {"code39": 16, "codabar": 15, "code128": 11}


def _estimate_barcode_width_dots(resolved_fmt: str, value: str, height_dots: int, module_width_dots: int) -> int:
	if resolved_fmt in MATRIX_FORMATS:
		return height_dots  # QR/Data Matrix are square-ish.

	n = len(str(value).strip())
	if resolved_fmt in _FIXED_BARCODE_MODULES:
		modules = _FIXED_BARCODE_MODULES[resolved_fmt]
	elif resolved_fmt in ("ean14", "itf14", "itf"):
		modules = max(1, (n + 1) // 2) * 15 + 6
	elif resolved_fmt in _PER_CHAR_MODULES:
		modules = (n + 2) * _PER_CHAR_MODULES[resolved_fmt]
	else:
		modules = (n + 3) * 11  # Code 128-style fallback.

	return modules * module_width_dots


def _aligned_x(alignment: str, margin: int, available_width: int, content_width: int) -> int:
	"""Left/Center/Right x-origin for one field, clamped so it never lands
	left of the margin or pushes the content past the label's right edge."""
	if alignment == "Center":
		x = margin + max(0, (available_width - content_width) // 2)
	elif alignment == "Right":
		x = margin + max(0, available_width - content_width)
	else:
		x = margin
	return max(margin, min(x, margin + max(0, available_width - content_width)))


def build_label_zpl(row, settings: dict, size: str) -> str:
	"""Build the ZPL for exactly one physical label (^XA ... ^XZ)."""
	width_dots, height_dots = get_label_dimensions_dots(settings, size)
	rules = settings["display_rules"][size]
	alignment = rules.get("alignment") or "Left"

	is_valid, resolved_fmt, err_msg = validate_barcode_compatibility(row.barcode, row.barcode_type)
	if not is_valid:
		frappe.throw(
			_("Row #{0}: Barcode value '{1}' is invalid for Barcode Type '{2}'. {3}").format(
				row.idx, row.barcode, row.barcode_type, err_msg
			)
		)

	# Text lines to print, in order, computed up front so the barcode gets
	# whatever vertical room is left over - this is what keeps the label
	# from overflowing its own physical height when several display
	# toggles are on at once (fixed proportions of label height overflow
	# as soon as more than 2-3 toggles are enabled together).
	text_lines_before = []
	if rules["show_item_name"] and row.item_name:
		text_lines_before.append(row.item_name)
	if rules["show_item_code"] and row.item:
		text_lines_before.append(row.item)

	text_lines_after = []
	if rules["show_barcode_number"]:
		text_lines_after.append(row.barcode)
	if rules["show_barcode_type"]:
		text_lines_after.append(row.barcode_type)
	if rules["show_uom"] and row.uom:
		text_lines_after.append(f"UOM: {row.uom}")
	if rules["show_rate"] and row.rate:
		text_lines_after.append(frappe.utils.fmt_money(row.rate))

	n_lines = len(text_lines_before) + len(text_lines_after)

	# Text height AND barcode bar width both scale with the chosen label
	# size - using a fixed module width/margin regardless of size was the
	# bug: a Medium/Large label ended up with the exact same small,
	# narrow-barred content as Small, just floating in a corner with the
	# rest of the label left empty instead of actually filling it.
	text_height_mm = {"Small": 2.5, "Medium": 3.3, "Large": 4.2}[size]
	module_width_mm = {"Small": 0.25, "Medium": 0.4, "Large": 0.5}[size]
	text_h = mm_to_dots(text_height_mm, settings["printer_dpi"])
	module_width_dots = mm_to_dots(module_width_mm, settings["printer_dpi"])
	line_gap = text_h + mm_to_dots(0.6, settings["printer_dpi"])
	margin = mm_to_dots(1.5, settings["printer_dpi"])
	barcode_gap = mm_to_dots(1.5, settings["printer_dpi"])
	min_barcode_height = mm_to_dots(6.0, settings["printer_dpi"])

	barcode_height_dots = max(
		min_barcode_height,
		height_dots - (2 * margin) - (n_lines * line_gap) - barcode_gap,
	)

	if resolved_fmt in MATRIX_FORMATS:
		barcode_height_mm = barcode_height_dots * 25.4 / settings["printer_dpi"]
		if barcode_height_mm < MIN_MATRIX_SCAN_HEIGHT_MM:
			frappe.throw(
				_(
					"Row #{0}: {1} needs at least {2}mm to scan reliably, but only {3:.1f}mm is available on "
					"a {4} label with the current display toggles. Pick a larger label size, turn off some "
					"display toggles to leave more room, or switch this row to a linear barcode type "
					"(EAN/UPC/Code 39/Code 128)."
				).format(row.idx, get_display_barcode_type("", resolved_fmt), MIN_MATRIX_SCAN_HEIGHT_MM, barcode_height_mm, size)
			)

	lines = ["^XA", f"^PW{width_dots}", f"^LL{height_dots}", "^LH0,0", "^CI28"]
	available_width = width_dots - (2 * margin)
	fb_justify = {"Left": "L", "Center": "C", "Right": "R"}.get(alignment, "L")

	y = margin
	for text in text_lines_before:
		# ^FB (Field Block) gives text fields native justification within
		# a width - no barcode does, so the barcode below is positioned
		# manually further down using an estimated rendered width instead.
		lines.append(f"^FO{margin},{y}^FB{available_width},1,0,{fb_justify}^A0N,{text_h},{text_h}^FD{_zpl_escape(text)}^FS")
		y += line_gap

	barcode_width_dots = _estimate_barcode_width_dots(resolved_fmt, row.barcode, barcode_height_dots, module_width_dots)
	barcode_x = _aligned_x(alignment, margin, available_width, barcode_width_dots)
	barcode_zpl = _barcode_field_zpl(resolved_fmt, row.barcode, barcode_height_dots)
	lines.append(f"^FO{barcode_x},{y}^BY{module_width_dots},3,{barcode_height_dots}")
	lines.append(barcode_zpl)
	y += barcode_height_dots + barcode_gap

	for text in text_lines_after:
		lines.append(f"^FO{margin},{y}^FB{available_width},1,0,{fb_justify}^A0N,{text_h},{text_h}^FD{_zpl_escape(text)}^FS")
		y += line_gap

	lines.append("^XZ")
	return "\n".join(lines)


def resolve_row(
	item_code: str, barcode: str = "", barcode_type: str = "", no_of_barcodes=1, idx=1, source_row_name: str = ""
) -> "frappe._dict":
	"""Fetch and validate everything needed to print one item row, mirroring
	business rules 5.1/5.2: auto-fetch Item Name/Barcode/Barcode Type/UOM
	from the Item master, block on a missing Barcode, and auto-detect the
	Barcode Type when it's left blank (business rule 5.3)."""
	if not item_code:
		frappe.throw(_("Item is required."))

	no_of_barcodes = frappe.utils.cint(no_of_barcodes) or 1
	if no_of_barcodes < 1:
		frappe.throw(_("No. of Barcodes must be at least 1."))

	details = get_item_barcode_details(item_code, barcode or "")
	row = frappe._dict(
		idx=idx,
		item=item_code,
		item_name=details.get("item_name") or "",
		uom=details.get("uom") or "",
		rate=details.get("rate") or 0.0,
		barcode=barcode or details.get("barcode") or "",
		barcode_type=barcode_type or details.get("barcode_type") or "",
		barcode_uom=details.get("barcode_uom") or "",
		no_of_barcodes=no_of_barcodes,
		source_row_name=source_row_name or "",
	)

	if not (row.barcode or "").strip():
		available = details.get("barcodes") or []
		if len(available) > 1:
			# resolve_row/get_item_barcode_details deliberately leave the
			# barcode blank rather than guessing when there's more than
			# one to choose from - this is that guard actually landing,
			# not a "no barcode at all" situation.
			frappe.throw(
				_("Item {0} has {1} barcodes. Please select which one to print.").format(item_code, len(available))
			)
		frappe.throw(_("Item {0} does not have a barcode. Please add barcode in Item Master.").format(item_code))

	is_valid, resolved_fmt, err_msg = validate_barcode_compatibility(row.barcode, row.barcode_type)
	if not is_valid:
		disp_type = get_display_barcode_type(row.barcode, row.barcode_type, row.item)
		frappe.throw(
			_("Row #{0}: Barcode value '{1}' is invalid for Barcode Type '{2}'. {3}").format(
				row.idx, row.barcode, disp_type or row.barcode_type, err_msg
			)
		)

	if not (row.barcode_type or "").strip():
		row.barcode_type = get_display_barcode_type(row.barcode, resolved_fmt, row.item)

	return row


def _parse_items(items) -> list:
	if isinstance(items, str):
		items = frappe.parse_json(items)
	if not items:
		frappe.throw(_("Please add at least one item row."))
	return items


# Maps a Purchase Document Type (Barcode Print Settings) to its items child
# doctype - both get the "Barcodes Printed" counter field (see
# install.after_install.ensure_purchase_barcode_tracking_fields), so
# switching this setting later doesn't require adding fields again.
PURCHASE_ITEM_DOCTYPES = {
	"Purchase Order": "Purchase Order Item",
	"Purchase Receipt": "Purchase Receipt Item",
}


def _get_purchase_line_allowance(source_doctype: str, row_name: str, max_extra: int) -> tuple[int, int]:
	"""Returns (already_printed, max_allowed) for one Purchase Order/Purchase
	Receipt Item line, read fresh from the database - this is the
	enforcement boundary, so client-sent counts are never trusted here."""
	child_doctype = PURCHASE_ITEM_DOCTYPES.get(source_doctype)
	if not child_doctype:
		frappe.throw(_("Unsupported source document type '{0}'.").format(source_doctype))

	row = frappe.db.get_value(child_doctype, row_name, ["qty", "custom_barcodes_printed"], as_dict=True)
	if not row:
		frappe.throw(_("Source line {0} was not found.").format(row_name))

	already_printed = frappe.utils.cint(row.custom_barcodes_printed)
	max_allowed = frappe.utils.cint(row.qty) + frappe.utils.cint(max_extra)
	return already_printed, max_allowed


@frappe.whitelist()
def is_purchase_print_enabled_for(doctype: str) -> bool:
	"""Lightweight, permission-light check used by the Print Barcode button
	on Purchase Order/Purchase Receipt forms (public/js/purchase_barcode_button.js).
	Uses a raw single-value DB read rather than get_barcode_print_settings()
	deliberately: Barcode Print Settings itself is read-restricted to
	System Manager, but anyone who can already view the Purchase
	Order/Purchase Receipt should be able to see whether this button
	applies, without needing separate access to Settings."""
	enabled = frappe.db.get_single_value("Barcode Print Settings", "enable_purchase_document_printing")
	configured_doctype = frappe.db.get_single_value("Barcode Print Settings", "purchase_document_type")
	return bool(enabled) and configured_doctype == doctype


@frappe.whitelist()
def get_purchase_document_items(source_doctype: str, source_name: str) -> dict:
	"""Pre-fill data for the Print Barcode page when opened via the Print
	Barcode button on a submitted Purchase Order/Purchase Receipt. Each
	row's starting 'No. of Barcodes' defaults to whatever is still allowed
	(qty + Max Extra Barcodes per Line, minus what's already been printed),
	not always 1 - matching how GRN-triggered label printing works
	elsewhere (quantities default from what's actually on the document)."""
	settings = get_barcode_print_settings()
	if not settings["enable_purchase_document_printing"]:
		frappe.throw(_("Printing from a Purchase Document is not enabled in Barcode Print Settings."))
	if settings["purchase_document_type"] != source_doctype:
		frappe.throw(
			_("Barcode Print Settings is currently configured for {0}, not {1}.").format(
				settings["purchase_document_type"] or _("no document"), source_doctype
			)
		)

	frappe.has_permission(source_doctype, "read", throw=True)
	doc = frappe.get_doc(source_doctype, source_name)
	if doc.docstatus != 1:
		frappe.throw(_("{0} must be submitted before printing barcodes.").format(source_doctype))

	rows = []
	for row in doc.items:
		already_printed = frappe.utils.cint(row.get("custom_barcodes_printed"))
		max_allowed = frappe.utils.cint(row.qty) + settings["max_extra_barcodes_per_line"]
		remaining = max(0, max_allowed - already_printed)
		rows.append({
			"item": row.item_code,
			"no_of_barcodes": remaining,
			"source_row_name": row.name,
			"max_allowed": remaining,
		})

	return {"source_doctype": source_doctype, "source_name": source_name, "items": rows}


@frappe.whitelist()
def get_quick_print_job(items, size: str, source_doctype: str = "", source_name: str = "") -> dict:
	"""Server-side step of the print flow (plan step 6.6/3): resolve and
	validate every row, then build the full ZPL print job. The browser
	sends this, unmodified, straight to QZ Tray or Zebra Browser Print -
	no PDF, no print preview, no browser print dialog, and nothing is
	saved as a document - this is a one-time-use print action."""
	if size not in LABEL_SIZES:
		frappe.throw(_("Invalid label size '{0}'.").format(size))

	if source_doctype and source_name:
		# Printing from a Purchase Order/Purchase Receipt's own button:
		# anyone who can already view that document can print its
		# barcodes - no separate role restriction, regardless of whether
		# they happen to also be a Stock Manager/User.
		frappe.has_permission(source_doctype, "read", doc=source_name, throw=True)
	else:
		# Free-form printing from the standalone Print Barcode page.
		# Nobody creates Barcode Print Log rows directly (log_print always
		# writes with ignore_permissions) - read access is used here as
		# the gate for "is this user allowed to use barcode printing at
		# all" (System Manager / Stock Manager / Stock User all have it).
		frappe.has_permission("Barcode Print Log", "read", throw=True)

	items = _parse_items(items)
	settings = get_barcode_print_settings()

	blocks = []
	resolved_items = []
	total_labels = 0
	for idx, item_row in enumerate(items, start=1):
		source_row_name = item_row.get("source_row_name") or ""
		row = resolve_row(
			item_row.get("item"),
			item_row.get("barcode", ""),
			item_row.get("barcode_type", ""),
			item_row.get("no_of_barcodes", 1),
			idx,
			source_row_name,
		)

		if source_doctype and source_row_name:
			already_printed, max_allowed = _get_purchase_line_allowance(
				source_doctype, source_row_name, settings["max_extra_barcodes_per_line"]
			)
			remaining = max_allowed - already_printed
			if row.no_of_barcodes > remaining:
				frappe.throw(
					_(
						"Row #{0}: only {1} more label(s) may be printed for this line "
						"(limit is quantity + {2} extra, {3} already printed)."
					).format(row.idx, max(0, remaining), settings["max_extra_barcodes_per_line"], already_printed)
				)

		label_zpl = build_label_zpl(row, settings, size)
		blocks.extend([label_zpl] * row.no_of_barcodes)
		total_labels += row.no_of_barcodes
		resolved_items.append(dict(row))

	width_mm_field, height_mm_field = LABEL_SIZE_SETTINGS_FIELDS[size]

	return {
		"zpl": "\n".join(blocks),
		"total_labels": total_labels,
		"connector": settings["print_connector"],
		"printer": settings["default_label_printer"],
		"label_width_mm": settings[width_mm_field],
		"label_height_mm": settings[height_mm_field],
		"resolved_items": resolved_items,
		"source_doctype": source_doctype,
		"source_name": source_name,
	}


@frappe.whitelist()
def log_print(
	items,
	size: str,
	status: str = "Success",
	failure_reason: str = "",
	source_doctype: str = "",
	source_name: str = "",
) -> None:
	"""Record what was printed (or attempted) - who, what, when, and
	whether it succeeded, and (only on success) bump each Purchase
	Order/Purchase Receipt line's printed-barcode counter. QZ Tray/Zebra
	Browser Print send every label in one combined job per print action,
	so success/failure is only knowable per batch here, not per individual
	label within it - a "Failed" status means none of that row's requested
	labels were confirmed printed."""
	if size not in LABEL_SIZES:
		frappe.throw(_("Invalid label size '{0}'.").format(size))
	if status not in ("Success", "Failed"):
		frappe.throw(_("Invalid status '{0}'.").format(status))

	items = _parse_items(items)
	is_success = status == "Success"

	for item_row in items:
		no_of_barcodes = frappe.utils.cint(item_row.get("no_of_barcodes")) or 1
		source_row_name = item_row.get("source_row_name") or ""

		frappe.get_doc({
			"doctype": "Barcode Print Log",
			"item": item_row.get("item"),
			"item_name": item_row.get("item_name"),
			"barcode": item_row.get("barcode"),
			"barcode_type": item_row.get("barcode_type"),
			"uom": item_row.get("uom"),
			"no_of_barcodes": no_of_barcodes,
			"printed_count": no_of_barcodes if is_success else 0,
			"failed_count": 0 if is_success else no_of_barcodes,
			"status": status,
			"failure_reason": failure_reason if not is_success else "",
			"label_size": size,
			"source_doctype": source_doctype or "",
			"source_name": source_name or "",
			"source_row_name": source_row_name,
			"printed_by": frappe.session.user,
		}).insert(ignore_permissions=True)

		if is_success and source_doctype and source_row_name:
			child_doctype = PURCHASE_ITEM_DOCTYPES.get(source_doctype)
			if child_doctype:
				frappe.db.sql(
					f"UPDATE `tab{child_doctype}` SET custom_barcodes_printed = "
					f"COALESCE(custom_barcodes_printed, 0) + %s WHERE name = %s",
					(no_of_barcodes, source_row_name),
				)
