frappe.ui.form.on("Purchase Receipt", {
	refresh(frm) {
		barcode_print_manager.add_purchase_print_barcode_button(frm, "Purchase Receipt");
	},
});
