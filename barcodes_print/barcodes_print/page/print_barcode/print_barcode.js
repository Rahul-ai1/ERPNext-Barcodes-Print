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
		args: { item_code, price_list, tax_template: row.doc.tax_template || "" },
		callback(r) {
			row.doc.rate = (r.message || {}).rate;
			row.refresh_field("rate");
		},
	});
};

// Informational only, never printed on a label - a live on-hand figure so
// the user has stock context before deciding how many to print. A
// separate call from get_item_barcode_details on purpose: this is live
// stock status, not part of what actually prints.
barcodes_print.print_barcode_page._refresh_row_stock_qty = function (row, batch_no) {
	const item_code = row.doc.item;
	if (!item_code) {
		row.doc.stock_qty = null;
		row.refresh_field("stock_qty");
		return;
	}
	frappe.call({
		method: "barcodes_print.api.item.get_item_stock_qty",
		args: { item_code, batch_no: batch_no || "" },
		callback(r) {
			row.doc.stock_qty = (r.message || {}).stock_qty;
			row.refresh_field("stock_qty");
		},
	});
};

barcodes_print.print_barcode_page.get_item_fields = function (default_price_list, batch_serial_mode) {
	// Batch/Serial Printing (site-wide, in Settings) - when a type is off
	// there, its column/picker doesn't appear here at all, rather than
	// appearing but never doing anything.
	const batch_enabled = batch_serial_mode !== "Serial Only" && batch_serial_mode !== "Neither";
	const serial_enabled = batch_serial_mode !== "Batch Only" && batch_serial_mode !== "Neither";

	// Only ever touch fields that actually exist as grid columns - a
	// disabled type's field isn't just hidden, it's left out of the field
	// list entirely, so refresh_field("batch_no")/("serial_nos") would
	// throw if called unconditionally below.
	const batch_serial_fieldnames = [
		...(batch_enabled ? ["batch_no"] : []),
		...(serial_enabled ? ["serial_nos"] : []),
	];

	const fields = [
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
					["item_name", "uom", "barcode", "barcode_type", ...batch_serial_fieldnames].forEach((f) => {
						row.doc[f] = "";
						row.refresh_field(f);
					});
					row.doc.rate = null;
					row.refresh_field("rate");
					row.doc.stock_qty = null;
					row.refresh_field("stock_qty");
					row.doc._has_batch_no = 0;
					row.doc._has_serial_no = 0;
					barcodes_print.print_barcode_page.update_total();
					return;
				}
				frappe.call({
					method: "barcodes_print.api.item.get_item_barcode_details",
					args: {
						item_code,
						price_list: row.doc.price_list || "",
						tax_template: row.doc.tax_template || "",
					},
					callback(r) {
						const data = r.message || {};
						row.doc.item_name = data.item_name || "";
						row.doc.uom = data.uom || "";
						row.doc.barcode = data.barcode || "";
						row.doc.barcode_type = data.barcode_type || "";
						row.doc.rate = data.rate;
						// A different Item invalidates any Batch No/Serial
						// Nos picked for the previous one.
						if (batch_enabled) row.doc.batch_no = "";
						if (serial_enabled) row.doc.serial_nos = "";
						row.doc._has_batch_no = data.has_batch_no;
						row.doc._has_serial_no = data.has_serial_no;
						["item_name", "uom", "barcode", "barcode_type", "rate", ...batch_serial_fieldnames].forEach(
							(f) => row.refresh_field(f)
						);
						// Batch No was just cleared above for this new Item, so
						// Stock Qty starts as the Item's overall on-hand figure
						// - the Batch No onchange handler below re-scopes it.
						barcodes_print.print_barcode_page._refresh_row_stock_qty(row, "");

						// More than one barcode on this Item: the server
						// deliberately leaves Barcode blank rather than
						// guessing (data.barcode is "" here) - ask which
						// one to use instead of silently picking the
						// first row in the Item's own Barcodes table.
						// If exactly one row is marked Default, the server
						// already resolved data.barcode above - no prompt,
						// but the dropdown below still lets the user pick
						// a different one for this print.
						const choices = data.barcodes || [];
						if (choices.length > 1 && !data.barcode) {
							barcodes_print.print_barcode_page.prompt_barcode_choice(row, choices);
						}

						// Serial-tracked: ask which physical units are being
						// labelled right away, same as the barcode-choice
						// prompt above - Batch No is picked separately below
						// (a Link field of its own) if the item only tracks
						// batches, not individual serials. Already gated
						// server-side by Batch/Serial Printing - data.has_serial_no
						// is 0 whenever that's off, regardless of the Item's
						// own tracking.
						if (data.has_serial_no) {
							barcodes_print.print_barcode_page.prompt_serial_no_choice(row, item_code, "");
						}
					},
				});
			},
		},
		{ fieldname: "item_name", fieldtype: "Data", in_list_view: 1, label: __("Item Name"), read_only: 1 },
		{ fieldname: "uom", fieldtype: "Link", options: "UOM", in_list_view: 1, label: __("UOM"), read_only: 1 },
		{ fieldname: "barcode", fieldtype: "Data", in_list_view: 1, label: __("Barcode"), read_only: 1 },
		{ fieldname: "barcode_type", fieldtype: "Data", in_list_view: 1, label: __("Barcode Type"), read_only: 1 },
	];

	if (batch_enabled) {
		fields.push({
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
				if (row.doc._has_serial_no && row.doc.item) {
					barcodes_print.print_barcode_page.prompt_serial_no_choice(row, row.doc.item, this.value || "");
				}
				barcodes_print.print_barcode_page._refresh_row_stock_qty(row, this.value || "");
			},
		});
	}
	if (serial_enabled) {
		fields.push({
			fieldname: "serial_nos",
			fieldtype: "Small Text",
			in_list_view: 1,
			label: __("Serial Nos"),
			read_only: 1,
		});
	}

	fields.push(
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
			fieldname: "company",
			fieldtype: "Link",
			options: "Company",
			in_list_view: 1,
			label: __("Company"),
			default: frappe.defaults.get_default("company") || "",
			description: __("Used to find this Item's Tax Template. Never printed on the label."),
			onchange: function () {
				barcodes_print.print_barcode_page._refresh_row_rate(this.grid_row);
			},
		},
		{
			fieldname: "tax_template",
			fieldtype: "Link",
			options: "Item Tax Template",
			in_list_view: 1,
			label: __("Tax Template"),
			description: __('If set, Rate prints "Incl. of taxes"; left blank, "Excl. of taxes". The tax % itself is never printed.'),
			get_query: function (doc) {
				// A convenience filter, not a requirement - Company can be
				// blank and still let any Item Tax Template be picked
				// directly, per "end of the day user just needs a tax value".
				return doc.company ? { filters: { company: doc.company } } : {};
			},
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
			description: __("Auto-fetched from Price List, marked up by Tax Template's tax % if one is set. Left off the label entirely if no price is found."),
		},
		{
			fieldname: "stock_qty",
			fieldtype: "Float",
			in_list_view: 1,
			label: __("Stock Qty"),
			read_only: 1,
			description: __("Informational only, never printed on the label. Blank for service/non-stock items; scoped to Batch No when one is picked."),
		},
		// Hidden bookkeeping only, never rendered as a grid column:
		// which Purchase Order/Purchase Receipt line (if any) this row
		// came from, so a successful print can bump that line's own
		// printed-barcode counter.
		{ fieldname: "source_row_name", fieldtype: "Data", hidden: 1 },
		{ fieldname: "max_allowed", fieldtype: "Int", hidden: 1 }
	);

	return fields;
};

barcodes_print.print_barcode_page.render = function (page) {
	page.main.empty();

	const $wrapper = $('<div class="print-barcode-page">')
		.css({ "max-width": "1400px", margin: "0 auto", "padding-top": "10px" })
		.appendTo(page.main);

	const $source_banner = $("<div>").appendTo($wrapper);

	$(`<p class="text-muted">${__(
		"Add items below, choose a label size, and print directly to your label printer. Nothing here is saved as a document - each print is a one-time action."
	)}</p>`).appendTo($wrapper);

	const $scan_area = $('<div style="margin-bottom: 10px; max-width: 320px;">').appendTo($wrapper);
	const $import_area = $('<div style="margin-bottom: 15px;">').appendTo($wrapper);
	// Label Size and My Printer sit above the item grid - both are chosen
	// once per print run, not per row, so they read more naturally as
	// setup before the table than as an afterthought below it.
	const $size_area = $('<div style="max-width: 200px; margin-bottom: 10px;">').appendTo($wrapper);
	const $printer_area = $('<div class="text-muted" style="margin-bottom: 15px; font-size: 13px;">').appendTo($wrapper);
	const $items_area = $('<div style="margin-bottom: 10px;">').appendTo($wrapper);
	const $total_area = $('<div class="text-muted" style="margin-bottom: 20px; font-size: 13px;">').appendTo($wrapper);

	// The Price List column's per-row default needs Barcode Print
	// Settings' Default Price List up front, so the grid itself isn't
	// built until this one small, read-permission-light call returns.
	// Returned so callers (on_page_load) can wait for _state to exist
	// before doing anything that depends on it (e.g. apply_route_options).
	return frappe.call({
		method: "barcodes_print.api.barcode.get_print_barcode_page_defaults",
	}).then((r) => {
		const defaults = r.message || {};
		const default_price_list = defaults.default_price_list || "";
		const print_connector = defaults.print_connector || "";
		const my_default_printer = defaults.my_default_printer || "";
		const batch_serial_print_mode = defaults.batch_serial_print_mode || "Both";

		const items_control = frappe.ui.form.make_control({
			parent: $items_area,
			df: {
				fieldname: "items",
				fieldtype: "Table",
				label: __("Items"),
				options: "Print Label Item",
				in_place_edit: false,
				cannot_add_rows: false,
				fields: barcodes_print.print_barcode_page.get_item_fields(default_price_list, batch_serial_print_mode),
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
				options: "Small\nMedium\nLarge\nSmall Label Only\nMedium Label Only\nLarge Label Only",
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
			print_connector,
			my_default_printer,
			batch_serial_print_mode,
			$source_banner,
			$total_area,
			$printer_area,
			$import_area,
			$scan_area,
			source_doctype: "",
			source_name: "",
		};

		barcodes_print.print_barcode_page.render_import_export();
		barcodes_print.print_barcode_page.render_scan_box();

		barcodes_print.print_barcode_page.update_total();
		barcodes_print.print_barcode_page.render_my_printer();
	});
};

// The current user's own saved printer (see api.printer) - shown so it's
// obvious which printer a click of "Print" will actually go to, with a
// "Change" link into a live picker rather than a free-text field a typo
// could silently break.
barcodes_print.print_barcode_page.render_my_printer = function () {
	const state = barcodes_print.print_barcode_page._state;
	const label = state.my_default_printer
		? __("My Printer: {0}", [state.my_default_printer])
		: __("My Printer: not set (using the site default/OS default)");
	state.$printer_area.empty();
	$(`<span>${frappe.utils.escape_html(label)}</span> `).appendTo(state.$printer_area);
	$(`<a href="#" style="margin-left: 6px;">${__("Change")}</a>`)
		.appendTo(state.$printer_area)
		.on("click", (e) => {
			e.preventDefault();
			barcodes_print.print_barcode_page.prompt_my_printer_choice();
		});
};

// Lists live printers from whichever connector Barcode Print Settings
// currently has active, lets the user pick one, and saves it as their own
// default via api.printer.set_my_default_printer - scoped server-side to
// the calling user, so this can never affect anyone else's.
barcodes_print.print_barcode_page.prompt_my_printer_choice = function () {
	const state = barcodes_print.print_barcode_page._state;
	const is_zebra = state.print_connector === "Zebra Browser Print";
	const list_printers = () =>
		is_zebra ? barcodes_print.zebra.list_printers() : barcodes_print.qz.list_printers();

	frappe.show_alert({ message: __("Looking for printers..."), indicator: "blue" });
	list_printers()
		.then((printers) => {
			if (!printers || !printers.length) {
				frappe.msgprint(
					__("No printers were found via {0}. Make sure it's installed, running, and connected, then try again.", [
						is_zebra ? "Zebra Browser Print" : "QZ Tray",
					])
				);
				return;
			}
			frappe.prompt(
				{
					fieldname: "printer",
					fieldtype: "Select",
					label: __("My Printer"),
					options: printers,
					default: state.my_default_printer || printers[0],
					reqd: 1,
				},
				(values) => {
					frappe.call({
						method: "barcodes_print.api.printer.set_my_default_printer",
						args: { printer_name: values.printer },
					}).then(() => {
						state.my_default_printer = values.printer;
						barcodes_print.print_barcode_page.render_my_printer();
						frappe.show_alert({ message: __("Saved"), indicator: "green" });
					});
				},
				__("Set My Printer")
			);
		})
		.catch((err) => {
			frappe.msgprint(__("Could not list printers: {0}", [err.message || err]));
		});
};

// Bulk-fill the grid from an uploaded Excel/CSV file, or download the
// current rows as a re-uploadable template - see api/import_items.py for
// the column shape and per-row validation rules.
barcodes_print.print_barcode_page.IMPORT_TEMPLATE_COLUMNS = [
	"item_code", "barcode", "no_of_barcodes", "price_list", "company", "tax_template", "batch_no", "serial_nos",
];

// Styled as real buttons, matching the native Frappe grid's own
// Download/Upload bulk-edit buttons (see frappe/public/js/frappe/form/grid.js
// setup_allow_bulk_edit/setup_download) - that native mechanism itself can't
// be reused here, since it requires a real bound frm (this grid is a
// standalone control with nothing saved as a document), so this wires the
// same visual button component to this app's own import/export endpoints.
barcodes_print.print_barcode_page.render_import_export = function () {
	const state = barcodes_print.print_barcode_page._state;
	state.$import_area.empty();
	frappe.ui
		.button({
			label: __("Load from File"),
			icon: "upload",
			variant: "outline",
			onclick: () => barcodes_print.print_barcode_page.prompt_import_file(),
		})
		.css("margin-right", "8px")
		.appendTo(state.$import_area);
	frappe.ui
		.button({
			label: __("Download Template"),
			icon: "download",
			variant: "outline",
			onclick: () => barcodes_print.print_barcode_page.download_import_template(),
		})
		.appendTo(state.$import_area);
};

barcodes_print.print_barcode_page.prompt_import_file = function () {
	new frappe.ui.FileUploader({
		folder: "Home",
		allow_multiple: false,
		restrictions: { allowed_file_types: [".xlsx", ".xls", ".csv"] },
		on_success: (file_doc) => {
			frappe.call({
				method: "barcodes_print.api.import_items.import_items_from_file",
				args: { file_url: file_doc.file_url },
				freeze: true,
				freeze_message: __("Reading file..."),
			}).then((r) => {
				const result = r.message || {};
				const rows = result.rows || [];
				const errors = result.errors || [];
				if (rows.length) {
					barcodes_print.print_barcode_page.populate_rows_from_import(rows);
				}
				if (errors.length) {
					barcodes_print.print_barcode_page.show_import_errors(errors, rows.length);
				} else {
					frappe.show_alert({ message: __("{0} rows added.", [rows.length]), indicator: "green" });
				}
			});
		},
	});
};

barcodes_print.print_barcode_page.show_import_errors = function (errors, added_count) {
	const list = errors.map((e) => `<li>${frappe.utils.escape_html(e)}</li>`).join("");
	frappe.msgprint({
		title: __("Some rows were skipped"),
		indicator: "orange",
		message: `<p>${__("{0} rows were added. {1} rows were skipped:", [added_count, errors.length])}</p><ul>${list}</ul>`,
	});
};

// Mirrors apply_route_options()'s own row-population pattern (grid.add_new_row()
// + direct doc/refresh_field, since a programmatically-added row never gets
// its field controls lazily built) - kept as its own function rather than
// sharing apply_route_options's, since an imported row's shape (serial_nos
// already newline-joined, no source_row_name/max_allowed) differs from a
// Purchase Order/Receipt line's.
barcodes_print.print_barcode_page.populate_rows_from_import = function (rows) {
	const state = barcodes_print.print_barcode_page._state;
	const grid = state.items_control.grid;
	const mode = state.batch_serial_print_mode || "Both";

	rows.forEach((item_row) => {
		grid.add_new_row();
		const row = grid.grid_rows[grid.grid_rows.length - 1];
		row.doc.item = item_row.item;
		row.doc.no_of_barcodes = item_row.no_of_barcodes;
		row.doc.price_list = item_row.price_list || state.default_price_list || "";
		row.doc.company = item_row.company || "";
		row.doc.tax_template = item_row.tax_template || "";
		const refresh_fields = ["item", "no_of_barcodes", "price_list", "company", "tax_template"];
		if (mode !== "Serial Only" && mode !== "Neither") {
			row.doc.batch_no = item_row.batch_no || "";
			refresh_fields.push("batch_no");
		}
		if (mode !== "Batch Only" && mode !== "Neither") {
			row.doc.serial_nos = item_row.serial_nos || "";
			refresh_fields.push("serial_nos");
		}
		refresh_fields.forEach((f) => row.refresh_field(f));

		frappe.call({
			method: "barcodes_print.api.item.get_item_barcode_details",
			args: {
				item_code: item_row.item,
				barcode: item_row.barcode || "",
				price_list: row.doc.price_list,
				tax_template: row.doc.tax_template || "",
			},
			callback(r) {
				const item_data = r.message || {};
				row.doc.item_name = item_data.item_name || "";
				row.doc.uom = item_data.uom || "";
				row.doc.barcode = item_data.barcode || "";
				row.doc.barcode_type = item_data.barcode_type || "";
				row.doc.rate = item_data.rate;
				row.doc._has_batch_no = item_data.has_batch_no;
				row.doc._has_serial_no = item_data.has_serial_no;
				["item_name", "uom", "barcode", "barcode_type", "rate"].forEach((f) => row.refresh_field(f));

				const choices = item_data.barcodes || [];
				if (choices.length > 1 && !item_data.barcode) {
					barcodes_print.print_barcode_page.prompt_barcode_choice(row, choices);
				}
			},
		});
	});
	barcodes_print.print_barcode_page.update_total();
};

barcodes_print.print_barcode_page.download_import_template = function () {
	const state = barcodes_print.print_barcode_page._state;
	const cols = barcodes_print.print_barcode_page.IMPORT_TEMPLATE_COLUMNS;
	const field_for_col = {
		item_code: "item", barcode: "barcode", no_of_barcodes: "no_of_barcodes", price_list: "price_list",
		company: "company", tax_template: "tax_template", batch_no: "batch_no", serial_nos: "serial_nos",
	};
	const rows = state.items_control.get_value() || [];
	const csv_escape = (v) => {
		const s = v === null || v === undefined ? "" : String(v);
		return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
	};
	const lines = [cols.join(",")];
	rows.forEach((row) => {
		lines.push(cols.map((c) => csv_escape(row[field_for_col[c]])).join(","));
	});

	const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
	const url = URL.createObjectURL(blob);
	const $link = $("<a>").attr({ href: url, download: "barcode_print_template.csv" }).appendTo("body");
	$link[0].click();
	$link.remove();
	URL.revokeObjectURL(url);
};

// Scan a barcode/Serial No/Batch No (or type an Item Code) to fill or
// update a grid row, instead of always picking an Item from the dropdown
// first - see api.item.resolve_scan for what a scan can match.
barcodes_print.print_barcode_page.render_scan_box = function () {
	const state = barcodes_print.print_barcode_page._state;
	state.$scan_area.empty();
	const scan_control = frappe.ui.form.make_control({
		parent: state.$scan_area,
		df: {
			fieldname: "scan_barcode",
			fieldtype: "Data",
			label: __("Scan Barcode"),
			placeholder: __("Scan or type a barcode, Serial No, Batch No, or Item Code, then press Enter"),
		},
		render_input: true,
	});
	scan_control.refresh();
	scan_control.$input.on("keydown", (e) => {
		if (e.key !== "Enter") return;
		e.preventDefault();
		const value = (scan_control.get_value() || "").trim();
		if (!value) return;
		barcodes_print.print_barcode_page.handle_scan_value(value);
		scan_control.set_value("");
	});
};

barcodes_print.print_barcode_page.handle_scan_value = function (value) {
	const state = barcodes_print.print_barcode_page._state;
	frappe.call({
		method: "barcodes_print.api.item.resolve_scan",
		args: { scanned_value: value, price_list: state.default_price_list || "" },
		freeze: true,
	}).then((r) => {
		const data = r.message || {};
		barcodes_print.print_barcode_page.apply_scan_result(data);
	});
};

// Finds the grid row a scanned Item already has, if any - a plain item
// matches on Item alone; a batch-tracked item also matches the scanned
// Batch No (a different batch is a separate row, not the same one); a
// serial-tracked item's existing row is matched so a new serial can be
// appended to it. Searched last-added-first, so "more than one existing
// row matches" resolves to whichever was most recently added/edited.
barcodes_print.print_barcode_page._find_matching_scan_row = function (item_code, batch_no) {
	const state = barcodes_print.print_barcode_page._state;
	const grid_rows = state.items_control.grid.grid_rows || [];
	for (let i = grid_rows.length - 1; i >= 0; i--) {
		const row = grid_rows[i];
		if (row.doc.item !== item_code) continue;
		if (row.doc._has_batch_no && !row.doc._has_serial_no) {
			if ((row.doc.batch_no || "") === (batch_no || "")) return row;
			continue;
		}
		return row;
	}
	return null;
};

barcodes_print.print_barcode_page.apply_scan_result = function (data) {
	const state = barcodes_print.print_barcode_page._state;
	const item_code = data.item;
	const batch_no = data.batch_no || "";
	const serial_no = data.serial_no || "";
	const existing = barcodes_print.print_barcode_page._find_matching_scan_row(item_code, batch_no);

	if (existing) {
		if (data.has_serial_no && serial_no) {
			const serials = (existing.doc.serial_nos || "").split("\n").filter(Boolean);
			if (!serials.includes(serial_no)) serials.push(serial_no);
			existing.doc.serial_nos = serials.join("\n");
			existing.refresh_field("serial_nos");
		} else {
			existing.doc.no_of_barcodes = cint(existing.doc.no_of_barcodes) + 1;
			existing.refresh_field("no_of_barcodes");
		}
		barcodes_print.print_barcode_page._refresh_row_stock_qty(existing, existing.doc.batch_no || "");
		barcodes_print.print_barcode_page.update_total();
		return;
	}

	const grid = state.items_control.grid;
	grid.add_new_row();
	const row = grid.grid_rows[grid.grid_rows.length - 1];
	row.doc.item = item_code;
	row.doc.item_name = data.item_name || "";
	row.doc.uom = data.uom || "";
	row.doc.barcode = data.barcode || "";
	row.doc.barcode_type = data.barcode_type || "";
	row.doc.rate = data.rate;
	row.doc.price_list = state.default_price_list || "";
	row.doc._has_batch_no = data.has_batch_no;
	row.doc._has_serial_no = data.has_serial_no;
	const refresh_fields = ["item", "item_name", "uom", "barcode", "barcode_type", "rate", "price_list"];
	if (data.has_batch_no && batch_no) {
		row.doc.batch_no = batch_no;
		refresh_fields.push("batch_no");
	}
	if (data.has_serial_no && serial_no) {
		row.doc.serial_nos = serial_no;
		refresh_fields.push("serial_nos");
	}
	refresh_fields.forEach((f) => row.refresh_field(f));
	barcodes_print.print_barcode_page._refresh_row_stock_qty(row, batch_no);
	barcodes_print.print_barcode_page.update_total();
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
			// Auto-fetched straight from the source document/line (see
			// get_purchase_document_items) - never a user/site default -
			// so the price printed always matches what's actually on that
			// Purchase Order/Receipt line.
			row.doc.company = item_row.company || "";
			row.doc.tax_template = item_row.tax_template || "";
			// Only ever populated for a Purchase Receipt line (see
			// get_purchase_document_items) - already resolved server-side
			// from either the line's own batch_no/serial_no fields or its
			// Serial and Batch Bundle, so no picker dialog is needed here.
			// Guarded by the same Batch/Serial Printing setting the grid's
			// own columns are built from - these fields don't exist on the
			// row at all when a type is off.
			const mode = state.batch_serial_print_mode || "Both";
			const refresh_fields = ["item", "no_of_barcodes", "price_list", "company", "tax_template"];
			if (mode !== "Serial Only" && mode !== "Neither") {
				row.doc.batch_no = item_row.batch_no || "";
				refresh_fields.push("batch_no");
			}
			if (mode !== "Batch Only" && mode !== "Neither") {
				row.doc.serial_nos = item_row.serial_no || "";
				refresh_fields.push("serial_nos");
			}
			refresh_fields.forEach((f) => row.refresh_field(f));

			frappe.call({
				method: "barcodes_print.api.item.get_item_barcode_details",
				args: {
					item_code: item_row.item,
					price_list: row.doc.price_list,
					tax_template: row.doc.tax_template || "",
				},
				callback(r) {
					const item_data = r.message || {};
					row.doc.item_name = item_data.item_name || "";
					row.doc.uom = item_data.uom || "";
					row.doc.barcode = item_data.barcode || "";
					row.doc.barcode_type = item_data.barcode_type || "";
					row.doc.rate = item_data.rate;
					row.doc._has_batch_no = item_data.has_batch_no;
					row.doc._has_serial_no = item_data.has_serial_no;
					["item_name", "uom", "barcode", "barcode_type", "rate"].forEach((f) => row.refresh_field(f));

					const choices = item_data.barcodes || [];
					if (choices.length > 1 && !item_data.barcode) {
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
