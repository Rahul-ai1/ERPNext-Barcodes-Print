# Privacy Policy — Barcodes Print

**Last updated:** 2026-10-30

Barcodes Print is a Frappe/ERPNext app that runs entirely inside your own ERPNext site and your own infrastructure. It does not operate any external service, and the developer does not collect, receive, store, or have access to any data from your site.

## What data this app touches

All data the app reads or writes stays inside your own ERPNext database:

- **Item, Item Price, Item Barcode, Batch, Serial No, Price List** — read to resolve what to print.
- **Purchase Order / Purchase Receipt / Purchase Invoice / Subcontracting Order** — read, when you choose to print from one.
- **Barcode Print Settings** — your own site's configuration (printer connector, label dimensions, display rules).
- **Barcode Print Log** — a record this app creates on your own site of what was printed, when, and by whom, for your own audit purposes.
- **Your saved default printer** — stored against your own User record, on your own site.

None of this data ever leaves your own ERPNext site through this app. The app sends print jobs (raw ZPL) directly from your browser to a printer connector running on your own local network — QZ Tray or Zebra Browser Print — never to any server operated by the developer.

## Third-party services

This app does not call any third-party API or external service. QZ Tray and Zebra Browser Print are local desktop applications you install yourself; they are not operated by the developer of this app.

## Changes to this policy

Any future change to this policy will be published at this same URL, in this repository.

## Contact

Questions about this policy or the app can be raised via [GitHub Issues](https://github.com/Rahul-ai1/ERPNext-Barcodes-Print/issues).
