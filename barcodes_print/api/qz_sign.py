# Copyright (c) 2026, Rahul Chaudhary and contributors
# For license information, please see license.txt

"""QZ Tray signed-connection support.

Without this, the app connects to QZ Tray anonymously (see the trivial
setCertificatePromise/setSignaturePromise in public/js/qz_tray_print.js's
predecessor) - QZ Tray then shows its own "Action Required - Untrusted
website" popup, and because there is no real certificate identifying the
site, "Remember this decision" only holds for that one connection, not
permanently. Every user sees that popup on every print, every session.

Signing fixes this: a private key lives only on the server (encrypted,
never sent to the browser); its matching public certificate is what QZ
Tray shows the user for a one-time approval; the client then calls back
to this app's own `sign` endpoint for every QZ Tray request, and QZ Tray
verifies the signature against the certificate it already trusts. Once a
user approves that one real identity, QZ Tray remembers it for good - no
more per-print prompts.
"""

import base64
import datetime

import frappe
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from frappe.utils.password import get_decrypted_password, set_encrypted_password

SETTINGS_DOCTYPE = "Barcode Print Settings"
SETTINGS_NAME = "Barcode Print Settings"
PRIVATE_KEY_FIELDNAME = "qz_private_key"
CERTIFICATE_FIELDNAME = "qz_certificate"


def _generate_keypair() -> tuple[str, str]:
	private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

	site_name = frappe.local.site if getattr(frappe.local, "site", None) else "Barcodes Print"
	subject = issuer = x509.Name([
		x509.NameAttribute(NameOID.COMMON_NAME, "Barcodes Print"),
		x509.NameAttribute(NameOID.ORGANIZATION_NAME, site_name),
	])
	now = datetime.datetime.now(datetime.timezone.utc)
	cert = (
		x509.CertificateBuilder()
		.subject_name(subject)
		.issuer_name(issuer)
		.public_key(private_key.public_key())
		.serial_number(x509.random_serial_number())
		.not_valid_before(now - datetime.timedelta(days=1))
		.not_valid_after(now + datetime.timedelta(days=3650))
		.sign(private_key, hashes.SHA256())
	)

	private_pem = private_key.private_bytes(
		encoding=serialization.Encoding.PEM,
		format=serialization.PrivateFormat.TraditionalOpenSSL,
		encryption_algorithm=serialization.NoEncryption(),
	).decode()
	cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
	return private_pem, cert_pem


def _ensure_keypair() -> str:
	"""Return the public certificate, generating a fresh keypair the first
	time this is ever called on a given site. Idempotent - safe to call on
	every request."""
	cert_pem = frappe.db.get_single_value(SETTINGS_DOCTYPE, CERTIFICATE_FIELDNAME)
	if cert_pem:
		return cert_pem

	private_pem, cert_pem = _generate_keypair()
	set_encrypted_password(SETTINGS_DOCTYPE, SETTINGS_NAME, private_pem, fieldname=PRIVATE_KEY_FIELDNAME)
	frappe.db.set_single_value(SETTINGS_DOCTYPE, CERTIFICATE_FIELDNAME, cert_pem)
	return cert_pem


@frappe.whitelist()
def get_certificate() -> str:
	"""The public certificate QZ Tray shows the user for a one-time approval.
	Not sensitive, but still gated behind the same "can this user print
	barcodes at all" check used elsewhere in this app."""
	frappe.has_permission("Barcode Print Log", "read", throw=True)
	return _ensure_keypair()


@frappe.whitelist()
def sign(request: str) -> str:
	"""Sign one QZ Tray request (the exact string it hands us) with this
	site's private key, so QZ Tray can verify it against the certificate
	it already trusts. Algorithm must match qz.security.setSignatureAlgorithm
	on the client (SHA512) - see public/js/qz_tray_print.js."""
	frappe.has_permission("Barcode Print Log", "read", throw=True)
	_ensure_keypair()
	private_pem = get_decrypted_password(SETTINGS_DOCTYPE, SETTINGS_NAME, fieldname=PRIVATE_KEY_FIELDNAME)
	private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
	signature = private_key.sign(request.encode(), padding.PKCS1v15(), hashes.SHA512())
	return base64.b64encode(signature).decode()
