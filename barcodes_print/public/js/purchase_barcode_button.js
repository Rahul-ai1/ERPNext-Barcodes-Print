frappe.provide("barcodes_print");

// Shared by public/js/purchase_order.js, purchase_receipt.js,
// purchase_invoice.js, subcontracting_order.js, and
// subcontracting_receipt.js. Whether the button actually appears is
// entirely settings-driven (Barcode Print Settings' two independent
// master switches: Enable Printing From a Purchase Document + Purchase
// Document Type for the first three, Enable Printing From a
// Subcontracting Document + Subcontracting Document Type for the last
// two) - this file runs on every submitted document of that type
// regardless, and simply does nothing when the setting doesn't match
// this doctype.
barcodes_print.add_purchase_print_barcode_button = function (frm, doctype) {
	if (frm.doc.docstatus !== 1) {
		return;
	}
	// A subcontracted Purchase Order/Receipt/Invoice's own items are what
	// you're buying from the subcontractor (raw materials/service cost),
	// not what you're printing labels for - the actual finished-goods
	// receipt happens on its Subcontracting Order/Receipt instead, which
	// get this same button in their own right (see
	// public/js/subcontracting_order.js and subcontracting_receipt.js).
	// All three carry this same flag whenever they're linked to a
	// subcontracted PO, not just Purchase Order itself.
	if (
		["Purchase Order", "Purchase Receipt", "Purchase Invoice"].includes(doctype) &&
		frm.doc.is_subcontracted
	) {
		return;
	}
	// Return (credit note / reversal) documents are explicitly out of
	// scope - printing from a standard document is supported, a return
	// isn't.
	if (["Purchase Invoice", "Subcontracting Receipt"].includes(doctype) && frm.doc.is_return) {
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
