frappe.ui.form.on("Barcode Print Settings", {
	refresh(frm) {
		// Fixed rows: Small, Medium, Large, and a Label Only row for each size.
		frm.set_df_property("display_rules", "cannot_add_rows", 1);
		frm.set_df_property("display_rules", "cannot_delete_rows", 1);
		load_item_master_field_options(frm);
	},
});

frappe.ui.form.on("Barcode Label Item Field", {
	item_field(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const match = (frm._item_master_fields || []).find((field) => field.fieldname === row.item_field);
		frappe.model.set_value(cdt, cdn, "field_label", match ? match.label : row.item_field || "");
	},
});

function load_item_master_field_options(frm) {
	frappe.call({
		method: "barcodes_print.api.item.get_item_master_fields",
		callback(r) {
			const fields = r.message || [];
			frm._item_master_fields = fields;
			const options = fields.map((field) => field.fieldname).join("\n");
			const df = frappe.meta.get_docfield("Barcode Label Item Field", "item_field");
			if (df) {
				df.options = options;
			}
			const grid = frm.fields_dict.item_display_fields && frm.fields_dict.item_display_fields.grid;
			if (!grid) {
				return;
			}
			const grid_df = (grid.docfields || []).find((field) => field.fieldname === "item_field");
			if (grid_df) {
				grid_df.options = options;
			}
			grid.refresh();
		},
	});
}
