app_name = "barcodes_print"
app_title = "Barcodes Print"
app_publisher = "Rahul Chaudhary"
app_description = "Generate and print barcode labels for thermal barcode printers"
app_email = "rc1248117@outlook.com"
app_license = "MIT"
# Read by frappe.utils.app_logo() for the app's own mark in the dock rail and
# a dock-less sidebar's header - without this it falls back to a plain
# letter avatar, regardless of the Dock/Sidebar fixtures' own icon fields.
# The "?v=N" suffix makes browsers fetch the logo again after it changes
# (assets are otherwise cached for a year) - bump it on every icon update.
app_logo_url = "/assets/barcodes_print/icons/desktop_icons/subtle/barcodes_print.svg?v=4"
app_home = "/app/barcodes-print"
required_apps = ["erpnext"]

# Without this, the app never appears on the /apps switcher page at all -
# not a broken icon there, it's simply absent from the list of tiles.
add_to_apps_screen = [
	{
		"name": app_name,
		"logo": app_logo_url,
		"title": app_title,
		"route": app_home,
	}
]

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

doc_events = {
	"Item": {
		"validate": "barcodes_print.overrides.item.validate_single_default_barcode",
	}
}

doctype_js = {
	# Core ERPNext doctypes this app doesn't own - a real committed file via
	# doctype_js, never a Client Script. Every file here runs on every
	# submitted document of that type regardless of settings; the "Print
	# Barcode" button itself only appears when Barcode Print Settings
	# enables it for that doctype (see public/js/purchase_barcode_button.js).
	# Purchase Order/Purchase Receipt/Purchase Invoice share one single-select
	# (purchase_document_type); Subcontracting Order/Subcontracting Receipt
	# share a separate one (subcontracting_document_type) - independent
	# master switches, so either pair can be on, off, or both at once.
	"Purchase Order": "public/js/purchase_order.js",
	"Purchase Receipt": "public/js/purchase_receipt.js",
	"Purchase Invoice": "public/js/purchase_invoice.js",
	"Subcontracting Order": "public/js/subcontracting_order.js",
	"Subcontracting Receipt": "public/js/subcontracting_receipt.js",
}

