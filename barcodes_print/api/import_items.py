# Copyright (c) 2026, Rahul Chaudhary and contributors
# For license information, please see license.txt

"""
Bulk-fills the Print Barcode page's item grid from an uploaded Excel/CSV
file, instead of adding rows one at a time. Each row is validated
independently against the same Item/Batch/Serial rules the grid itself
enforces (see barcodes_print.api.item's batch_belongs_to_item/
serial_no_item_and_batch and resolve_row in api/zpl.py) - a bad row is
skipped and reported, never a whole-file failure.
"""

import frappe
from frappe import _
from frappe.defaults import get_user_default
from frappe.utils import cint

from barcodes_print.api.item import batch_belongs_to_item, get_item_barcode_details, serial_no_item_and_batch

MAX_IMPORT_ROWS = 1000

# Accepted header names (case-insensitive, spaces/underscores interchangeable)
# mapped to the canonical grid-row keys they fill.
_HEADER_SYNONYMS = {
	"item_code": "item",
	"item": "item",
	"sku": "item",
	"barcode": "barcode",
	"qty": "no_of_barcodes",
	"quantity": "no_of_barcodes",
	"copies": "no_of_barcodes",
	"no_of_barcodes": "no_of_barcodes",
	"price_list": "price_list",
	"company": "company",
	"tax_template": "tax_template",
	"batch": "batch_no",
	"batch_no": "batch_no",
	"serial": "serial_nos",
	"serial_no": "serial_nos",
	"serial_nos": "serial_nos",
}

TEMPLATE_COLUMNS = (
	"item_code",
	"barcode",
	"no_of_barcodes",
	"price_list",
	"company",
	"tax_template",
	"batch_no",
	"serial_nos",
)


def _normalize_header(header) -> str:
	return str(header or "").strip().lower().replace(" ", "_")


def _read_rows_from_file(file_url: str) -> list:
	"""Raw rows (list of cell lists, header row first) from an uploaded
	.xlsx/.xls or .csv file - whichever it actually is, by extension."""
	lower_url = (file_url or "").lower()
	if lower_url.endswith(".csv"):
		from frappe.utils.csvutils import read_csv_content

		content = frappe.get_doc("File", {"file_url": file_url}).get_content()
		return read_csv_content(content)

	from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

	return read_xlsx_file_from_attached_file(file_url=file_url) or []


def _rows_to_dicts(raw_rows: list) -> list:
	"""First row is headers; every later row becomes a dict keyed by the
	canonical field names in _HEADER_SYNONYMS. An unrecognized header
	column is kept under its own normalized name but never consulted."""
	if not raw_rows:
		return []
	header_keys = [_HEADER_SYNONYMS.get(_normalize_header(h), _normalize_header(h)) for h in raw_rows[0]]
	dicts = []
	for raw_row in raw_rows[1:]:
		if not any(cell not in (None, "") for cell in raw_row):
			continue  # skip a fully blank row rather than reporting it as an error
		dicts.append({key: raw_row[i] if i < len(raw_row) else None for i, key in enumerate(header_keys)})
	return dicts


def _validate_and_normalize_row(raw: dict, row_num: int, default_company: str) -> tuple:
	"""One row's worth of the same checks resolve_row enforces at print
	time (api/zpl.py), run here up front so a bad row never reaches the
	grid. Returns (normalized_row_dict, None) or (None, error_message)."""
	item_code = str(raw.get("item") or "").strip()
	if not item_code:
		return None, _("Row {0}: Item Code is required.").format(row_num)

	item = frappe.db.get_value("Item", item_code, ["disabled"], as_dict=True)
	if not item:
		return None, _("Row {0}: Item {1} does not exist.").format(row_num, item_code)
	if cint(item.disabled):
		return None, _("Row {0}: Item {1} is disabled.").format(row_num, item_code)

	barcode = str(raw.get("barcode") or "").strip()
	if barcode:
		details = get_item_barcode_details(item_code, barcode=barcode)
		if details.get("barcode") != barcode:
			return None, _("Row {0}: Barcode {1} does not belong to Item {2}.").format(row_num, barcode, item_code)

	qty = raw.get("no_of_barcodes")
	qty = cint(qty) if qty not in (None, "") else 1
	if qty < 1:
		return None, _("Row {0}: No. of Barcodes must be a positive whole number.").format(row_num)

	batch_no = str(raw.get("batch_no") or "").strip()
	if batch_no and not batch_belongs_to_item(batch_no, item_code):
		return None, _("Row {0}: Batch No {1} was not found for Item {2}.").format(row_num, batch_no, item_code)

	serial_nos = [s.strip() for s in str(raw.get("serial_nos") or "").replace(",", "\n").splitlines() if s.strip()]
	for serial_no in serial_nos:
		serial_item_code, serial_batch_no = serial_no_item_and_batch(serial_no)
		if serial_item_code is None:
			return None, _("Row {0}: Serial No {1} was not found.").format(row_num, serial_no)
		if serial_item_code != item_code:
			return None, _("Row {0}: Serial No {1} belongs to Item {2}, not {3}.").format(
				row_num, serial_no, serial_item_code, item_code
			)
		if batch_no and serial_batch_no and serial_batch_no != batch_no:
			return None, _("Row {0}: Serial No {1} belongs to Batch {2}, not {3}.").format(
				row_num, serial_no, serial_batch_no, batch_no
			)

	return {
		"item": item_code,
		"barcode": barcode,
		"no_of_barcodes": qty,
		"price_list": str(raw.get("price_list") or "").strip(),
		"company": str(raw.get("company") or "").strip() or default_company,
		"tax_template": str(raw.get("tax_template") or "").strip(),
		"batch_no": batch_no,
		"serial_nos": "\n".join(serial_nos),
	}, None


@frappe.whitelist()
def import_items_from_file(file_url: str) -> dict:
	"""Parse and validate an uploaded Excel/CSV file for the Print Barcode
	page's item grid. Returns {"rows": [...valid rows...], "errors": [...]}
	- a bad row is reported and skipped, never a whole-file failure, except
	the row-count cap below, which rejects the file outright."""
	if not file_url:
		frappe.throw(_("No file was uploaded."))

	raw_rows = _read_rows_from_file(file_url)
	data_rows = _rows_to_dicts(raw_rows)

	if len(data_rows) > MAX_IMPORT_ROWS:
		frappe.throw(
			_("This file has {0} rows, which is over the {1}-row limit. Please split it and upload {1} rows or fewer at a time.").format(
				len(data_rows), MAX_IMPORT_ROWS
			)
		)

	default_company = get_user_default("Company") or ""
	rows, errors = [], []
	for i, raw in enumerate(data_rows, start=2):  # row 1 is the header row
		normalized, error = _validate_and_normalize_row(raw, i, default_company)
		if error:
			errors.append(error)
		else:
			rows.append(normalized)

	return {"rows": rows, "errors": errors}
