frappe.ui.form.on("Purchase Order", {
	refresh(frm) {
		barcodes_print.add_purchase_print_barcode_button(frm, "Purchase Order");
	},
});
