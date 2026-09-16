frappe.ui.form.on("Purchase Order", {
	refresh(frm) {
		barcode_print_manager.add_purchase_print_barcode_button(frm, "Purchase Order");
	},
});
