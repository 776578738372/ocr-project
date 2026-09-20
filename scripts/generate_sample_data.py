"""Generates the supplier master CSV and every sample W-9 document used by the
CLI demo and the eval harness.

Run once from the repo root: `python scripts/generate_sample_data.py`
Everything it writes goes under data/, and eval/manifest.json (ground truth
for the eval harness) is hand-written separately to match what this script
produces -- see eval/README or the design doc for the scenario table.
"""

from __future__ import annotations

import csv
import io
import os

import pymupdf as fitz
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from w9_onboarding.security import hash_identifier, tokenize_tin

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO_ROOT, "data")
SAMPLES_DIR = os.path.join(DATA_DIR, "sample_w9s")

_CLASSIFICATION_LABELS = [
    ("individual_sole_proprietor", "Individual/sole proprietor"),
    ("llc", "Limited liability company"),
    ("c_corporation", "C Corporation"),
    ("s_corporation", "S Corporation"),
    ("partnership", "Partnership"),
    ("trust_estate", "Trust/estate"),
    ("other", "Other"),
]


def render_w9_pdf(
    path: str,
    *,
    legal_name: str,
    dba_name: str = "",
    tax_classification: str = "c_corporation",
    llc_subclass: str | None = None,
    foreign_partner: bool = False,
    include_line_3b: bool = True,
    street: str,
    city: str,
    state: str,
    zip_code: str,
    tin: str,
    signer_name: str | None = None,
    date: str | None = None,
) -> None:
    c = canvas.Canvas(path, pagesize=letter)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, 760, "Form W-9 (Rev. March 2024)")
    c.setFont("Helvetica", 10)
    c.drawString(50, 744, "Request for Taxpayer Identification Number and Certification")

    c.setFont("Helvetica", 10)
    y = 710

    def line(text: str, gap: int = 16) -> None:
        nonlocal y
        c.drawString(50, y, text)
        y -= gap

    line(f"1 Name (as shown on your income tax return): {legal_name}")
    line(f"2 Business name/disregarded entity name, if different from above: {dba_name}")
    line("3a Federal tax classification (check one):")
    for key, label in _CLASSIFICATION_LABELS:
        mark = "X" if key == tax_classification else " "
        suffix = ""
        if key == "llc":
            suffix = f"   (enter tax classification: C, S, or P): {llc_subclass or ''}"
        line(f"    [{mark}] {label}{suffix}", gap=14)
    if include_line_3b:
        mark = "X" if foreign_partner else " "
        line(f"3b Foreign partner, owner, or beneficiary indicator (check if applicable): [{mark}] Yes")
    line(f"5 Address (number, street, and apt. or suite no.): {street}")
    line(f"6 City, state, and ZIP code: {city}, {state} {zip_code}")
    line("7 List account number(s) here (optional):")
    y -= 10
    line("Part I Taxpayer Identification Number (TIN):")
    line(tin)
    y -= 10
    line("Part II Certification")
    line(f"Signature of U.S. person: {signer_name or ''}   Date: {date or ''}")

    c.showPage()
    c.save()


def render_wrong_form_pdf(path: str) -> None:
    """A digital PDF with real embedded text, but it's a W-8BEN, not a W-9 --
    none of the W-9 field-label regexes should match anything in here."""
    c = canvas.Canvas(path, pagesize=letter)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, 760, "Form W-8BEN (Rev. October 2021)")
    c.setFont("Helvetica", 10)
    c.drawString(50, 744, "Certificate of Foreign Status of Beneficial Owner for United States Tax Withholding")
    y = 710
    for text in [
        "Part I Identification of Beneficial Owner",
        "1  Name of individual who is the beneficial owner: Klaus Weber",
        "2  Country of citizenship: Germany",
        "3  Permanent residence address: Bahnhofstrasse 12, Berlin, Germany",
        "6  Foreign tax identifying number: DE123456789",
        "Part III Certification",
        "Sign here: Klaus Weber   Date: 09/10/2026",
    ]:
        c.drawString(50, y, text)
        y -= 16
    c.showPage()
    c.save()


def rasterize_pdf_to_jpeg(pdf_path: str, jpeg_path: str) -> None:
    doc = fitz.open(pdf_path)
    page = doc[0]
    pix = page.get_pixmap(dpi=150)
    pix.save(jpeg_path)
    doc.close()


def wrap_image_in_textless_pdf(jpeg_path: str, pdf_path: str) -> None:
    """Simulates a scanned document saved as a PDF: a full-page image with no
    embedded text layer at all (classifier stage 2: 0 embedded characters)."""
    c = canvas.Canvas(pdf_path, pagesize=letter)
    c.drawImage(jpeg_path, 0, 0, width=letter[0], height=letter[1])
    c.showPage()
    c.save()


def write_invalid_file(path: str) -> None:
    with open(path, "wb") as f:
        f.write(b"This is not a PDF, PNG, or JPEG -- just plain bytes.\x00\x01\x02")


def write_supplier_master(path: str) -> None:
    rows = [
        {
            "supplier_id": "sup_00417",
            "legal_name": "Acme Corporation",
            "dba_name": "",
            "tin_token": tokenize_tin("27-4821093"),
            "tax_classification": "c_corporation",
            "address_street": "1150 Industrial Pkwy",
            "address_city": "Columbus",
            "address_state": "OH",
            "address_zip": "43215",
            "bank_routing_hash": hash_identifier("021000021"),
            "bank_account_last4": "4821",
            "bank_account_holder_name": "Acme Corporation",
            "status": "active",
            "last_updated_at": "2025-11-02",
        },
        {
            "supplier_id": "sup_00892",
            "legal_name": "Globex Industries Inc",
            "dba_name": "Globex",
            "tin_token": tokenize_tin("45-1122334"),
            "tax_classification": "c_corporation",
            "address_street": "500 Commerce Dr",
            "address_city": "Austin",
            "address_state": "TX",
            "address_zip": "73301",
            "bank_routing_hash": hash_identifier("111000025"),
            "bank_account_last4": "7788",
            "bank_account_holder_name": "Globex Industries Inc",
            "status": "active",
            "last_updated_at": "2025-08-14",
        },
        {
            "supplier_id": "sup_01203",
            "legal_name": "Initech LLC",
            "dba_name": "",
            "tin_token": tokenize_tin("88-9988776"),
            "tax_classification": "llc",
            "address_street": "200 Office Park Blvd",
            "address_city": "Denver",
            "address_state": "CO",
            "address_zip": "80202",
            "bank_routing_hash": hash_identifier("102000021"),
            "bank_account_last4": "3344",
            "bank_account_holder_name": "Initech LLC",
            "status": "active",
            "last_updated_at": "2025-06-30",
        },
    ]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    os.makedirs(SAMPLES_DIR, exist_ok=True)

    write_supplier_master(os.path.join(DATA_DIR, "sample_supplier_master.csv"))

    # 1. Clean match, minor address diff -> MATCHED_EXIST / UPDATE_EXISTING
    render_w9_pdf(
        os.path.join(SAMPLES_DIR, "clean_w9_acme.pdf"),
        legal_name="Acme Corporation", tax_classification="c_corporation",
        street="1200 Industrial Pkwy", city="Columbus", state="OH", zip_code="43215",
        tin="27-4821093", signer_name="Jane Whitfield", date="09/15/2026",
    )

    # 2. No TIN/name match anywhere -> CREATE_NEW / RESOLVED_NEW
    render_w9_pdf(
        os.path.join(SAMPLES_DIR, "clean_w9_newco.pdf"),
        legal_name="Bright Path Logistics LLC", tax_classification="llc", llc_subclass="P",
        street="900 Freight Way", city="Reno", state="NV", zip_code="89501",
        tin="94-1234567", signer_name="Marcus Boone", date="09/12/2026",
    )

    # 3. Same TIN as Acme, totally different name -> ESCALATED_HUMAN (possible TIN hijack)
    render_w9_pdf(
        os.path.join(SAMPLES_DIR, "clean_w9_tin_hijack.pdf"),
        legal_name="Shell Ventures Group", tax_classification="other",
        street="1 Anonymous Ln", city="Wilmington", state="DE", zip_code="19801",
        tin="27-4821093", signer_name="A. Smith", date="09/18/2026",
    )

    # 4. Clean match on name+TIN, unsigned -> blocking validation flag -> ESCALATED_HUMAN
    render_w9_pdf(
        os.path.join(SAMPLES_DIR, "clean_w9_unsigned.pdf"),
        legal_name="Vertex Analytics Inc", tax_classification="c_corporation",
        street="77 Summit Ave", city="Boulder", state="CO", zip_code="80301",
        tin="31-7654321", signer_name=None, date=None,
    )

    # 5. Wrong form entirely (W-8, not W-9) -> ERR_POSSIBLE_WRONG_FORM -> ESCALATED_HUMAN
    render_wrong_form_pdf(os.path.join(SAMPLES_DIR, "wrong_form_w8.pdf"))

    # 6. Invalid file type -> REJECTED_INVALID
    write_invalid_file(os.path.join(SAMPLES_DIR, "invalid_document.pdf"))

    # 7. Scanned/photo (JPEG) -> VLM_EXTRACTION (mock) -> unknown fields -> ESCALATED_HUMAN
    rasterize_pdf_to_jpeg(
        os.path.join(SAMPLES_DIR, "clean_w9_acme.pdf"),
        os.path.join(SAMPLES_DIR, "scanned_w9_acme.jpg"),
    )

    # 8. PDF wrapper around a scanned image, no embedded text layer -> VLM_EXTRACTION (mock)
    wrap_image_in_textless_pdf(
        os.path.join(SAMPLES_DIR, "scanned_w9_acme.jpg"),
        os.path.join(SAMPLES_DIR, "scanned_wrapper_w9.pdf"),
    )

    print(f"Wrote supplier master + {len(os.listdir(SAMPLES_DIR))} sample documents to {DATA_DIR}")


if __name__ == "__main__":
    main()
