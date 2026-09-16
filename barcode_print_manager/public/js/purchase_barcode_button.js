frappe.provide("barcode_print_manager");

// Shared by public/js/purchase_order.js and public/js/purchase_receipt.js.
// Whether the button actually appears is entirely settings-driven
// (Barcode Print Settings > Enable Printing From a Purchase Document +
// Purchase Document Type) - this file runs on every submitted Purchase
// Order/Purchase Receipt regardless, and simply does nothing when the
// setting doesn't match this doctype.
barcode_print_manager.add_purchase_print_barcode_button = function (frm, doctype) {
	if (frm.doc.docstatus !== 1) {
		return;
	}

	frappe.call({
		method: "barcode_print_manager.api.zpl.is_purchase_print_enabled_for",
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
