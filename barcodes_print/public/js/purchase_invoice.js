frappe.ui.form.on("Purchase Invoice", {
	refresh(frm) {
		barcodes_print.add_purchase_print_barcode_button(frm, "Purchase Invoice");
	},
});
