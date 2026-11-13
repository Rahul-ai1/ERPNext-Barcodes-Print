frappe.ui.form.on("Subcontracting Receipt", {
	refresh(frm) {
		barcodes_print.add_purchase_print_barcode_button(frm, "Subcontracting Receipt");
	},
});
