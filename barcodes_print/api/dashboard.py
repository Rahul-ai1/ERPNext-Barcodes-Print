# Copyright (c) 2026, Rahul Chaudhary and contributors
# For license information, please see license.txt

"""Custom Number Card backends for the Barcodes Print workspace - each one
needs a relative date ("today", "last 24 hours") that a plain Number Card
filter can't express, so these compute the value live on every dashboard
load instead."""

import frappe
from frappe.utils import add_to_date, now_datetime, nowdate


def _check_read_permission():
	frappe.has_permission("Barcode Print Log", "read", throw=True)


@frappe.whitelist()
def print_jobs_today() -> dict:
	_check_read_permission()
	count = frappe.db.count("Barcode Print Log", {"creation": [">=", nowdate()]})
	return {"value": count, "fieldtype": "Int"}


@frappe.whitelist()
def labels_printed_today() -> dict:
	_check_read_permission()
	total = frappe.db.sql(
		"select coalesce(sum(printed_count), 0) from `tabBarcode Print Log` where creation >= %s",
		(nowdate(),),
	)[0][0]
	return {"value": total, "fieldtype": "Int"}


@frappe.whitelist()
def failures_last_24h() -> dict:
	_check_read_permission()
	cutoff = add_to_date(now_datetime(), hours=-24)
	count = frappe.db.count("Barcode Print Log", {"status": "Failed", "creation": [">=", cutoff]})
	return {"value": count, "fieldtype": "Int"}
