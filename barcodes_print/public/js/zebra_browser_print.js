frappe.provide("barcodes_print.zebra");

// ---- Zebra Browser Print connector ----
// Zebra Browser Print's client SDK (BrowserPrint-x.x.x.min.js) is Zebra's
// own proprietary, license-gated file - it cannot be committed to this
// open app repo. To use this connector, download it from Zebra and place
// it at public/js/vendor/BrowserPrint-<version>.min.js on this bench, then
// list it in hooks.py app_include_js above this file.
//
// NOTE: Zebra Browser Print has no public Linux client (Windows/macOS
// only as of this writing), so this connector could only be built against
// its publicly documented JS SDK shape and has not been verified against
// a real device. Verify on a Windows/Mac test machine before relying on
// it in production - see the project notes in the README.
barcodes_print.zebra = {
	_check_sdk() {
		if (typeof BrowserPrint === "undefined") {
			frappe.throw(
				__(
					"Zebra Browser Print library was not found on this page. Please install the Zebra Browser Print application and its browser SDK script on this computer, then contact your administrator."
				)
			);
		}
	},

	get_device(printer_name) {
		this._check_sdk();
		return new Promise((resolve, reject) => {
			if (printer_name) {
				BrowserPrint.getLocalDevices(
					(devices) => {
						const match = (devices || []).find(
							(d) => d.name === printer_name || d.uid === printer_name
						);
						if (match) {
							resolve(match);
						} else {
							reject(
								new Error(
									__("Printer '{0}' was not found via Zebra Browser Print.", [printer_name])
								)
							);
						}
					},
					(err) => reject(new Error(err)),
					"printer"
				);
			} else {
				BrowserPrint.getDefaultDevice(
					"printer",
					(device) => resolve(device),
					(err) => reject(new Error(err))
				);
			}
		});
	},

	async print(job) {
		this._check_sdk();
		const device = await this.get_device(job.printer);
		return new Promise((resolve, reject) => {
			device.send(
				job.zpl,
				() => resolve(),
				(err) => reject(new Error(err))
			);
		});
	},
};
