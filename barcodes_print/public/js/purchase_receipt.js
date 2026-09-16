frappe.ui.form.on("Purchase Receipt", {
	refresh(frm) {
		barcodes_print.add_purchase_print_barcode_button(frm, "Purchase Receipt");
	},
});
