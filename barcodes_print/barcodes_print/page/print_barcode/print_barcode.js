frappe.pages["print-barcode"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Print Barcode"),
		single_column: true,
	});

	// render() builds the grid asynchronously (it needs Barcode Print
	// Settings' Default Price List first) - apply_route_options() reads
	// _state, which render() only sets once that call resolves, so it has
	// to wait for the same promise rather than firing right away.
	barcodes_print.print_barcode_page.render(page).then(() => {
		barcodes_print.print_barcode_page.apply_route_options();
	});
};

// Re-check for a fresh hand-off from a Purchase Order/Purchase Receipt
// "Print Barcode" button every time this page is (re-)shown - on_page_load
// only fires the first time this route is ever visited in the session.
frappe.pages["print-barcode"].refresh = function () {
	barcodes_print.print_barcode_page.apply_route_options();
};

frappe.provide("barcodes_print.print_barcode_page");

// Row field definitions for the Items table. Declared once and reused for
// the on-page control - a standalone field control (no real form behind
// it) needs its child row fields declared inline rather than resolved from
// the "Print Label Item" doctype's own meta, and its rows aren't tagged
// with a real doctype, so auto-fetch is wired via this field's own
// onchange instead of a doctype-keyed frappe.ui.form.on trigger.
// Shown whenever an Item has more than one barcode registered - printing
// silently uses the first one otherwise, which may not be the one the
// user actually wants on the label (e.g. a retail EAN-13 vs. an internal
// Code 128 SKU code). Leaving the dialog open (or cancelling it) leaves
// Barcode blank, which get_quick_print_job then blocks on server-side
// with a clear "please select which one to print" message.
barcodes_print.print_barcode_page.prompt_barcode_choice = function (row, choices) {
	const options = choices.map(
		(b) => `${b.barcode} (${b.barcode_type || __("no type")})`
	);
	frappe.prompt(
		{
			fieldname: "selected",
			fieldtype: "Select",
			label: __("This item has {0} barcodes - which one should print?", [choices.length]),
			options: options,
			reqd: 1,
		},
		(values) => {
			const chosen = choices[options.indexOf(values.selected)];
			row.doc.barcode = chosen.barcode;
			row.doc.barcode_type = chosen.barcode_type;
			if (chosen.uom) {
				row.doc.uom = chosen.uom;
			}
			["barcode", "barcode_type", "uom"].forEach((f) => row.refresh_field(f));
		},
		__("Select Barcode"),
		__("Use This Barcode")
	);
};

// Re-fetches this row's rate for whatever Item + Price List it currently
// has - called after either one changes. Leaves rate blank (server-side,
// that means None -> the label prints "N/A") when there's no Item yet or
// no Price List to look it up in.
barcodes_print.print_barcode_page._refresh_row_rate = function (row) {
	const item_code = row.doc.item;
	const price_list = row.doc.price_list;
	if (!item_code || !price_list) {
		row.doc.rate = null;
		row.refresh_field("rate");
		return;
	}
	frappe.call({
		method: "barcodes_print.api.item.get_item_barcode_details",
		args: { item_code, price_list },
		callback(r) {
			row.doc.rate = (r.message || {}).rate;
			row.refresh_field("rate");
		},
	});
};

barcodes_print.print_barcode_page.get_item_fields = function (default_price_list) {
	return [
		{
			fieldname: "item",
			fieldtype: "Link",
			options: "Item",
			in_list_view: 1,
			label: __("Item"),
			reqd: 1,
			onchange: function () {
				const row = this.grid_row;
				const item_code = this.value;
				if (!item_code) {
					["item_name", "uom", "barcode", "barcode_type"].forEach((f) => {
						row.doc[f] = "";
						row.refresh_field(f);
					});
					row.doc.rate = null;
					row.refresh_field("rate");
					barcodes_print.print_barcode_page.update_total();
					return;
				}
				frappe.call({
					method: "barcodes_print.api.item.get_item_barcode_details",
					args: { item_code, price_list: row.doc.price_list || "" },
					callback(r) {
						const data = r.message || {};
						row.doc.item_name = data.item_name || "";
						row.doc.uom = data.uom || "";
						row.doc.barcode = data.barcode || "";
						row.doc.barcode_type = data.barcode_type || "";
						row.doc.rate = data.rate;
						["item_name", "uom", "barcode", "barcode_type", "rate"].forEach((f) => row.refresh_field(f));

						// More than one barcode on this Item: the server
						// deliberately leaves Barcode blank rather than
						// guessing (data.barcode is "" here) - ask which
						// one to use instead of silently picking the
						// first row in the Item's own Barcodes table.
						const choices = data.barcodes || [];
						if (choices.length > 1) {
							barcodes_print.print_barcode_page.prompt_barcode_choice(row, choices);
						}
					},
				});
			},
		},
		{ fieldname: "item_name", fieldtype: "Data", in_list_view: 1, label: __("Item Name"), read_only: 1 },
		{ fieldname: "uom", fieldtype: "Link", options: "UOM", in_list_view: 1, label: __("UOM"), read_only: 1 },
		{ fieldname: "barcode", fieldtype: "Data", in_list_view: 1, label: __("Barcode"), read_only: 1 },
		{ fieldname: "barcode_type", fieldtype: "Data", in_list_view: 1, label: __("Barcode Type"), read_only: 1 },
		{
			fieldname: "no_of_barcodes",
			fieldtype: "Int",
			in_list_view: 1,
			label: __("No. of Barcodes"),
			reqd: 1,
			default: 1,
			onchange: function () {
				const row = this.grid_row;
				const max_allowed = cint(row.doc.max_allowed);
				if (max_allowed && cint(this.value) > max_allowed) {
					frappe.show_alert({
						message: __("Only {0} more label(s) are allowed for this line - the server will reject anything higher.", [max_allowed]),
						indicator: "orange",
					});
				}
				barcodes_print.print_barcode_page.update_total();
			},
		},
		{
			fieldname: "price_list",
			fieldtype: "Link",
			options: "Price List",
			in_list_view: 1,
			label: __("Price List"),
			default: default_price_list || "",
			onchange: function () {
				barcodes_print.print_barcode_page._refresh_row_rate(this.grid_row);
			},
		},
		{
			fieldname: "rate",
			fieldtype: "Currency",
			in_list_view: 1,
			label: __("Rate / Price"),
			read_only: 1,
			description: __("Auto-fetched from Price List. Prints as N/A if no price is found."),
		},
		// Hidden bookkeeping only, never rendered as a grid column:
		// which Purchase Order/Purchase Receipt line (if any) this row
		// came from, so a successful print can bump that line's own
		// printed-barcode counter.
		{ fieldname: "source_row_name", fieldtype: "Data", hidden: 1 },
		{ fieldname: "max_allowed", fieldtype: "Int", hidden: 1 },
	];
};

barcodes_print.print_barcode_page.render = function (page) {
	page.main.empty();

	const $wrapper = $('<div class="print-barcode-page">')
		.css({ "max-width": "1000px", margin: "0 auto", "padding-top": "10px" })
		.appendTo(page.main);

	const $source_banner = $("<div>").appendTo($wrapper);

	$(`<p class="text-muted">${__(
		"Add items below, choose a label size, and print directly to your label printer. Nothing here is saved as a document - each print is a one-time action."
	)}</p>`).appendTo($wrapper);

	const $items_area = $('<div style="margin-bottom: 10px;">').appendTo($wrapper);
	const $total_area = $('<div class="text-muted" style="margin-bottom: 20px; font-size: 13px;">').appendTo($wrapper);
	const $size_area = $('<div style="max-width: 200px;">').appendTo($wrapper);

	// The Price List column's per-row default needs Barcode Print
	// Settings' Default Price List up front, so the grid itself isn't
	// built until this one small, read-permission-light call returns.
	// Returned so callers (on_page_load) can wait for _state to exist
	// before doing anything that depends on it (e.g. apply_route_options).
	return frappe.call({
		method: "barcodes_print.api.barcode.get_print_barcode_page_defaults",
	}).then((r) => {
		const default_price_list = (r.message || {}).default_price_list || "";

		const items_control = frappe.ui.form.make_control({
			parent: $items_area,
			df: {
				fieldname: "items",
				fieldtype: "Table",
				label: __("Items"),
				options: "Print Label Item",
				in_place_edit: false,
				cannot_add_rows: false,
				fields: barcodes_print.print_barcode_page.get_item_fields(default_price_list),
			},
			render_input: true,
		});
		items_control.refresh();

		// A live total needs to react to every grid change - row added,
		// row removed, any field edited - not just the specific onchange
		// handlers below (relying only on those left the total stuck/stale
		// right after picking an Item, since that path never called it).
		// Watching the grid's own DOM directly is what actually catches all
		// of these uniformly.
		new MutationObserver(() => {
			barcodes_print.print_barcode_page.update_total();
		}).observe(items_control.grid.wrapper[0], { childList: true, subtree: true, characterData: true });

		const size_control = frappe.ui.form.make_control({
			parent: $size_area,
			df: {
				fieldname: "size",
				fieldtype: "Select",
				label: __("Label Size"),
				options: "Small\nMedium\nLarge",
				default: "Medium",
				reqd: 1,
			},
			render_input: true,
		});
		size_control.refresh();
		size_control.set_value("Medium");

		page.set_primary_action(__("Print"), () => {
			barcodes_print.print_barcode_page.print();
		}, "printer");

		barcodes_print.print_barcode_page._state = {
			page,
			items_control,
			size_control,
			default_price_list,
			$source_banner,
			$total_area,
			source_doctype: "",
			source_name: "",
		};

		barcodes_print.print_barcode_page.update_total();
	});
};

// A live running total, updated on every row add/remove/quantity change -
// the page's own "how many labels is this actually going to print" check
// before committing to the printer.
barcodes_print.print_barcode_page.update_total = function () {
	const state = barcodes_print.print_barcode_page._state;
	if (!state) return;
	const items = state.items_control.get_value() || [];
	const total = items.reduce((sum, row) => sum + (cint(row.no_of_barcodes) || 0), 0);
	state.$total_area.text(
		items.length ? __("{0} label(s) across {1} item row(s) will be printed.", [total, items.length]) : ""
	);
};

// Called on every page load/show - looks for a hand-off left by a Purchase
// Order/Purchase Receipt's own "Print Barcode" button (see
// public/js/purchase_barcode_button.js), via frappe.route_options.
barcodes_print.print_barcode_page.apply_route_options = function () {
	const options = frappe.route_options;
	if (!options || !options.barcode_source_doctype || !options.barcode_source_name) {
		return;
	}
	frappe.route_options = null;

	const source_doctype = options.barcode_source_doctype;
	const source_name = options.barcode_source_name;

	frappe.call({
		method: "barcodes_print.api.zpl.get_purchase_document_items",
		args: { source_doctype, source_name },
		freeze: true,
		freeze_message: __("Loading items from {0}...", [source_name]),
	}).then((r) => {
		const data = r.message || {};
		const state = barcodes_print.print_barcode_page._state;
		if (!state) return;

		state.source_doctype = source_doctype;
		state.source_name = source_name;

		state.$source_banner.html(`
			<div class="alert alert-info" style="margin-bottom: 15px;">
				${__("Printing barcodes for {0}", [`<a href="/app/${frappe.router.slug(source_doctype)}/${source_name}">${source_name}</a>`])}
				- ${__("each line's \"No. of Barcodes\" defaults to what's still allowed for that line (quantity + any extra your settings allow, minus what's already been printed). You can lower it, but not raise it past that limit.")}
			</div>
		`);

		// A standalone Table control's own set_value() doesn't populate
		// grid rows the way it would on a real form, and a row added
		// programmatically via the grid's own add_new_row() (as opposed
		// to a real user click on "Add row") never gets its field
		// controls lazily built, so row.get_field(...) - the mechanism
		// the "item" field's own onchange auto-fetch normally goes
		// through - throws "fieldname not found" on it. Sidestepping
		// that entirely: fetch each item's details directly via the same
		// API the onchange handler uses, and set doc fields + refresh_field
		// directly, which works regardless of whether the row has ever
		// been opened/clicked into.
		const grid = state.items_control.grid;
		(data.items || []).forEach((item_row) => {
			grid.add_new_row();
			const row = grid.grid_rows[grid.grid_rows.length - 1];
			row.doc.item = item_row.item;
			row.doc.no_of_barcodes = item_row.no_of_barcodes;
			row.doc.source_row_name = item_row.source_row_name;
			row.doc.max_allowed = item_row.max_allowed;
			row.doc.price_list = state.default_price_list || "";
			["item", "no_of_barcodes", "price_list"].forEach((f) => row.refresh_field(f));

			frappe.call({
				method: "barcodes_print.api.item.get_item_barcode_details",
				args: { item_code: item_row.item, price_list: row.doc.price_list },
				callback(r) {
					const item_data = r.message || {};
					row.doc.item_name = item_data.item_name || "";
					row.doc.uom = item_data.uom || "";
					row.doc.barcode = item_data.barcode || "";
					row.doc.barcode_type = item_data.barcode_type || "";
					row.doc.rate = item_data.rate;
					["item_name", "uom", "barcode", "barcode_type", "rate"].forEach((f) => row.refresh_field(f));

					const choices = item_data.barcodes || [];
					if (choices.length > 1) {
						barcodes_print.print_barcode_page.prompt_barcode_choice(row, choices);
					}
				},
			});
		});
		barcodes_print.print_barcode_page.update_total();
	});
};

barcodes_print.print_barcode_page.print = function () {
	const state = barcodes_print.print_barcode_page._state;
	const items = state.items_control.get_value();
	const size = state.size_control.get_value();

	if (!(items || []).length) {
		frappe.msgprint(__("Please add at least one item row."));
		return;
	}

	frappe
		.call({
			method: "barcodes_print.api.zpl.get_quick_print_job",
			args: {
				items,
				size,
				source_doctype: state.source_doctype || "",
				source_name: state.source_name || "",
			},
			freeze: true,
			freeze_message: __("Preparing labels..."),
		})
		.then((r) => {
			const job = r.message;
			if (!job || !job.zpl) {
				frappe.msgprint(__("Nothing to print."));
				return;
			}

			const connector =
				job.connector === "Zebra Browser Print" ? barcodes_print.zebra : barcodes_print.qz;

			const finish_log = (status, failure_reason) =>
				frappe.call({
					method: "barcodes_print.api.zpl.log_print",
					args: {
						items: job.resolved_items,
						size,
						status,
						failure_reason: failure_reason || "",
						source_doctype: state.source_doctype || "",
						source_name: state.source_name || "",
					},
				});

			connector
				.print(job)
				.then(() => {
					frappe.show_alert({
						message: __("Sent {0} label(s) to the printer.", [job.total_labels]),
						indicator: "green",
					});
					finish_log("Success");
					// Clear the grid so the page is ready for the next
					// one-time print, without navigating away.
					state.items_control.grid.grid_rows.slice().forEach((row) => row.remove());
					state.source_doctype = "";
					state.source_name = "";
					state.$source_banner.empty();
					barcodes_print.print_barcode_page.update_total();
				})
				.catch((err) => {
					const message =
						(err && err.message) ||
						__("Could not send the print job. Please check the printer connection and try again.");
					finish_log("Failed", message);
					frappe.msgprint({
						title: __("Print Failed"),
						indicator: "red",
						message,
					});
				});
		});
};
