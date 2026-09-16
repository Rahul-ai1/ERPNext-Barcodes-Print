# Copyright (c) 2026 and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import add_days, now_datetime

# Barcode Print Log is a high-volume, append-only audit trail (one row per
# item per print action) with no business value beyond recent troubleshooting
# - kept short-lived on purpose rather than growing forever. See the
# "N/A"-style notice on the list view (barcode_print_log_list.js) for the
# user-facing side of this same policy.
LOG_RETENTION_DAYS = 15


def cleanup_old_print_logs():
	cutoff = add_days(now_datetime(), -LOG_RETENTION_DAYS)
	frappe.db.delete("Barcode Print Log", {"creation": ["<", cutoff]})
