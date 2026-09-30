frappe.provide("barcodes_print");

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
frappe.provide("barcodes_print.qz");

barcodes_print.qz = {
	_configured: false,

	_configure_security() {
		if (this._configured) {
			return;
		}
		// Signed connection: the certificate identifies this site to QZ Tray
		// consistently (generated once server-side, never changes), and every
		// request is signed with the matching private key, which never leaves
		// the server - see api/qz_sign.py. Without this, QZ Tray treats the
		// connection as anonymous and shows its "Action Required - Untrusted
		// website" popup on every single print, for every user, since
		// "Remember this decision" can't stick to an identity that isn't
		// actually verifiable. With it, a user approves once, ever.
		qz.security.setCertificatePromise((resolve, reject) => {
			frappe.call({ method: "barcodes_print.api.qz_sign.get_certificate" })
				.then((r) => resolve(r.message))
				.catch(reject);
		});
		qz.security.setSignatureAlgorithm("SHA512");
		qz.security.setSignaturePromise((to_sign) => (resolve, reject) => {
			frappe.call({
				method: "barcodes_print.api.qz_sign.sign",
				args: { request: to_sign },
			})
				.then((r) => resolve(r.message))
				.catch(reject);
		});
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
		// qz.printers.getDefault() doesn't always reject when this computer
		// has no OS-level default printer - on some machines/QZ versions it
		// resolves with an empty/blank value instead. Passing that straight
		// through to qz.print() surfaces QZ Tray's own generic "A printer
		// must be specified before printing" error instead of a message that
		// actually tells the user what to do about it - so both the reject
		// and the empty-resolve case are handled the same way here.
		let default_printer;
		try {
			default_printer = await qz.printers.getDefault();
		} catch (err) {
			default_printer = null;
		}
		if (!default_printer) {
			frappe.throw(
				__(
					"No Default Label Printer is set in Barcode Print Settings, and this computer has no default printer either. Please set a Default Label Printer in Barcode Print Settings, or set a default printer on this computer."
				)
			);
		}
		return default_printer;
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
