app_name = "barcode_print_manager"
app_title = "Barcode Print Manager"
app_publisher = "Yash"
app_description = "Generate and print barcode labels for thermal barcode printers"
app_email = "yash@example.com"
app_license = "MIT"
required_apps = ["erpnext"]

after_install = "barcode_print_manager.install.after_install.after_install"
after_migrate = "barcode_print_manager.install.after_install.after_migrate"

app_include_js = [
	"/assets/barcode_print_manager/js/vendor/qz-tray.js",
	"/assets/barcode_print_manager/js/qz_tray_print.js",
	"/assets/barcode_print_manager/js/zebra_browser_print.js",
	"/assets/barcode_print_manager/js/purchase_barcode_button.js",
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

