"""Generate the three fictional sample PDF forms shipped with the repository.

Every organisation, person and form here is invented. The forms imitate the shape of real
partner paperwork (a beneficiary change, a direct-debit mandate, a tax self-certification)
so the engine can be exercised end to end without any real document.

Requires the optional ``reportlab`` dependency: ``pip install form-fill-engine[samples]``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Text:
    name: str
    label: str
    width: float = 260
    max_len: int | None = None


@dataclass(frozen=True)
class Checkbox:
    name: str
    label: str


@dataclass(frozen=True)
class Radio:
    name: str
    label: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class Section:
    heading: str
    items: tuple[Text | Checkbox | Radio, ...]


@dataclass(frozen=True)
class FormSpec:
    filename: str
    provider: str
    title: str
    reference: str
    sections: tuple[Section, ...]


FORMS: tuple[FormSpec, ...] = (
    FormSpec(
        filename="northwind_beneficiary_change.pdf",
        provider="Northwind Assurance",
        title="Beneficiary Designation Change",
        reference="NWA-BEN-03",
        sections=(
            Section(
                "1. Policy",
                (
                    Text("policy_number", "Policy number", 140, max_len=12),
                    Radio("contract_type", "Contract type", ("life", "savings")),
                    Text("life_cover_amount", "Life cover amount (EUR)", 140),
                ),
            ),
            Section(
                "2. Policyholder",
                (
                    Text("holder_name", "Full name", max_len=40),
                    Text("holder_birth_date", "Date of birth (DD/MM/YYYY)", 120),
                    Text("holder_national_id", "National ID number", 160),
                    Text("holder_email", "E-mail"),
                    Text("kyc_review_date", "Last identity review (DD/MM/YYYY)", 120),
                ),
            ),
            Section(
                "3. Beneficiaries",
                (
                    Text("benef1_name", "Beneficiary 1, full name"),
                    Text("benef1_share", "Beneficiary 1, share (%)", 60),
                    Text("benef2_name", "Beneficiary 2, full name"),
                    Text("benef2_share", "Beneficiary 2, share (%)", 60),
                    Checkbox("irrevocable", "Designation is irrevocable"),
                ),
            ),
            Section(
                "4. Signature",
                (
                    Text("sign_place", "Signed at", 180),
                    Text("sign_date", "Date (DD/MM/YYYY)", 120),
                ),
            ),
        ),
    ),
    FormSpec(
        filename="bluepeak_sepa_mandate.pdf",
        provider="Bluepeak Wealth Partners",
        title="SEPA Direct Debit Mandate, Regular Contributions",
        reference="BWP-SDD-11",
        sections=(
            Section(
                "1. Account",
                (
                    Text("account_reference", "Investment account reference", 160, max_len=10),
                    Radio("frequency", "Contribution frequency", ("monthly", "quarterly")),
                    Text("amount", "Contribution amount (EUR)", 120),
                ),
            ),
            Section(
                "2. Debtor",
                (
                    Text("debtor_name", "Account holder name", max_len=40),
                    Text("debtor_street", "Street and number"),
                    Text("debtor_postcode", "Postcode", 90),
                    Text("debtor_city", "City", 180),
                    Text("debtor_country", "Country (ISO code)", 60, max_len=2),
                ),
            ),
            Section(
                "3. Bank account",
                (
                    Text("iban", "IBAN", 300, max_len=42),
                    Text("bic", "BIC", 140, max_len=11),
                    Checkbox("mandate_consent", "Debtor authorises the debit (recorded consent)"),
                ),
            ),
            Section("4. Signature", (Text("mandate_date", "Date (DD/MM/YYYY)", 120),)),
        ),
    ),
    FormSpec(
        filename="harborline_tax_residence.pdf",
        provider="Harborline Pension Trust",
        title="Change of Address and Tax Residence Self-Certification",
        reference="HPT-TRS-07",
        sections=(
            Section(
                "1. Member",
                (
                    Text("member_number", "Member number", 140),
                    Text("full_name", "Full name", max_len=40),
                    Text("birth_date", "Date of birth (DD/MM/YYYY)", 120),
                ),
            ),
            Section(
                "2. New address",
                (
                    Text("address_street", "Street and number"),
                    Text("address_postcode", "Postcode", 90),
                    Text("address_city", "City", 180),
                    Text("address_country", "Country", 180),
                    Text("effective_date", "Effective from (DD/MM/YYYY)", 120),
                ),
            ),
            Section(
                "3. Tax residence",
                (
                    Text("tax_country", "Country of tax residence", 180),
                    Text("tin", "Tax identification number (if resident abroad)", 180),
                    Checkbox("us_person", "I am a US person for tax purposes"),
                    Text("us_tin", "US taxpayer number (US persons only)", 180),
                ),
            ),
            Section(
                "4. Declaration (completed by the member, never by automation)",
                (Checkbox("declaration_accepted", "I confirm the information above is correct"),),
            ),
        ),
    ),
)


def build_form(spec: FormSpec, out_dir: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / spec.filename
    width, height = A4
    c = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    c.setTitle(f"{spec.provider}: {spec.title}")
    c.setAuthor("form-fill-engine sample generator")
    form = c.acroForm

    c.setFillColor(colors.HexColor("#1f3a5f"))
    c.rect(0, height - 70, width, 70, stroke=0, fill=1)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(40, height - 32, spec.provider)
    c.setFont("Helvetica", 11)
    c.drawString(40, height - 52, spec.title)
    c.drawRightString(width - 40, height - 32, f"Form {spec.reference}")
    c.setFillColor(colors.HexColor("#b03a2e"))
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, height - 84, "FICTIONAL SAMPLE FORM. Not a real organisation or document.")

    y = height - 110
    label_x, field_x = 40, 250
    for section in spec.sections:
        c.setFillColor(colors.HexColor("#1f3a5f"))
        c.setFont("Helvetica-Bold", 10)
        c.drawString(label_x, y, section.heading)
        y -= 22
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 9)
        for item in section.items:
            c.drawString(label_x, y + 5, item.label)
            if isinstance(item, Text):
                kwargs = {"maxlen": item.max_len} if item.max_len else {}
                form.textfield(
                    name=item.name,
                    x=field_x,
                    y=y,
                    width=item.width,
                    height=16,
                    fontSize=9,
                    borderWidth=0.5,
                    borderColor=colors.grey,
                    fillColor=colors.HexColor("#f4f7fb"),
                    **kwargs,
                )
            elif isinstance(item, Checkbox):
                form.checkbox(name=item.name, x=field_x, y=y, size=13, buttonStyle="check")
            else:
                x = field_x
                for option in item.options:
                    form.radio(name=item.name, value=option, x=x, y=y, size=13, selected=False)
                    c.drawString(x + 17, y + 3, option)
                    x += 90
            y -= 24
        y -= 8
    c.setFont("Helvetica", 7)
    c.setFillColor(colors.grey)
    c.drawString(40, 30, "Generated by form-fill-engine/scripts/generate_sample_forms.py")
    c.showPage()
    c.save()
    return path


def build_all(out_dir: Path | str) -> list[Path]:
    return [build_form(spec, Path(out_dir)) for spec in FORMS]
