frappe.ui.form.on("Subcontracting Order", {
	refresh(frm) {
		barcodes_print.add_purchase_print_barcode_button(frm, "Subcontracting Order");
	},
});
