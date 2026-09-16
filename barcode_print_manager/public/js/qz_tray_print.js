frappe.provide("barcode_print_manager");

// Item auto-fetch for the Print Barcode page's dialog grid lives in
// page/print_barcode/print_barcode.js as the "item" field's own onchange
// handler - a Dialog Table field's rows aren't tagged with a real doctype,
// so doctype-keyed frappe.ui.form.on triggers (the old approach here,
// before this app dropped its submittable "Print Label and Barcode"
// document for a one-time-use page) never fire for them.

// ---- QZ Tray connector ----
// Sends raw ZPL straight to a local printer via the QZ Tray desktop app.
// No print preview, no browser print dialog - see the QZ Tray setup guide
// in the README for one-time end-user setup.
frappe.provide("barcode_print_manager.qz");

barcode_print_manager.qz = {
	_configured: false,

	_configure_security() {
		if (this._configured) {
			return;
		}
		// Unsigned connections: QZ Tray shows its own "Action Required"
		// allow/deny popup the first time this site connects (or every
		// time, unless the user checks "Remember this decision").
		qz.security.setCertificatePromise((resolve) => resolve());
		qz.security.setSignaturePromise(() => (resolve) => resolve());
		this._configured = true;
	},

	async ensure_connected() {
		if (typeof qz === "undefined") {
			frappe.throw(__("QZ Tray browser library failed to load. Please reload the page and try again."));
		}
		this._configure_security();
		if (qz.websocket.isActive()) {
			return;
		}
		try {
			await qz.websocket.connect();
		} catch (err) {
			frappe.throw(
				__(
					"Could not connect to QZ Tray. Please make sure QZ Tray is installed and running on this computer (check the system tray), then try again."
				)
			);
		}
	},

	async resolve_printer(printer_name) {
		if (printer_name) {
			return printer_name;
		}
		try {
			return await qz.printers.getDefault();
		} catch (err) {
			frappe.throw(
				__(
					"No Default Label Printer is set in Barcode Print Settings, and QZ Tray could not find a default printer either. Please set a Default Label Printer in Barcode Print Settings, or set a default printer on this computer."
				)
			);
		}
	},

	async print(job) {
		await this.ensure_connected();
		const printer_name = await this.resolve_printer(job.printer);
		const config = qz.configs.create(printer_name);
		// A plain string is sent as raw/command/plain data - exactly the ZPL
		// text as built server-side, unmodified.
		await qz.print(config, [job.zpl]);
	},
};
