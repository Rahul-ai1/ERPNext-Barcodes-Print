frappe.ui.form.on("Barcode Print Settings", {
	refresh(frm) {
		// One Display Rule per size. Item Master Fields rows are added on purpose.
		frm.set_df_property("display_rules", "cannot_add_rows", 1);
		frm.set_df_property("display_rules", "cannot_delete_rows", 1);
		const grid = frm.fields_dict.item_display_fields.grid;
		grid.df.cannot_add_rows = 0;
		grid.cannot_add_rows = 0;
		grid.df.cannot_delete_rows = 0;
		grid.wrapper.find(".grid-add-row").removeClass("hidden d-none");
		if (!grid._item_field_add_wrapped) {
			grid.add_new_row = () => {
				_open_add_item_field_dialog(frm);
			};
			grid._item_field_add_wrapped = true;
		}
		_load_item_field_options(frm);
	},
	add_item_display_field(frm) {
		_open_add_item_field_dialog(frm);
	},
});

frappe.ui.form.on("Barcode Label Item Field", {
	item_field(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const label = (frm._item_field_labels || {})[row.item_field];
		if (row.item_field && !row.field_label && label) {
			frappe.model.set_value(cdt, cdn, "field_label", label);
		}
	},
});

function _load_item_field_options(frm) {
	frappe.call({
		method: "barcodes_print.api.item.get_item_master_fields",
		callback(r) {
			const fields = r.message || [];
			frm._item_field_labels = {};
			fields.forEach((field) => {
				frm._item_field_labels[field.fieldname] = field.label;
			});
			const grid = frm.fields_dict.item_display_fields.grid;
			if (!grid.grid_rows) {
				return;
			}
			grid.update_docfield_property(
				"item_field",
				"options",
				fields.map((field) => field.fieldname).join("\n")
			);
		},
	});
}

function _open_add_item_field_dialog(frm) {
	const labels = frm._item_field_labels || {};
	const used = new Set((frm.doc.item_display_fields || []).map((row) => row.item_field));
	const options = Object.keys(labels).filter((fieldname) => fieldname && !used.has(fieldname));
	if (!options.length) {
		frappe.msgprint(__("There are no more Item fields to add."));
		return;
	}

	const dialog = new frappe.ui.Dialog({
		title: __("Add Item Field"),
		fields: [
			{
				fieldname: "item_field",
				fieldtype: "Select",
				label: __("Item Field"),
				options: options.join("\n"),
				reqd: 1,
				onchange() {
					const fieldname = dialog.get_value("item_field");
					dialog.set_df_property("item_field", "description", labels[fieldname] || "");
				},
			},
		],
		primary_action_label: __("Add Row"),
		primary_action(values) {
			frm.add_child("item_display_fields", {
				item_field: values.item_field,
				field_label: labels[values.item_field] || values.item_field,
				bold: 0,
				show_small: 1,
				show_medium: 1,
				show_large: 1,
				show_small_label_only: 1,
				show_medium_label_only: 1,
				show_large_label_only: 1,
			});
			frm.refresh_field("item_display_fields");
			dialog.hide();
		},
	});
	dialog.show();
}
