frappe.ui.form.on("Barcode Print Settings", {
	refresh(frm) {
		// Fixed rows: one Display Rule per size, and one Item field per printable field.
		frm.set_df_property("display_rules", "cannot_add_rows", 1);
		frm.set_df_property("display_rules", "cannot_delete_rows", 1);
		frm.set_df_property("item_display_fields", "cannot_add_rows", 1);
		frm.set_df_property("item_display_fields", "cannot_delete_rows", 1);
	},
});
