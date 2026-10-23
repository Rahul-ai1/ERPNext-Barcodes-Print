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

// Shown automatically right after picking a serial-tracked Item (and again
// if Batch No changes on one), so the user can pick exactly which physical
// units this print run covers - one label prints per Serial No selected
// here, each encoding that exact Serial No so scanning it anywhere in
// ERPNext later resolves Item + Batch automatically (see api/zpl.py
// resolve_row). Cancelling just leaves serial_nos as it was; there's no
// forced choice the way there is for an ambiguous plain barcode.
barcodes_print.print_barcode_page.prompt_serial_no_choice = function (row, item_code, batch_no) {
	frappe.call({
		method: "barcodes_print.api.item.get_available_serial_nos",
		args: { item_code, batch_no: batch_no || "" },
	}).then((r) => {
		const available = r.message || [];
		if (!available.length) {
			frappe.show_alert({
				message: __("No available Serial Nos found for {0}.", [item_code]),
				indicator: "orange",
			});
			return;
		}

		const already_selected = new Set((row.doc.serial_nos || "").split("\n").filter(Boolean));
		const dialog = new frappe.ui.Dialog({
			title: __("Select Serial Nos"),
			fields: [
				{
					fieldname: "serials",
					fieldtype: "MultiCheck",
					label: __("Select which units this print run is for"),
					options: available.map((s) => ({
						label: s.batch_no ? `${s.name} (${s.batch_no})` : s.name,
						value: s.name,
						checked: already_selected.has(s.name),
					})),
					columns: 2,
				},
			],
			primary_action_label: __("Use Selected"),
			primary_action(values) {
				const selected = values.serials || [];
				if (!selected.length) {
					frappe.msgprint(__("Select at least one Serial No, or close this dialog to leave it unchanged."));
					return;
				}
				row.doc.serial_nos = selected.join("\n");
				row.refresh_field("serial_nos");
				dialog.hide();
			},
		});
		dialog.show();
	});
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

// On-hand quantity for the row's item, or for its Batch when one is set.
// A non-stock item comes back blank.
barcodes_print.print_barcode_page._refresh_row_stock = function (row) {
	if (!row || !row.doc) return;
	const item_code = row.doc.item;
	if (!item_code) {
		row.doc.stock_qty = null;
		row.refresh_field("stock_qty");
		return;
	}
	frappe.call({
		method: "barcodes_print.api.item.get_item_barcode_details",
		args: { item_code, batch_no: row.doc.batch_no || "" },
		callback(r) {
			row.doc.stock_qty = (r.message || {}).stock_qty;
			row.refresh_field("stock_qty");
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
				// A loaded Purchase Invoice or spreadsheet row stamps the
				// batch and serials before this runs. Keep them while the
				// item is still that loaded item. A later item change drops them.
				const keep_tracking =
					!!row.doc._keep_batch_serial && row.doc._loaded_item === item_code;
				if (!item_code) {
					["item_name", "uom", "barcode", "barcode_type", "batch_no", "serial_nos"].forEach((f) => {
						row.doc[f] = "";
						row.refresh_field(f);
					});
					row.doc.rate = null;
					row.doc.stock_qty = null;
					row.refresh_field("rate");
					row.refresh_field("stock_qty");
					row.doc._has_batch_no = 0;
					row.doc._has_serial_no = 0;
					row.doc._keep_batch_serial = 0;
					row.doc._loaded_item = "";
					barcodes_print.print_barcode_page.update_total();
					return;
				}
				const kept_batch = keep_tracking ? row.doc.batch_no || "" : "";
				const kept_serials = keep_tracking ? row.doc.serial_nos || "" : "";
				if (!keep_tracking) {
					row.doc._keep_batch_serial = 0;
				}
				row.doc._loaded_item = item_code;
				frappe.call({
					method: "barcodes_print.api.item.get_item_barcode_details",
					args: { item_code, price_list: row.doc.price_list || "", batch_no: kept_batch },
					callback(r) {
						const data = r.message || {};
						row.doc.item_name = data.item_name || "";
						row.doc.uom = data.uom || "";
						row.doc.barcode = data.barcode || "";
						row.doc.barcode_type = data.barcode_type || "";
						row.doc.rate = data.rate;
						row.doc.stock_qty = data.stock_qty;
						row.doc.batch_no = kept_batch;
						row.doc.serial_nos = kept_serials;
						row.doc._has_batch_no = data.has_batch_no;
						row.doc._has_serial_no = data.has_serial_no;
						["item_name", "uom", "barcode", "barcode_type", "rate", "stock_qty", "batch_no", "serial_nos"].forEach(
							(f) => row.refresh_field(f)
						);

						// More than one barcode on this Item: the server
						// deliberately leaves Barcode blank rather than
						// guessing (data.barcode is "" here) - ask which
						// one to use instead of silently picking the
						// first row in the Item's own Barcodes table.
						const choices = data.barcodes || [];
						if (choices.length > 1) {
							barcodes_print.print_barcode_page.prompt_barcode_choice(row, choices);
						}

						// Serial-tracked: ask which physical units are being
						// labelled right away, same as the barcode-choice
						// prompt above - Batch No is picked separately below
						// (a Link field of its own) if the item only tracks
						// batches, not individual serials. A loaded invoice
						// row already has its serials.
						if (data.has_serial_no && !kept_serials) {
							barcodes_print.print_barcode_page.prompt_serial_no_choice(row, item_code, kept_batch);
						}
					},
				});
			},
		},
		{ fieldname: "item_name", fieldtype: "Data", in_list_view: 1, label: __("Item Name"), read_only: 1 },
		{ fieldname: "uom", fieldtype: "Link", options: "UOM", in_list_view: 1, label: __("UOM"), read_only: 1 },
		{
			fieldname: "stock_qty",
			fieldtype: "Float",
			in_list_view: 1,
			label: __("Stock Balance"),
			read_only: 1,
			description: __("Actual quantity on hand. A Batch No shows that batch's quantity."),
		},
		{ fieldname: "barcode", fieldtype: "Data", in_list_view: 1, label: __("Barcode"), read_only: 1 },
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
		{ fieldname: "barcode_type", fieldtype: "Data", in_list_view: 1, label: __("Barcode Type"), read_only: 1 },
		{
			fieldname: "batch_no",
			fieldtype: "Link",
			options: "Batch",
			in_list_view: 1,
			label: __("Batch No"),
			get_query: function (doc) {
				return { filters: { item: doc.item, disabled: 0 } };
			},
			onchange: function () {
				const row = this.grid_row;
				barcodes_print.print_barcode_page._refresh_row_stock(row);
				if (row.doc._has_serial_no && row.doc.item) {
					barcodes_print.print_barcode_page.prompt_serial_no_choice(row, row.doc.item, this.value || "");
				}
			},
		},
		{
			fieldname: "serial_nos",
			fieldtype: "Small Text",
			in_list_view: 1,
			label: __("Serial Nos"),
			read_only: 1,
		},
		// Hidden bookkeeping only, never rendered as a grid column:
		// which Purchase Order/Purchase Receipt line (if any) this row
		// came from, so a successful print can bump that line's own
		// printed-barcode counter.
		{ fieldname: "source_row_name", fieldtype: "Data", hidden: 1 },
		{ fieldname: "max_allowed", fieldtype: "Int", hidden: 1 },
	];
};

// Sales Invoice shows a settings gear on the item-grid header. That gear is
// drawn only when the header row has a form. This page has no document, so
// the form is present just long enough to build the header, then removed.
// Leaving it on the grid would make Frappe read rows from a parent document.
barcodes_print.print_barcode_page.attach_column_config = function (grid) {
	const settings_doctype = "Print Label Item";
	const header_frm = {
		doctype: settings_doctype,
		meta: {},
		get_perm() {
			return 1;
		},
	};

	grid.setup_user_defined_columns = function () {
		const saved = (frappe.get_user_settings(settings_doctype, "GridView") || {})[this.doctype];
		if (!saved || !saved.length) {
			this.user_defined_columns = [];
			return;
		}
		this.user_defined_columns = saved
			.map((row) => {
				const source = (this.docfields || []).find((df) => df.fieldname === row.fieldname);
				if (!source || source.hidden) return null;
				const column = Object.assign({}, source);
				column.in_list_view = 1;
				column.columns = row.columns;
				column.sticky = row.sticky;
				return column;
			})
			.filter(Boolean);
	};

	const make_head = grid.make_head;
	grid.make_head = function () {
		const saved_frm = this.frm;
		this.frm = header_frm;
		try {
			make_head.call(this);
		} finally {
			this.frm = saved_frm;
		}
		barcodes_print.print_barcode_page._bind_column_gear(this);
	};

	const saved = (frappe.get_user_settings(settings_doctype, "GridView") || {})[grid.doctype];
	if (saved && saved.length) {
		grid.visible_columns = [];
		grid.refresh();
		return;
	}
	grid.make_head();
};

// The gear's own handler reads field definitions from DocType meta. On this
// page that meta is not loaded yet, so the click throws before the dialog
// can open. Load it first, and keep the page's own fields as a fallback.
barcodes_print.print_barcode_page._bind_column_gear = function (grid) {
	const header = grid.header_row;
	const button = header && header.configure_columns_button;
	if (!button || button.data("print-barcode-gear")) return;
	button.data("print-barcode-gear", 1);
	button.off("click").on("click", (event) => {
		event.preventDefault();
		event.stopPropagation();
		const open_dialog = () => {
			barcodes_print.print_barcode_page._register_grid_fields(grid);
			header.configure_dialog_for_columns_selector();
		};
		if (frappe.get_meta(grid.doctype)) {
			open_dialog();
			return;
		}
		frappe.model.with_doctype(grid.doctype, open_dialog);
	});
};

barcodes_print.print_barcode_page._register_grid_fields = function (grid) {
	const doctype = grid.doctype;
	if (!doctype) return;
	frappe.meta.docfield_map[doctype] = frappe.meta.docfield_map[doctype] || {};
	(grid.docfields || []).forEach((df) => {
		if (!df || !df.fieldname || frappe.meta.docfield_map[doctype][df.fieldname]) return;
		frappe.meta.docfield_map[doctype][df.fieldname] = Object.assign({ parent: doctype }, df);
	});
};

// A handheld scanner types the code and sends Enter. Queue scans so a
// second beep that arrives before the first lookup returns is not dropped.
barcodes_print.print_barcode_page.add_scanned_item = function (search_value) {
	const state = barcodes_print.print_barcode_page._state;
	if (!state) return;
	state._scan_queue = state._scan_queue || [];
	state._scan_queue.push(search_value);
	barcodes_print.print_barcode_page._drain_scan_queue();
};

barcodes_print.print_barcode_page._drain_scan_queue = function () {
	const state = barcodes_print.print_barcode_page._state;
	if (!state || state._scan_busy) return;
	const search_value = (state._scan_queue || []).shift();
	if (!search_value) return;

	state._scan_busy = true;
	frappe
		.call({
			method: "barcodes_print.api.item.resolve_scan",
			args: {
				search_value,
				price_list: state.default_price_list || "",
			},
		})
		.then((r) => {
			const data = r.message || {};
			if (!data.item) {
				frappe.show_alert({
					message: __("No item found for {0}.", [search_value]),
					indicator: "red",
				});
				return;
			}
			const prompted = barcodes_print.print_barcode_page._apply_scan(data);
			if (!prompted && state.scan_control) {
				state.scan_control.$input.focus();
			}
		})
		.then(release_scan, release_scan);

	function release_scan() {
		state._scan_busy = false;
		barcodes_print.print_barcode_page._drain_scan_queue();
	}
};

barcodes_print.print_barcode_page._apply_scan = function (data) {
	const state = barcodes_print.print_barcode_page._state;
	const grid = state.items_control.grid;

	if (data.serial_no && barcodes_print.print_barcode_page._serial_already_listed(data.serial_no)) {
		frappe.show_alert({
			message: __("{0} is already in the list.", [data.serial_no]),
			indicator: "orange",
		});
		return false;
	}

	const existing = barcodes_print.print_barcode_page._find_scan_row(data);
	if (existing) {
		if (data.serial_no) {
			const serials = (existing.doc.serial_nos || "").split("\n").filter(Boolean);
			serials.push(data.serial_no);
			existing.doc.serial_nos = serials.join("\n");
			existing.refresh_field("serial_nos");
		} else if (!barcodes_print.print_barcode_page._bump_scan_qty(existing)) {
			return false;
		}
		barcodes_print.print_barcode_page.update_total();
		frappe.show_alert({
			message: __("Updated {0}.", [data.item_name || data.item]),
			indicator: "green",
		});
		return false;
	}

	const row = barcodes_print.print_barcode_page._fill_scan_row(grid, data);
	barcodes_print.print_barcode_page.update_total();
	frappe.show_alert({
		message: __("Added {0}.", [data.item_name || data.item]),
		indicator: "green",
	});

	const choices = data.barcodes || [];
	let prompted = false;
	if (!data.barcode && choices.length > 1) {
		barcodes_print.print_barcode_page.prompt_barcode_choice(row, choices);
		prompted = true;
	}
	if (data.has_serial_no && !data.serial_no) {
		barcodes_print.print_barcode_page.prompt_serial_no_choice(row, data.item, data.batch_no || "");
		prompted = true;
	}
	return prompted;
};

barcodes_print.print_barcode_page._find_scan_row = function (data) {
	const grid = barcodes_print.print_barcode_page._state.items_control.grid;
	return (grid.grid_rows || []).find((row) => {
		if ((row.doc.item || "") !== data.item) return false;
		if ((row.doc.batch_no || "") !== (data.batch_no || "")) return false;
		if (data.serial_no) return true;
		return (row.doc.barcode || "") === (data.barcode || "");
	});
};

barcodes_print.print_barcode_page._serial_already_listed = function (serial_no) {
	const grid = barcodes_print.print_barcode_page._state.items_control.grid;
	return (grid.grid_rows || []).some((row) =>
		(row.doc.serial_nos || "").split("\n").filter(Boolean).includes(serial_no)
	);
};

barcodes_print.print_barcode_page._bump_scan_qty = function (row) {
	const max_allowed = cint(row.doc.max_allowed);
	const next = cint(row.doc.no_of_barcodes) + 1;
	if (max_allowed && next > max_allowed) {
		frappe.show_alert({
			message: __("This line cannot have more than {0} label(s).", [max_allowed]),
			indicator: "orange",
		});
		return false;
	}
	row.doc.no_of_barcodes = next;
	row.refresh_field("no_of_barcodes");
	return true;
};

barcodes_print.print_barcode_page._fill_scan_row = function (grid, data) {
	const state = barcodes_print.print_barcode_page._state;
	grid.add_new_row();
	const row = grid.grid_rows[grid.grid_rows.length - 1];
	row.doc.item = data.item;
	row.doc.item_name = data.item_name || "";
	row.doc.uom = data.uom || "";
	row.doc.barcode = data.barcode || "";
	row.doc.barcode_type = data.barcode_type || "";
	row.doc.rate = data.rate;
	row.doc.stock_qty = data.stock_qty;
	row.doc.batch_no = data.batch_no || "";
	row.doc.serial_nos = data.serial_no || "";
	row.doc.no_of_barcodes = 1;
	row.doc.price_list = state.default_price_list || "";
	row.doc._has_batch_no = data.has_batch_no;
	row.doc._has_serial_no = data.has_serial_no;
	[
		"item",
		"item_name",
		"uom",
		"barcode",
		"barcode_type",
		"rate",
		"stock_qty",
		"batch_no",
		"serial_nos",
		"no_of_barcodes",
		"price_list",
	].forEach((fieldname) => row.refresh_field(fieldname));
	return row;
};

barcodes_print.print_barcode_page.render = function (page) {
	page.main.empty();

	const $wrapper = $('<div class="print-barcode-page">')
		.css({ "max-width": "1000px", margin: "0 auto", "padding-top": "10px" })
		.appendTo(page.main);

	const $source_banner = $("<div>").appendTo($wrapper);

	$(`<p class="text-muted">${__(
		"Scan a barcode, serial, batch, or item code, add rows below, load a submitted Purchase Invoice, or load an Excel or CSV file. Choose a label size and how many columns, then print directly to your label printer. Label Only sizes print the same label with no barcode. Nothing here is saved as a document - each print is a one-time action."
	)}</p>`).appendTo($wrapper);

	// In the page body, not only the toolbar. Frappe hides toolbar custom
	// buttons below the large desktop width, so Print stays visible while
	// this button would not.
	const $load_area = $('<div style="margin-bottom: 15px;">').appendTo($wrapper);
	$(`<button type="button" class="btn btn-default btn-sm"></button>`)
		.text(__("Load from Purchase Invoice"))
		.appendTo($load_area)
		.on("click", () => barcodes_print.print_barcode_page.load_from_purchase_invoice());
	$(`<button type="button" class="btn btn-default btn-sm" style="margin-left: 8px;"></button>`)
		.text(__("Load from Excel"))
		.appendTo($load_area)
		.on("click", () => barcodes_print.print_barcode_page.load_from_excel());

	const $scan_area = $('<div class="print-barcode-scan" style="margin-bottom: 15px; max-width: 480px;">').appendTo(
		$wrapper
	);
	const $items_area = $('<div style="margin-bottom: 10px;">').appendTo($wrapper);
	const $total_area = $('<div class="text-muted" style="margin-bottom: 20px; font-size: 13px;">').appendTo($wrapper);
	const $options_row = $('<div class="row">').appendTo($wrapper);
	const $size_area = $('<div class="col-sm-4">').appendTo($options_row);
	const $columns_area = $('<div class="col-sm-4">').appendTo($options_row);
	const $gap_area = $('<div class="col-sm-4">').appendTo($options_row);

	// The Price List column's per-row default needs Barcode Print
	// Settings' Default Price List up front, so the grid itself isn't
	// built until this one small, read-permission-light call returns.
	// Returned so callers (on_page_load) can wait for _state to exist
	// before doing anything that depends on it (e.g. apply_route_options).
	// Column choices are stored on Print Label Item user settings so the
	// grid can apply them on this first build.
	return Promise.all([
		frappe.call({
			method: "barcodes_print.api.barcode.get_print_barcode_page_defaults",
		}),
		frappe.model.user_settings.get("Print Label Item").catch(() => ({})),
	]).then(([r, column_settings]) => {
		frappe.model.user_settings["Print Label Item"] = column_settings || {};
		const defaults = r.message || {};
		const default_price_list = defaults.default_price_list || "";
		const default_columns = String(cint(defaults.number_of_columns) || 1);
		const default_gap = flt(defaults.column_gap_mm) || 0;

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
		barcodes_print.print_barcode_page.attach_column_config(items_control.grid);

		const submit_scan = () => {
			const value = cstr(scan_control.get_value()).trim();
			// Clear the input directly. set_value() would run this field's
			// onchange again and can wipe the next scan if it resolves late.
			scan_control.$input.val("");
			if (!value) {
				return;
			}
			barcodes_print.print_barcode_page.add_scanned_item(value);
		};
		const scan_control = frappe.ui.form.make_control({
			parent: $scan_area,
			df: {
				fieldname: "scan_barcode",
				fieldtype: "Data",
				options: "Barcode",
				label: __("Scan Item"),
				placeholder: __("Barcode, serial, batch, or item code"),
				description: __("Scan and press Enter, or use the scan icon. Scanning the same item again adds another label."),
				onchange: submit_scan,
			},
			render_input: true,
		});
		scan_control.refresh();
		// Data fields also run onchange after a pause in typing. That would
		// look up a half-entered code. Enter and the camera scanner are enough.
		scan_control.change = () => {};
		scan_control.$input.on("keydown", (e) => {
			if (e.key !== "Enter") {
				return;
			}
			e.preventDefault();
			submit_scan();
		});
		scan_control.$input.focus();

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
				options: "Small\nMedium\nLarge\nSmall Label Only\nMedium Label Only\nLarge Label Only",
				default: "Medium",
				reqd: 1,
				description: __("Label Only prints that size with no barcode."),
			},
			render_input: true,
		});
		size_control.refresh();
		size_control.set_value("Medium");

		const columns_control = frappe.ui.form.make_control({
			parent: $columns_area,
			df: {
				fieldname: "number_of_columns",
				fieldtype: "Select",
				label: __("Number of Columns"),
				options: "1\n2\n3\n4",
				default: default_columns,
				reqd: 1,
				description: __("2 prints two labels side by side on each row."),
			},
			render_input: true,
		});
		columns_control.refresh();
		columns_control.set_value(default_columns);

		const gap_control = frappe.ui.form.make_control({
			parent: $gap_area,
			df: {
				fieldname: "column_gap_mm",
				fieldtype: "Float",
				label: __("Gap Between Columns (mm)"),
				default: default_gap,
				description: __("Space between labels in a row. Used when columns is more than 1."),
			},
			render_input: true,
		});
		gap_control.refresh();
		gap_control.set_value(default_gap);

		const load_button = page.add_button(__("Load from Purchase Invoice"), () => {
			barcodes_print.print_barcode_page.load_from_purchase_invoice();
		});
		load_button.closest(".custom-actions").removeClass("hidden-xs hidden-md");
		const excel_button = page.add_button(__("Load from Excel"), () => {
			barcodes_print.print_barcode_page.load_from_excel();
		});
		excel_button.closest(".custom-actions").removeClass("hidden-xs hidden-md");
		page.set_primary_action(__("Print"), () => {
			barcodes_print.print_barcode_page.print();
		}, "printer");
		page.set_secondary_action(__("Save to File"), () => {
			barcodes_print.print_barcode_page.print("file");
		}, "download");

		barcodes_print.print_barcode_page._state = {
			page,
			items_control,
			scan_control,
			size_control,
			columns_control,
			gap_control,
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
	// A row with Serial Nos selected prints exactly one label per serial
	// (see api/zpl.py get_quick_print_job's row expansion), not
	// no_of_barcodes - matching that here so the running total is accurate
	// before the user ever clicks Print.
	const row_count = (row) => {
		const serials = (row.serial_nos || "").split("\n").filter(Boolean);
		return serials.length || cint(row.no_of_barcodes) || 0;
	};
	const total = items.reduce((sum, row) => sum + row_count(row), 0);
	state.$total_area.text(
		items.length ? __("{0} label(s) across {1} item row(s) will be printed.", [total, items.length]) : ""
	);
};

// Toolbar button. Asks for a submitted Purchase Invoice, then replaces the
// grid with that invoice's stock lines. This is a one-time fill: it does
// not attach the invoice as a purchase-document source, so the reprint
// cap used by Purchase Order / Purchase Receipt does not apply.
barcodes_print.print_barcode_page.load_from_purchase_invoice = function () {
	const state = barcodes_print.print_barcode_page._state;
	if (!state) return;

	const open_dialog = () => {
		const dialog = new frappe.ui.Dialog({
			title: __("Load from Purchase Invoice"),
			fields: [
				{
					fieldname: "purchase_invoice",
					fieldtype: "Link",
					options: "Purchase Invoice",
					label: __("Purchase Invoice"),
					reqd: 1,
					get_query() {
						return { filters: { docstatus: 1 } };
					},
				},
			],
			primary_action_label: __("Load"),
			primary_action(values) {
				dialog.hide();
				barcodes_print.print_barcode_page.fetch_purchase_invoice(values.purchase_invoice);
			},
		});
		dialog.show();
	};

	const existing = state.items_control.get_value() || [];
	if (existing.length) {
		frappe.confirm(__("This replaces the items already on the page."), open_dialog);
		return;
	}
	open_dialog();
};

barcodes_print.print_barcode_page.fetch_purchase_invoice = function (purchase_invoice) {
	frappe.call({
		method: "barcodes_print.api.zpl.get_purchase_invoice_items",
		args: { purchase_invoice },
		freeze: true,
		freeze_message: __("Loading items from {0}...", [purchase_invoice]),
	}).then((r) => {
		const data = r.message || {};
		barcodes_print.print_barcode_page.apply_loaded_items(
			data.items,
			`<div class="alert alert-info" style="margin-bottom: 15px;">
				${__("Loaded items from {0}. Each line's label count follows its quantity. Service items are left out.", [
					`<a href="/app/purchase-invoice/${data.source_name}">${data.source_name}</a>`,
				])}
			</div>`
		);
	});
};

// Same one-time fill as Purchase Invoice. The file is not kept as a
// purchase source, so the reprint cap does not apply.
barcodes_print.print_barcode_page.load_from_excel = function () {
	const state = barcodes_print.print_barcode_page._state;
	if (!state) return;

	const open_dialog = () => {
		const dialog = new frappe.ui.Dialog({
			title: __("Load from Excel"),
			fields: [
				{
					fieldname: "file",
					fieldtype: "Attach",
					label: __("Excel or CSV File"),
					reqd: 1,
				},
				{
					fieldtype: "HTML",
					fieldname: "help",
					options: `<div class="text-muted" style="margin-top: 8px;">
						${__("The first row is the header. Include Item Code or Barcode. Qty defaults to 1. Batch No and Serial Nos are optional. Keep codes as text so leading zeros stay.")}
					</div>`,
				},
			],
			primary_action_label: __("Load"),
			primary_action(values) {
				dialog.hide();
				barcodes_print.print_barcode_page.fetch_excel(values.file);
			},
		});
		dialog.show();
	};

	const existing = state.items_control.get_value() || [];
	if (existing.length) {
		frappe.confirm(__("This replaces the items already on the page."), open_dialog);
		return;
	}
	open_dialog();
};

barcodes_print.print_barcode_page.fetch_excel = function (file_url) {
	frappe.call({
		method: "barcodes_print.api.item.get_items_from_spreadsheet",
		args: { file_url },
		freeze: true,
		freeze_message: __("Loading items from the spreadsheet..."),
	}).then((r) => {
		const data = r.message || {};
		const loaded = (data.items || []).length;
		barcodes_print.print_barcode_page.apply_loaded_items(
			data.items,
			`<div class="alert alert-info" style="margin-bottom: 15px;">
				${__("Loaded {0} item row(s) from the spreadsheet. Qty is the label count. A blank Qty is 1.", [loaded])}
			</div>`
		);
		barcodes_print.print_barcode_page.show_skipped_rows(data);
	});
};

barcodes_print.print_barcode_page.show_skipped_rows = function (data) {
	const skipped = data.skipped || [];
	if (!skipped.length) return;
	const extra = Math.max(0, cint(data.skipped_count) - skipped.length);
	let message = skipped.map((row) => __("Row {0}: {1}", [row.row, row.message])).join("<br>");
	if (extra) {
		message += `<br>${__("{0} more row(s) were skipped.", [extra])}`;
	}
	frappe.msgprint({
		title: __("Some rows were skipped"),
		message,
		indicator: "orange",
	});
};

barcodes_print.print_barcode_page.apply_loaded_items = function (items, banner_html) {
	const state = barcodes_print.print_barcode_page._state;
	if (!state) return;

	state.items_control.grid.grid_rows.slice().forEach((row) => row.remove());
	state.source_doctype = "";
	state.source_name = "";
	state.$source_banner.html(banner_html);

	const grid = state.items_control.grid;
	(items || []).forEach((item_row) => {
		grid.add_new_row();
		const row = grid.grid_rows[grid.grid_rows.length - 1];
		const batch_no = item_row.batch_no || "";
		const serial_nos = item_row.serial_nos || "";
		const requested_barcode = item_row.barcode || "";
		row.doc.item = item_row.item;
		row.doc.no_of_barcodes = item_row.no_of_barcodes;
		row.doc.batch_no = batch_no;
		row.doc.serial_nos = serial_nos;
		row.doc.price_list = state.default_price_list || "";
		row.doc.source_row_name = "";
		row.doc.max_allowed = 0;
		row.doc._keep_batch_serial = batch_no || serial_nos ? 1 : 0;
		row.doc._loaded_item = item_row.item;
		["item", "no_of_barcodes", "batch_no", "serial_nos", "price_list"].forEach((f) => row.refresh_field(f));

		frappe.call({
			method: "barcodes_print.api.item.get_item_barcode_details",
			args: {
				item_code: item_row.item,
				barcode: requested_barcode,
				price_list: row.doc.price_list,
				batch_no,
			},
			callback(details_response) {
				const item_data = details_response.message || {};
				const choices = item_data.barcodes || [];
				const matched = requested_barcode
					? choices.find((choice) => choice.barcode === requested_barcode)
					: null;
				row.doc.item_name = item_data.item_name || "";
				row.doc.uom = item_data.uom || "";
				row.doc.barcode = matched ? matched.barcode : item_data.barcode || "";
				row.doc.barcode_type = matched ? matched.barcode_type || "" : item_data.barcode_type || "";
				row.doc.rate = item_data.rate;
				row.doc.stock_qty = item_data.stock_qty;
				row.doc.batch_no = batch_no;
				row.doc.serial_nos = serial_nos;
				["item_name", "uom", "barcode", "barcode_type", "rate", "stock_qty", "batch_no", "serial_nos"].forEach(
					(f) => row.refresh_field(f)
				);

				if (!matched && choices.length > 1) {
					barcodes_print.print_barcode_page.prompt_barcode_choice(row, choices);
				}
			},
		});
	});
	barcodes_print.print_barcode_page.update_total();
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
					row.doc.stock_qty = item_data.stock_qty;
					["item_name", "uom", "barcode", "barcode_type", "rate", "stock_qty"].forEach((f) =>
						row.refresh_field(f)
					);

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

// Printer-less path: the same ZPL the printer would receive, saved as a
// .zpl file so it can be sent later from a machine that has the printer.
barcodes_print.file = {
	print(job) {
		const blob = new Blob([job.zpl], { type: "text/plain;charset=utf-8" });
		const url = URL.createObjectURL(blob);
		const link = document.createElement("a");
		const stamp = (frappe.datetime.now_datetime() || "labels").replace(/[: ]/g, "-");
		link.href = url;
		link.download = `barcode-labels-${stamp}.zpl`;
		document.body.appendChild(link);
		link.click();
		link.remove();
		URL.revokeObjectURL(url);
		return Promise.resolve();
	},
};

barcodes_print.print_barcode_page.print = function (destination) {
	const state = barcodes_print.print_barcode_page._state;
	const items = state.items_control.get_value();
	const selected = state.size_control.$input && state.size_control.$input.find("option:selected");
	const size = cstr((selected && selected.attr("value")) || state.size_control.get_value()).trim();

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
				number_of_columns: state.columns_control.get_value(),
				column_gap_mm: state.gap_control.get_value() || 0,
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

			const save_to_file = destination === "file" || job.connector === "File";
			const connector = save_to_file
				? barcodes_print.file
				: job.connector === "Zebra Browser Print"
					? barcodes_print.zebra
					: barcodes_print.qz;

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
						message: save_to_file
							? __("Saved {0} label(s) to a ZPL file.", [job.total_labels])
							: __("Sent {0} label(s) to the printer.", [job.total_labels]),
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
