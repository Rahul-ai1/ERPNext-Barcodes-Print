app_name = "barcodes_print"
app_title = "Barcodes Print"
app_publisher = "Auriga IT"
app_description = "Generate and print barcode labels for thermal barcode printers"
app_email = "rahul.chaudhary@aurigait.com"
app_license = "MIT"
required_apps = ["erpnext"]

after_install = "barcodes_print.install.after_install.after_install"
after_migrate = "barcodes_print.install.after_install.after_migrate"

# Barcode Print Log is a high-volume audit trail with no long-term business
# value - kept to the last 15 days only (see tasks.LOG_RETENTION_DAYS), swept
# once daily rather than on every migrate/install.
scheduler_events = {
	"cron": {
		"0 2 * * *": ["barcodes_print.tasks.cleanup_old_print_logs"],
	}
}

app_include_js = [
	"/assets/barcodes_print/js/vendor/qz-tray.js",
	"/assets/barcodes_print/js/qz_tray_print.js",
	"/assets/barcodes_print/js/zebra_browser_print.js",
	"/assets/barcodes_print/js/purchase_barcode_button.js",
]
# Zebra's own Browser Print SDK (BrowserPrint-x.x.x.min.js) is proprietary
# and license-gated, so it is NOT bundled with this app and NOT listed
# above - see public/js/vendor/README.md for what to do on a real
# Windows/Mac deployment. Declaring a path to a file that doesn't exist
# here would break `bench build` for the whole site, so add it above
# (before zebra_browser_print.js) only once the real file is in place.

doctype_js = {
	# Purchase Order/Purchase Receipt are core ERPNext doctypes this app
	# doesn't own - a real committed file via doctype_js, never a Client
	# Script. Both files run on every submitted document of that type
	# regardless of settings; the "Print Barcode" button itself only
	# appears when Barcode Print Settings enables it for that doctype
	# (see public/js/purchase_barcode_button.js).
	"Purchase Order": "public/js/purchase_order.js",
	"Purchase Receipt": "public/js/purchase_receipt.js",
}

