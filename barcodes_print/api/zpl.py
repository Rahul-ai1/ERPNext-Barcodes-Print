# Copyright (c) 2026, Rahul Chaudhary and contributors
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

from barcodes_print.api.barcode import (
	LABEL_ONLY_BY_SIZE,
	MATRIX_FORMATS,
	_line_spacing_mm,
	get_barcode_print_settings,
	get_display_barcode_type,
	validate_barcode_compatibility,
)
from barcodes_print.api.item import get_item_barcode_details, get_item_master_values

LABEL_SIZES = ("Small", "Medium", "Large")
# Print Barcode page choices. The three Label Only values use that size's
# dimensions and its no-barcode display rule.
PRINT_LABEL_CHOICES = LABEL_SIZES + tuple(LABEL_ONLY_BY_SIZE.values())
MAX_LABEL_COLUMNS = 4

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


def physical_label_size(size: str) -> str:
	"""Small Label Only uses the Small dimensions, and the same for Medium and Large."""
	for physical, label_only in LABEL_ONLY_BY_SIZE.items():
		if size == label_only:
			return physical
	return size


def is_label_only_choice(size: str) -> bool:
	return size in LABEL_ONLY_BY_SIZE.values()


def canonical_label_size(size: str) -> str:
	"""Match a print-page choice. Ignores capitalisation and extra spaces
	so 'small label only' still selects Small Label Only."""
	cleaned = " ".join(str(size or "").replace("\u00a0", " ").split())
	for choice in PRINT_LABEL_CHOICES:
		if cleaned.lower() == choice.lower():
			return choice
	return cleaned


def get_label_dimensions_dots(settings: dict, size: str) -> tuple[int, int]:
	width_field, height_field = LABEL_SIZE_SETTINGS_FIELDS[physical_label_size(size)]
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
	physical = physical_label_size(size)
	# An explicit Label Only choice never prints a barcode, even when the
	# item has one. A Small/Medium/Large choice falls back to Label Only
	# only when the item itself has no barcode.
	label_only = is_label_only_choice(size)
	has_barcode = bool((row.barcode or "").strip()) and not label_only
	if label_only:
		rule_key = size
	elif has_barcode:
		rule_key = physical
	else:
		rule_key = LABEL_ONLY_BY_SIZE[physical]
	rules = settings["display_rules"][rule_key]
	alignment = rules.get("alignment") or "Left"

	resolved_fmt = ""
	if has_barcode:
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
	text_lines_before = _header_lines(settings)
	if rules["show_item_name"] and row.item_name:
		text_lines_before.append({
			"text": _limited_item_name(row.item_name, settings),
			"bold": True,
			"emphasis": True,
		})
	if rules["show_item_code"] and row.item:
		text_lines_before.append(row.item)

	text_lines_after = []
	if has_barcode and rules["show_barcode_number"]:
		text_lines_after.append(row.barcode)
	if has_barcode and rules["show_barcode_type"]:
		text_lines_after.append(row.barcode_type)
	if rules["show_batch_no"] and row.batch_no:
		text_lines_after.append(_("Batch: {0}").format(row.batch_no))
	if rules["show_serial_no"] and row.serial_no:
		text_lines_after.append(_("Serial: {0}").format(row.serial_no))
	item_lines = _item_master_lines(row, settings, rule_key)
	text_lines_after.extend(line for line in item_lines if line.get("before_uom"))
	if rules["show_uom"] and row.uom:
		text_lines_after.append(f"UOM: {row.uom}")
	text_lines_after.extend(line for line in item_lines if not line.get("before_uom"))
	if rules["show_rate"]:
		# Last line on the label. None means no Item Price was found, so
		# the line is "N/A" rather than a silent 0.00.
		text_lines_after.append(_("MRP: {0}").format(_format_mrp(row)))

	# Text height AND barcode bar width both scale with the chosen label
	# size - using a fixed module width/margin regardless of size was the
	# bug: a Medium/Large label ended up with the exact same small,
	# narrow-barred content as Small, just floating in a corner with the
	# rest of the label left empty instead of actually filling it.
	text_height_mm = {"Small": 2.5, "Medium": 3.3, "Large": 4.2}[physical]
	module_width_mm = {"Small": 0.25, "Medium": 0.4, "Large": 0.5}[physical]
	text_h = mm_to_dots(text_height_mm, settings["printer_dpi"])
	module_width_dots = mm_to_dots(module_width_mm, settings["printer_dpi"])
	line_spacing = _line_spacing_dots(settings)
	text_block = sum(
		_line_font_height(entry, text_h) + line_spacing
		for entry in text_lines_before + text_lines_after
	)
	margin = mm_to_dots(1.5, settings["printer_dpi"])
	header_top_gap = _header_top_gap(text_lines_before, settings["printer_dpi"])
	barcode_gap = mm_to_dots(1.5, settings["printer_dpi"])
	min_barcode_height = mm_to_dots(6.0, settings["printer_dpi"])

	barcode_height_dots = 0
	if has_barcode:
		barcode_height_dots = max(
			min_barcode_height,
			height_dots - (2 * margin) - header_top_gap - text_block - barcode_gap,
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

	y = margin + header_top_gap
	for text in text_lines_before:
		# ^FB (Field Block) gives text fields native justification within
		# a width - no barcode does, so the barcode below is positioned
		# manually further down using an estimated rendered width instead.
		y = _append_text_line(lines, text, y, margin, available_width, fb_justify, text_h, line_spacing)

	if has_barcode:
		barcode_width_dots = _estimate_barcode_width_dots(resolved_fmt, row.barcode, barcode_height_dots, module_width_dots)
		barcode_x = _aligned_x(alignment, margin, available_width, barcode_width_dots)
		barcode_zpl = _barcode_field_zpl(resolved_fmt, row.barcode, barcode_height_dots)
		lines.append(f"^FO{barcode_x},{y}^BY{module_width_dots},3,{barcode_height_dots}")
		lines.append(barcode_zpl)
		y += barcode_height_dots + barcode_gap

	for text in text_lines_after:
		y = _append_text_line(lines, text, y, margin, available_width, fb_justify, text_h, line_spacing)

	lines.append("^XZ")
	return "\n".join(lines)


def _format_mrp(row) -> str:
	"""Price with the currency symbol, then the tax note. No Item Price prints N/A."""
	if row.rate is None:
		return _("N/A")
	currency = getattr(row, "currency", None) or frappe.defaults.get_global_default("currency") or ""
	amount = frappe.utils.fmt_money(row.rate, currency=currency or None)
	if currency and frappe.defaults.get_global_default("hide_currency_symbol") == "Yes":
		symbol = frappe.db.get_value("Currency", currency, "symbol", cache=True) or currency
		if symbol not in amount:
			on_right = frappe.db.get_value("Currency", currency, "symbol_on_right", cache=True)
			amount = f"{amount} {symbol}" if on_right else f"{symbol} {amount}"
	return _("{0} (Inclusive of all taxes)").format(amount)


def _line_spacing_dots(settings: dict) -> int:
	"""Extra gap between text rows. An unset value keeps the previous 0.6mm."""
	spacing_mm = _line_spacing_mm(settings.get("line_spacing_mm"))
	dpi = settings["printer_dpi"]
	return max(0, round(spacing_mm * dpi / 25.4))


def _header_top_gap(lines, dpi: int) -> int:
	"""Extra space above the header. Labels without a header keep the normal margin."""
	if any(isinstance(line, dict) and line.get("header") for line in lines):
		return mm_to_dots(1.5, dpi)
	return 0


def _limited_item_name(item_name: str, settings: dict) -> str:
	"""0 or blank prints the whole item name. A positive limit cuts it there."""
	limit = frappe.utils.cint(settings.get("item_name_character_limit"))
	if limit > 0:
		return item_name[:limit]
	return item_name


def _header_lines(settings: dict) -> list:
	"""Free-text header from Barcode Print Settings, one printed row per line.
	Header alignment is separate from the Display Rule alignment."""
	header = (settings.get("header_text") or "").strip()
	justify = {"Left": "L", "Center": "C", "Right": "R"}.get(settings.get("header_alignment") or "Left", "L")
	return [
		{"text": line.strip(), "justify": justify, "bold": True, "emphasis": True, "header": True}
		for line in header.splitlines()
		if line.strip()
	]


def _is_net_weight(field) -> bool:
	"""Net Weight prints above the UOM line."""
	label = (field.get("label") or "").strip().lower()
	return field.get("fieldname") == "weight_per_unit" or label == "net weight"


def _item_master_lines(row, settings: dict, rule_key: str) -> list:
	"""Extra Item master fields enabled for this display rule."""
	values = getattr(row, "item_master_values", None) or {}
	lines = []
	for field in settings.get("item_fields") or []:
		if not (field.get("rules") or {}).get(rule_key):
			continue
		value = values.get(field["fieldname"])
		if value:
			lines.append({
				"text": f"{field.get('label') or field['fieldname']}: {value}",
				"before_uom": _is_net_weight(field),
			})
	return lines


def _emphasis_height(text_h: int) -> int:
	"""Header and item name are one step larger than the other lines."""
	return max(text_h + 2, round(text_h * 1.25))


def _line_font_height(entry, text_h: int) -> int:
	if isinstance(entry, dict) and entry.get("emphasis"):
		return _emphasis_height(text_h)
	return text_h


def _append_text_line(lines, entry, y, margin, available_width, fb_justify, text_h, line_spacing):
	"""Print one text row. Font 0 has no bold face, so Bold reprints the
	same text one dot to the right and the strokes look heavier."""
	if isinstance(entry, dict):
		text = entry.get("text") or ""
		bold = bool(entry.get("bold"))
		fb_justify = entry.get("justify") or fb_justify
	else:
		text = entry
		bold = False
	font_h = _line_font_height(entry, text_h)
	field = f"^FB{available_width},1,0,{fb_justify}^A0N,{font_h},{font_h}^FD{_zpl_escape(text)}^FS"
	lines.append(f"^FO{margin},{y}{field}")
	if bold:
		lines.append(f"^FO{margin + 1},{y}{field}")
	return y + font_h + line_spacing


_FO_X = re.compile(r"\^FO(\d+),")


def _shift_field_x(line: str, offset: int) -> str:
	"""Move one label's fields across the roll. Only the ^FO origin changes;
	barcode commands that follow it stay attached to that origin."""
	if not offset or not line.startswith("^FO"):
		return line
	return _FO_X.sub(lambda match: f"^FO{int(match.group(1)) + offset},", line, count=1)


def _label_body(zpl: str) -> list[str]:
	"""Field commands for one label, without the ^XA/^PW/^LL wrapper."""
	body = []
	for line in zpl.splitlines():
		if line in ("^XA", "^XZ") or line.startswith(("^PW", "^LL", "^LH", "^CI")):
			continue
		body.append(line)
	return body


def combine_label_zpl(
	label_zpls: list[str], columns: int, width_dots: int, height_dots: int, gap_dots: int
) -> str:
	"""Place labels across the roll. One column keeps each label as its own
	print (unchanged). Two or more columns put that many labels on one row
	and advance the media only after the row is full. A short last row still
	uses the full row width so the next print lines up with the stock."""
	if columns <= 1:
		return "\n".join(label_zpls)

	print_width = (columns * width_dots) + ((columns - 1) * gap_dots)
	pitch = width_dots + gap_dots
	bodies = [_label_body(zpl) for zpl in label_zpls]
	rows = []
	for start in range(0, len(bodies), columns):
		lines = ["^XA", f"^PW{print_width}", f"^LL{height_dots}", "^LH0,0", "^CI28"]
		for col, body in enumerate(bodies[start : start + columns]):
			offset = col * pitch
			lines.extend(_shift_field_x(line, offset) for line in body)
		lines.append("^XZ")
		rows.append("\n".join(lines))
	return "\n".join(rows)


def _resolve_columns(number_of_columns, settings: dict) -> int:
	if number_of_columns in (None, ""):
		columns = frappe.utils.cint(settings.get("number_of_columns")) or 1
	else:
		columns = frappe.utils.cint(number_of_columns)
	if columns < 1 or columns > MAX_LABEL_COLUMNS:
		frappe.throw(_("Number of Columns must be between 1 and {0}.").format(MAX_LABEL_COLUMNS))
	return columns


def _resolve_column_gap_dots(column_gap_mm, settings: dict, columns: int) -> int:
	if columns <= 1:
		return 0
	if column_gap_mm in (None, ""):
		gap_mm = frappe.utils.flt(settings.get("column_gap_mm"))
	else:
		gap_mm = frappe.utils.flt(column_gap_mm)
	if gap_mm < 0:
		frappe.throw(_("Gap Between Columns cannot be negative."))
	# mm_to_dots() forces at least 1 dot, which would shove a 0mm gap.
	return max(0, round(gap_mm * settings["printer_dpi"] / 25.4))


def resolve_row(
	item_code: str,
	barcode: str = "",
	barcode_type: str = "",
	no_of_barcodes=1,
	idx=1,
	source_row_name: str = "",
	price_list: str = "",
	batch_no: str = "",
	serial_no: str = "",
	label_only: bool = False,
) -> "frappe._dict":
	"""Fetch and validate everything needed to print one item row, mirroring
	business rules 5.1/5.2: auto-fetch Item Name/Barcode/Barcode Type/UOM
	from the Item master, block on a missing Barcode, and auto-detect the
	Barcode Type when it's left blank (business rule 5.3).

	A Serial No or Batch No, once given, becomes the label's own barcode
	value in place of the Item's generic Item Barcode - overrides any
	barcode/barcode_type also passed in. Scanning that value anywhere in
	ERPNext (erpnext.stock.utils.scan_barcode) resolves straight back to
	this Item - and, for a serial, its Batch too - with no change to
	ERPNext core, since Serial No/Batch are already directly scannable
	there. Code 128 is used regardless of the Item's own registered
	symbology, since these are free-form Frappe document names, not
	fixed-format retail codes."""
	if not item_code:
		frappe.throw(_("Item is required."))

	no_of_barcodes = frappe.utils.cint(no_of_barcodes) or 1
	if no_of_barcodes < 1:
		frappe.throw(_("No. of Barcodes must be at least 1."))

	if serial_no:
		serial_doc = frappe.db.get_value("Serial No", serial_no, ["item_code", "batch_no"], as_dict=True)
		if not serial_doc:
			frappe.throw(_("Row #{0}: Serial No {1} was not found.").format(idx, serial_no))
		if serial_doc.item_code != item_code:
			frappe.throw(
				_("Row #{0}: Serial No {1} belongs to Item {2}, not {3}.").format(
					idx, serial_no, serial_doc.item_code, item_code
				)
			)
		batch_no = batch_no or serial_doc.batch_no or ""
		barcode = serial_no
		barcode_type = "Code 128"
		no_of_barcodes = 1
	elif batch_no:
		if not frappe.db.exists("Batch", {"name": batch_no, "item": item_code}):
			frappe.throw(_("Row #{0}: Batch No {1} was not found for Item {2}.").format(idx, batch_no, item_code))
		barcode = batch_no
		barcode_type = "Code 128"

	encoding_batch_or_serial = bool(serial_no or batch_no)
	details = get_item_barcode_details(item_code, "" if encoding_batch_or_serial else (barcode or ""), price_list or "")
	row = frappe._dict(
		idx=idx,
		item=item_code,
		item_name=details.get("item_name") or "",
		uom=details.get("uom") or "",
		# None means no price was found for the resolved Price List (or
		# none was configured) - build_label_zpl prints "N/A" for that,
		# never a silent 0.00.
		rate=details.get("rate"),
		currency=details.get("currency") or "",
		price_list=price_list or "",
		barcode=barcode or details.get("barcode") or "",
		barcode_type=barcode_type or details.get("barcode_type") or "",
		barcode_uom=details.get("barcode_uom") or "",
		batch_no=batch_no or "",
		serial_no=serial_no or "",
		no_of_barcodes=no_of_barcodes,
		source_row_name=source_row_name or "",
	)

	if label_only:
		# The Print Barcode page asked for a Label Only size. Keep the
		# item text and skip barcode selection and validation.
		pass
	elif not (row.barcode or "").strip():
		available = details.get("barcodes") or []
		if len(available) > 1:
			# resolve_row/get_item_barcode_details deliberately leave the
			# barcode blank rather than guessing when there's more than
			# one to choose from - this is that guard actually landing,
			# not a "no barcode at all" situation.
			frappe.throw(
				_("Item {0} has {1} barcodes. Please select which one to print.").format(item_code, len(available))
			)
		# No barcode on the item: print a text label. build_label_zpl
		# uses that size's Label Only display rule and skips the barcode.
	else:
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

	fieldnames = [field["fieldname"] for field in get_barcode_print_settings().get("item_fields") or []]
	row.item_master_values = get_item_master_values(item_code, fieldnames)
	return row


def _parse_items(items) -> list:
	if isinstance(items, str):
		items = frappe.parse_json(items)
	if not items:
		frappe.throw(_("Please add at least one item row."))
	return items


def _parse_serial_nos(serial_nos) -> list:
	"""Print Label Item's serial_nos field is a newline-separated Small Text
	(same convention as the row's own multi-select picker); also accepts a
	plain list, in case a caller already has one."""
	if not serial_nos:
		return []
	if isinstance(serial_nos, str):
		return [s.strip() for s in serial_nos.splitlines() if s.strip()]
	return [str(s).strip() for s in serial_nos if str(s).strip()]


# Maps a Purchase Document Type (Barcode Print Settings, plus Subcontracting
# Order which is always available - see is_purchase_print_enabled_for) to
# its items child doctype - all three get the "Barcodes Printed" counter
# field (see install.after_install.ensure_purchase_barcode_tracking_fields),
# so switching the setting later doesn't require adding fields again.
# Subcontracting Order's own "items" field is specifically its finished-goods
# table - its "service_items"/"supplied_items" child tables are deliberately
# never touched here.
PURCHASE_ITEM_DOCTYPES = {
	"Purchase Order": "Purchase Order Item",
	"Purchase Receipt": "Purchase Receipt Item",
	"Subcontracting Order": "Subcontracting Order Item",
}


def _is_stock_item(item_code: str) -> bool:
	"""Service/non-stock items never get a physical barcode label - they
	have no unit to attach one to."""
	return bool(frappe.get_cached_value("Item", item_code, "is_stock_item"))


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
	on Purchase Order/Purchase Receipt/Subcontracting Order forms
	(public/js/purchase_barcode_button.js). Uses a raw single-value DB read
	rather than get_barcode_print_settings() deliberately: Barcode Print
	Settings itself is read-restricted to System Manager, but anyone who can
	already view the source document should be able to see whether this
	button applies, without needing separate access to Settings."""
	enabled = frappe.db.get_single_value("Barcode Print Settings", "enable_purchase_document_printing")
	if not enabled:
		return False
	if doctype == "Subcontracting Order":
		# Not an alternative alongside Purchase Order/Purchase Receipt - this
		# is where a *subcontracted* Purchase Order's own button redirects
		# to, so it's available whenever purchase-document printing is on
		# at all, regardless of which of the other two is configured.
		return True
	configured_doctype = frappe.db.get_single_value("Barcode Print Settings", "purchase_document_type")
	return configured_doctype == doctype


@frappe.whitelist()
def get_purchase_document_items(source_doctype: str, source_name: str) -> dict:
	"""Pre-fill data for the Print Barcode page when opened via the Print
	Barcode button on a submitted Purchase Order/Purchase Receipt/
	Subcontracting Order. Each row's starting 'No. of Barcodes' always
	defaults to that line's own quantity, never quantity + extra - the
	Max Extra Barcodes per Line allowance (or no limit at all, in
	unlimited_reprints mode) only ever matters if the user deliberately
	raises the number themselves, e.g. for a torn/damaged label reprint.
	Service/non-stock item rows are skipped outright - they have no
	physical unit to label."""
	settings = get_barcode_print_settings()
	if not settings["enable_purchase_document_printing"]:
		frappe.throw(_("Printing from a Purchase Document is not enabled in Barcode Print Settings."))
	if source_doctype != "Subcontracting Order" and settings["purchase_document_type"] != source_doctype:
		frappe.throw(
			_("Barcode Print Settings is currently configured for {0}, not {1}.").format(
				settings["purchase_document_type"] or _("no document"), source_doctype
			)
		)

	frappe.has_permission(source_doctype, "read", throw=True)
	doc = frappe.get_doc(source_doctype, source_name)
	if doc.docstatus != 1:
		frappe.throw(_("{0} must be submitted before printing barcodes.").format(source_doctype))

	if source_doctype == "Purchase Order" and doc.get("is_subcontracted"):
		frappe.throw(
			_(
				"{0} is subcontracted - its own items are what's being purchased from the "
				"subcontractor, not what's being received. Print barcodes from its Subcontracting "
				"Order instead."
			).format(source_name)
		)

	# Subcontracting Order's own "items" field is already just the finished
	# goods table - "service_items"/"supplied_items" are separate fields on
	# that doctype and are never read here.
	rows = []
	for row in doc.items:
		if not _is_stock_item(row.item_code):
			continue
		qty = frappe.utils.cint(row.qty)
		# Always defaults to the line's own quantity - never quantity + extra.
		# The extra allowance (or no limit at all, in unlimited_reprints
		# mode) only ever matters if the user deliberately raises this
		# number themselves, e.g. to reprint a torn/damaged label.
		no_of_barcodes = qty
		max_allowed = 0
		if not settings["unlimited_reprints"]:
			already_printed = frappe.utils.cint(row.get("custom_barcodes_printed"))
			max_allowed = qty + settings["max_extra_barcodes_per_line"]
			remaining = max(0, max_allowed - already_printed)
			# Never suggest more than the line quantity by default, but don't
			# suggest more than what's actually still allowed either.
			no_of_barcodes = min(qty, remaining)

		rows.append({
			"item": row.item_code,
			"no_of_barcodes": no_of_barcodes,
			"source_row_name": row.name,
			"max_allowed": max_allowed,
		})

	return {"source_doctype": source_doctype, "source_name": source_name, "items": rows}


@frappe.whitelist()
def get_quick_print_job(
	items,
	size: str,
	source_doctype: str = "",
	source_name: str = "",
	number_of_columns=None,
	column_gap_mm=None,
) -> dict:
	"""Server-side step of the print flow (plan step 6.6/3): resolve and
	validate every row, then build the full ZPL print job. The browser
	sends this, unmodified, straight to QZ Tray or Zebra Browser Print -
	no PDF, no print preview, no browser print dialog, and nothing is
	saved as a document - this is a one-time-use print action."""
	size = canonical_label_size(size)
	if size not in PRINT_LABEL_CHOICES:
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
	columns = _resolve_columns(number_of_columns, settings)
	gap_dots = _resolve_column_gap_dots(column_gap_mm, settings, columns)

	blocks = []
	resolved_items = []
	total_labels = 0
	for idx, item_row in enumerate(items, start=1):
		source_row_name = item_row.get("source_row_name") or ""
		# A row can override the Price List used for its own rate; falls
		# back to whatever's configured in Barcode Print Settings.
		price_list = item_row.get("price_list") or settings["default_price_list"]
		batch_no = item_row.get("batch_no") or ""
		serial_nos = _parse_serial_nos(item_row.get("serial_nos"))
		# One serial = one physical unit = exactly one label each, so a
		# row expands into len(serial_nos) labels instead of repeating a
		# single barcode value no_of_barcodes times.
		row_label_count = len(serial_nos) if serial_nos else frappe.utils.cint(item_row.get("no_of_barcodes", 1)) or 1

		if source_doctype and source_row_name and not settings["unlimited_reprints"]:
			already_printed, max_allowed = _get_purchase_line_allowance(
				source_doctype, source_row_name, settings["max_extra_barcodes_per_line"]
			)
			remaining = max_allowed - already_printed
			if row_label_count > remaining:
				frappe.throw(
					_(
						"Row #{0}: only {1} more label(s) may be printed for this line "
						"(limit is quantity + {2} extra, {3} already printed). Enable \"No Printing "
						"Limit\" in Barcode Print Settings to remove this cap."
					).format(idx, max(0, remaining), settings["max_extra_barcodes_per_line"], already_printed)
				)

		for serial_no in serial_nos or [""]:
			row = resolve_row(
				item_row.get("item"),
				item_row.get("barcode", ""),
				item_row.get("barcode_type", ""),
				item_row.get("no_of_barcodes", 1),
				idx,
				source_row_name,
				price_list,
				batch_no,
				serial_no,
				label_only=is_label_only_choice(size),
			)

			label_zpl = build_label_zpl(row, settings, size)
			blocks.extend([label_zpl] * row.no_of_barcodes)
			total_labels += row.no_of_barcodes
			resolved_items.append(dict(row))

	physical = physical_label_size(size)
	width_mm_field, height_mm_field = LABEL_SIZE_SETTINGS_FIELDS[physical]
	width_dots, height_dots = get_label_dimensions_dots(settings, physical)

	return {
		"zpl": combine_label_zpl(blocks, columns, width_dots, height_dots, gap_dots),
		"total_labels": total_labels,
		"number_of_columns": columns,
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
	size = canonical_label_size(size)
	if size not in PRINT_LABEL_CHOICES:
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
			"batch_no": item_row.get("batch_no") or "",
			"serial_no": item_row.get("serial_no") or "",
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
