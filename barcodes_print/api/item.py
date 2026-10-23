# Copyright (c) 2026, Rahul Chaudhary and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.utils import cint


def get_item_price(item_code: str, price_list: str) -> tuple:
	"""Return (rate, currency) for this Item in the Price List.

	rate is None when no Item Price row exists, so the label can print
	"N/A" instead of a zero. currency comes from that row, then the Price List."""
	if not item_code or not price_list:
		return None, ""
	row = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list},
		["price_list_rate", "currency"],
		as_dict=True,
	)
	if not row:
		return None, ""
	currency = row.currency or frappe.db.get_value("Price List", price_list, "currency") or ""
	return row.price_list_rate, currency


@frappe.whitelist()
def get_item_barcode_details(item_code: str, barcode: str = "", price_list: str = "", batch_no: str = "") -> dict:
	if not item_code:
		return {}

	if not frappe.db.exists("Item", item_code):
		return {}

	item = frappe.get_cached_doc("Item", item_code)
	details = {
		"item_name": item.item_name or "",
		"uom": item.stock_uom or "",
		"has_batch_no": cint(item.has_batch_no),
		"has_serial_no": cint(item.has_serial_no),
		# None (not 0.0) means "no price found" - kept distinct from a
		# genuine zero price so the label can print "N/A" instead of a
		# misleading "0.00". Only ever resolved from the given Price List's
		# own Item Price row - never Item.standard_rate, which isn't
		# necessarily the rate anyone actually sells/buys at.
		"rate": None,
		"currency": "",
		"item_group": item.item_group or "",
		"brand": item.brand or "",
		"description": item.description or "",
		"barcode": "",
		"barcode_type": "",
		"barcode_uom": "",
		# Every barcode this Item has, so a caller with more than one can
		# offer the user an explicit choice instead of silently guessing.
		"barcodes": [],
		# None for a non-stock item. 0 is a real empty bin. A Batch No
		# narrows this to that batch's quantity.
		"stock_qty": get_item_stock_qty(item_code, batch_no),
	}
	if price_list:
		details["rate"], details["currency"] = get_item_price(item_code, price_list)

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


def get_item_stock_qty(item_code: str, batch_no: str = ""):
	"""On-hand quantity for the Print Barcode row.

	Stock items with no bin yet are 0. A non-stock item has no balance.
	When the row names a Batch, the balance is that batch's quantity.
	"""
	if not item_code or not cint(frappe.db.get_value("Item", item_code, "is_stock_item")):
		return None

	batch_no = (batch_no or "").strip()
	if batch_no:
		batch = frappe.db.get_value("Batch", batch_no, ["item", "batch_qty"], as_dict=True)
		if not batch or batch.item != item_code:
			return None
		return frappe.utils.flt(batch.batch_qty)

	from erpnext.stock.utils import get_latest_stock_qty

	return frappe.utils.flt(get_latest_stock_qty(item_code) or 0)


@frappe.whitelist()
def resolve_scan(search_value: str, price_list: str = "") -> dict:
	"""Resolve one scanner value into the fields for a Print Barcode row.

	Uses the same lookup as ERPNext stock documents: Item Barcode, then
	Serial No, then Batch. Item Code is the fallback. A warehouse name is
	not an item, so it resolves to nothing here.
	"""
	search_value = (search_value or "").strip()
	if not search_value:
		return {}

	from erpnext.stock.utils import scan_barcode

	scan = scan_barcode(search_value) or {}
	if scan.get("warehouse") and not scan.get("item_code"):
		return {}

	item_code = scan.get("item_code")
	if not item_code and frappe.db.exists("Item", search_value):
		item_code = search_value
	if not item_code or not frappe.db.exists("Item", item_code):
		return {}
	if cint(frappe.db.get_value("Item", item_code, "disabled")):
		frappe.throw(_("Item {0} is disabled.").format(item_code))

	scanned_barcode = (scan.get("barcode") or "").strip()
	batch_no = scan.get("batch_no") or ""
	details = get_item_barcode_details(
		item_code, barcode=scanned_barcode, price_list=price_list or "", batch_no=batch_no
	)
	details["item"] = item_code
	details["batch_no"] = scan.get("batch_no") or ""
	details["serial_no"] = scan.get("serial_no") or ""
	if scanned_barcode:
		details["barcode"] = scanned_barcode
	if scan.get("uom"):
		details["uom"] = scan.get("uom")
	return details


# Print Barcode's Load from Excel. The first row is a header. One of
# Item Code or Barcode is required. Qty defaults to 1.
MAX_SPREADSHEET_ROWS = 1000
_SHOWN_SKIPPED_ROWS = 50
_HEADER_FIELDS = {
	"item_code": "item",
	"item": "item",
	"itemcode": "item",
	"barcode": "barcode",
	"item_barcode": "barcode",
	"qty": "qty",
	"quantity": "qty",
	"no_of_barcodes": "qty",
	"no_of_barcode": "qty",
	"labels": "qty",
	"copies": "qty",
	"batch_no": "batch_no",
	"batch": "batch_no",
	"batch_number": "batch_no",
	"serial_no": "serial_nos",
	"serial_nos": "serial_nos",
	"serial": "serial_nos",
	"serials": "serial_nos",
	"serial_number": "serial_nos",
	"serial_numbers": "serial_nos",
}


@frappe.whitelist()
def get_items_from_spreadsheet(file_url: str) -> dict:
	"""Rows for the Print Barcode page's Load from Excel button.

	Accepts .xlsx, .xls, and .csv. A bad row is skipped and reported.
	These rows are a one-time label run, same as Load from Purchase Invoice.
	"""
	frappe.has_permission("Item", "read", throw=True)
	header, data_rows, header_row_number = _spreadsheet_table(file_url)
	columns = _spreadsheet_columns(header)
	if len(data_rows) > MAX_SPREADSHEET_ROWS:
		frappe.throw(_("A spreadsheet can load at most {0} rows.").format(MAX_SPREADSHEET_ROWS))

	items = []
	skipped = []
	for index, raw_row in enumerate(data_rows):
		if not any(_cell_text(cell) for cell in raw_row):
			continue
		row_number = header_row_number + index + 1
		parsed, reason = _spreadsheet_label_row(raw_row, columns)
		if reason:
			skipped.append({"row": row_number, "message": reason})
			continue
		items.append(parsed)

	if not items:
		_throw_empty_spreadsheet(skipped)

	return {
		"items": items,
		"skipped": skipped[:_SHOWN_SKIPPED_ROWS],
		"skipped_count": len(skipped),
	}


def _spreadsheet_table(file_url: str):
	rows = _read_spreadsheet_rows(file_url)
	for index, row in enumerate(rows):
		if any(_cell_text(cell) for cell in row):
			return row, rows[index + 1 :], index + 1
	frappe.throw(_("The file has no rows."))


def _read_spreadsheet_rows(file_url: str) -> list:
	file_url = (file_url or "").strip()
	if not file_url:
		frappe.throw(_("Select a file."))
	file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_name:
		frappe.throw(_("The uploaded file was not found."))

	file_doc = frappe.get_doc("File", file_name)
	filename = (file_doc.file_name or file_url).lower()
	path = file_doc.get_full_path()
	try:
		with open(path, "rb") as handle:
			content = handle.read()
	except OSError:
		frappe.throw(_("Could not read the uploaded file."))

	if filename.endswith(".csv"):
		from frappe.utils.csvutils import read_csv_content

		return read_csv_content(content) or []
	if filename.endswith(".xlsx"):
		from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

		return read_xlsx_file_from_attached_file(fcontent=content) or []
	if filename.endswith(".xls"):
		from frappe.utils.xlsxutils import read_xls_file_from_attached_file

		return read_xls_file_from_attached_file(content) or []
	frappe.throw(_("Upload an .xlsx, .xls, or .csv file."))


def _spreadsheet_columns(header: list) -> dict:
	columns = {}
	for index, cell in enumerate(header):
		field = _HEADER_FIELDS.get(_header_key(cell))
		if field and field not in columns:
			columns[field] = index
	if "item" not in columns and "barcode" not in columns:
		frappe.throw(_("The first row must include an Item Code or Barcode column."))
	return columns


def _spreadsheet_label_row(raw_row: list, columns: dict):
	item_text = _column_text(raw_row, columns, "item")
	barcode = _column_text(raw_row, columns, "barcode")
	item_code, reason = _item_for_spreadsheet_row(item_text, barcode)
	if reason:
		return None, reason

	batch_no = _column_text(raw_row, columns, "batch_no")
	reason = _batch_for_item(item_code, batch_no)
	if reason:
		return None, reason

	serials, reason = _serials_for_item(item_code, _column_text(raw_row, columns, "serial_nos"), batch_no)
	if reason:
		return None, reason

	qty, reason = _spreadsheet_qty(_column_value(raw_row, columns, "qty"), serials)
	if reason:
		return None, reason

	return {
		"item": item_code,
		"barcode": barcode,
		"no_of_barcodes": qty,
		"batch_no": batch_no,
		"serial_nos": "\n".join(serials),
	}, ""


def _item_for_spreadsheet_row(item_code: str, barcode: str):
	if item_code:
		if not frappe.db.exists("Item", item_code):
			return "", _("Item {0} was not found.").format(frappe.utils.escape_html(item_code))
		if cint(frappe.db.get_value("Item", item_code, "disabled")):
			return "", _("Item {0} is disabled.").format(frappe.utils.escape_html(item_code))
		if barcode and not _barcode_on_item(item_code, barcode):
			return "", _("Barcode {0} is not on Item {1}.").format(
				frappe.utils.escape_html(barcode), frappe.utils.escape_html(item_code)
			)
		return item_code, ""

	if not barcode:
		return "", _("Enter an Item Code or a Barcode.")
	parent = frappe.db.get_value("Item Barcode", {"barcode": barcode}, "parent")
	if not parent or not frappe.db.exists("Item", parent):
		return "", _("Barcode {0} is not on any Item.").format(frappe.utils.escape_html(barcode))
	if cint(frappe.db.get_value("Item", parent, "disabled")):
		return "", _("Item {0} is disabled.").format(frappe.utils.escape_html(parent))
	return parent, ""


def _barcode_on_item(item_code: str, barcode: str) -> bool:
	return bool(frappe.db.get_value("Item Barcode", {"parent": item_code, "barcode": barcode}, "name"))


def _batch_for_item(item_code: str, batch_no: str) -> str:
	if not batch_no:
		return ""
	batch_item = frappe.db.get_value("Batch", batch_no, "item")
	if batch_item != item_code:
		return _("Batch {0} is not for Item {1}.").format(
			frappe.utils.escape_html(batch_no), frappe.utils.escape_html(item_code)
		)
	return ""


def _serials_for_item(item_code: str, raw_serials: str, batch_no: str):
	serials = []
	for chunk in raw_serials.replace(";", "\n").replace(",", "\n").splitlines():
		serial = chunk.strip()
		if serial and serial not in serials:
			serials.append(serial)
	for serial in serials:
		row = frappe.db.get_value("Serial No", serial, ["item_code", "batch_no"], as_dict=True)
		if not row or row.item_code != item_code:
			return [], _("Serial No {0} is not for Item {1}.").format(
				frappe.utils.escape_html(serial), frappe.utils.escape_html(item_code)
			)
		if batch_no and row.batch_no and row.batch_no != batch_no:
			return [], _("Serial No {0} is not in Batch {1}.").format(
				frappe.utils.escape_html(serial), frappe.utils.escape_html(batch_no)
			)
	return serials, ""


def _spreadsheet_qty(value, serials: list):
	if serials:
		return len(serials), ""
	if value in (None, ""):
		return 1, ""
	number = frappe.utils.flt(value)
	if number < 1 or number != int(number):
		return 0, _("Qty must be a whole number of at least 1.")
	return int(number), ""


def _throw_empty_spreadsheet(skipped: list):
	if not skipped:
		frappe.throw(_("The file has no item rows."))
	lines = "<br>".join(_("Row {0}: {1}").format(row["row"], row["message"]) for row in skipped[:_SHOWN_SKIPPED_ROWS])
	frappe.throw(lines, title=_("Could not load the spreadsheet"))


def _column_text(raw_row: list, columns: dict, field: str) -> str:
	return _cell_text(_column_value(raw_row, columns, field))


def _column_value(raw_row: list, columns: dict, field: str):
	index = columns.get(field)
	if index is None or index >= len(raw_row):
		return None
	return raw_row[index]


def _header_key(value) -> str:
	text = _cell_text(value).lower().replace(".", " ").replace("-", " ").replace("/", " ")
	return "_".join(text.split())


def _cell_text(value) -> str:
	if value is None or isinstance(value, bool):
		return ""
	if isinstance(value, float) and value.is_integer():
		return str(int(value))
	if isinstance(value, int):
		return str(value)
	return str(value).strip()


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


# Any Item value that can be printed as a single line of text. Layout,
# buttons, images, and child tables are not in this set.
PRINTABLE_ITEM_FIELDTYPES = {
	"Data",
	"Link",
	"Select",
	"Int",
	"Float",
	"Currency",
	"Date",
	"Datetime",
	"Time",
	"Check",
	"Percent",
	"Text",
	"Small Text",
	"Long Text",
	"Text Editor",
	"Read Only",
	"Phone",
	"Dynamic Link",
	"Duration",
	"Barcode",
	"Color",
}
TEXT_ITEM_FIELDTYPES = {"Text", "Small Text", "Long Text", "Text Editor"}


# Already printed from Display Rules. The extra-field picker does not list them again.
DISPLAY_RULE_ITEM_FIELDS = {"item_name", "item_code", "stock_uom"}


def iter_item_master_fields():
	"""Parent Item fields that can be added as extra label lines, in form order."""
	meta = frappe.get_meta("Item")
	for df in meta.fields:
		if df.fieldname in DISPLAY_RULE_ITEM_FIELDS:
			continue
		if df.fieldtype not in PRINTABLE_ITEM_FIELDTYPES:
			continue
		if df.get("is_virtual"):
			continue
		yield df


def item_master_field_map() -> dict:
	return {df.fieldname: df for df in iter_item_master_fields()}


@frappe.whitelist()
def get_item_master_fields() -> list:
	"""Field list for the Item Master Fields picker on Barcode Print Settings."""
	frappe.has_permission("Barcode Print Settings", "read", throw=True)
	return [
		{"fieldname": df.fieldname, "label": df.label or df.fieldname}
		for df in iter_item_master_fields()
	]


def format_item_field_value(df, value) -> str:
	"""One Item field as label text. Blank and unchecked values are omitted."""
	if value in (None, ""):
		return ""
	if df.fieldtype == "Check":
		return "Yes" if cint(value) else ""
	if df.fieldtype == "Currency":
		return frappe.utils.fmt_money(value)
	if df.fieldtype == "Date":
		return frappe.utils.formatdate(value)
	if df.fieldtype == "Datetime":
		return frappe.utils.format_datetime(value)
	text = str(value)
	if df.fieldtype in TEXT_ITEM_FIELDTYPES:
		text = frappe.utils.strip_html(text)
	return " ".join(text.split())


def get_item_master_values(item_code: str, fieldnames: list) -> dict:
	"""Values for the Item fields configured on Barcode Print Settings."""
	if not item_code or not fieldnames:
		return {}
	if not frappe.db.exists("Item", item_code):
		return {}

	item = frappe.get_cached_doc("Item", item_code)
	fields = item_master_field_map()
	values = {}
	for fieldname in fieldnames:
		df = fields.get(fieldname)
		if not df:
			continue
		text = format_item_field_value(df, item.get(fieldname))
		if text:
			values[fieldname] = text
	return values
