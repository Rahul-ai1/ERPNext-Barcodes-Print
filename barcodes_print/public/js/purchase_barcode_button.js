frappe.provide("barcodes_print");

// Shared by public/js/purchase_order.js and public/js/purchase_receipt.js.
// Whether the button actually appears is entirely settings-driven
// (Barcode Print Settings > Enable Printing From a Purchase Document +
// Purchase Document Type) - this file runs on every submitted Purchase
// Order/Purchase Receipt regardless, and simply does nothing when the
// setting doesn't match this doctype.
barcodes_print.add_purchase_print_barcode_button = function (frm, doctype) {
	if (frm.doc.docstatus !== 1) {
		return;
	}
	// A subcontracted Purchase Order's own items are what you're buying
	// from the subcontractor, not what you're printing labels for here -
	// the actual finished-goods receipt happens on its Subcontracting
	// Order instead, which gets this same button in its own right (see
	// public/js/subcontracting_order.js).
	if (doctype === "Purchase Order" && frm.doc.is_subcontracted) {
		return;
	}

	frappe.call({
		method: "barcodes_print.api.zpl.is_purchase_print_enabled_for",
		args: { doctype },
		callback(r) {
			if (!r.message) {
				return;
			}

			frm.add_custom_button(__("Print Barcode"), () => {
				frappe.route_options = {
					barcode_source_doctype: doctype,
					barcode_source_name: frm.doc.name,
				};
				frappe.set_route("print-barcode");
			});
		},
	});
};
