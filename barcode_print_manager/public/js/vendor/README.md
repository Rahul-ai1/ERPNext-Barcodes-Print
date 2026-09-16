# Vendor JS

## qz-tray.js

QZ Tray's official open-source browser client (LGPL-2.1), version 2.3.0,
matching the QZ Tray desktop app version this app was built against.
Downloaded from https://www.npmjs.com/package/qz-tray. Safe to commit and
redistribute.

## Zebra Browser Print SDK (not included)

Zebra's `BrowserPrint-x.x.x.min.js` client library is proprietary and
license-gated - it is not available on npm/CDN and cannot legally be
committed to this repository.

To enable the "Zebra Browser Print" connector on a real Windows/Mac
deployment:

1. Download the Browser Print SDK from Zebra (developer.zebra.com) and
   the Browser Print desktop application for the target OS.
2. Place the SDK's `BrowserPrint-x.x.x.min.js` file in this folder.
3. Add its path to `app_include_js` in `hooks.py`, above
   `zebra_browser_print.js`, e.g.:
   `"/assets/barcode_print_manager/js/vendor/BrowserPrint-3.1.250.min.js"`
4. Run `bench build --app barcode_print_manager` and restart the bench.

Until this is done, the QZ Tray connector works independently and is
unaffected - `public/js/zebra_browser_print.js` only throws a clear error
if a user's Barcode Print Settings is configured for the Zebra Browser
Print connector and the SDK script isn't present.
