frappe.listview_settings["Barcode Print Log"] = {
	onload(listview) {
		listview.page.add_inner_message(
			__("This log only keeps the last 15 days of print activity - older entries are cleared automatically every night.")
		);
	},
};
