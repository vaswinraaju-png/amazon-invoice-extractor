from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import pdfplumber
import re
import io

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def find(pattern, text, group=1, default="", flags=0):
    m = re.search(pattern, text, re.IGNORECASE | flags)
    return m.group(group).strip() if m else default


def extract_text_from_bytes(file_bytes: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception:
        return ""


def parse_invoice(filename: str, file_bytes: bytes) -> dict:
    text = extract_text_from_bytes(file_bytes)

    if not text.strip():
        return {"file": filename, "error": "Could not extract text — may be a scanned PDF"}

    lines = text.split("\n")
    data  = {}
    data["file"] = filename

    # ── Order Info ───────────────────────────────────────────────────────────
    data["order_id"]     = find(r"Order Number:\s*([A-Z0-9\-]+)", text)
    data["order_date"]   = find(r"Order Date:\s*([\d.\/\-]+)", text)
    data["invoice_id"]   = find(r"Invoice Number\s*:\s*([A-Z0-9\-]+)", text)
    data["invoice_date"] = find(r"Invoice Date\s*:\s*([\d.\/\-]+)", text)

    # ── Time from payment block ──────────────────────────────────────────────
    dt = re.search(r"Date & Time:\s*([\d/]+),\s*([\d:]+)", text)
    data["order_time"] = dt.group(2).strip() if dt else ""

    # ── Customer Name ────────────────────────────────────────────────────────
    # PDF renders two columns on one line:
    # "Diabliss Consumer Products Pvt Ltd   Syeda Fatima"
    # Strip seller name (everything up to Pvt Ltd / Ltd)
    for i, l in enumerate(lines):
        if "Billing Address" in l:
            name_line = lines[i + 1] if i + 1 < len(lines) else ""
            name = re.sub(
                r"^.*?(?:Pvt\.?\s*Ltd\.?|Ltd\.?)\s+", "",
                name_line, flags=re.IGNORECASE
            ).strip()
            data["customer_name"] = name if name else name_line.strip()
            break
    else:
        data["customer_name"] = ""

    # ── Email & Mobile ───────────────────────────────────────────────────────
    # Amazon invoices don't expose these — kept for completeness
    data["email"]  = find(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text, group=0)
    data["mobile"] = find(r"(?:Mobile|Phone|Contact)[:\s]+(\+?[\d\s\-]{10,14})", text) or \
                     find(r"(?<!\d)(\+91[\s\-]?\d{10}|\b[6-9]\d{9}\b)", text)

    # ── Address (billing) ────────────────────────────────────────────────────
    # Line layout (two-column PDF):
    #   i+1: "Seller Name     Customer Name"
    #   i+2: "* Seller St     Customer Street"
    #   i+3: "Seller City,ST  CUSTOMER CITY, STATE, PIN"
    for i, l in enumerate(lines):
        if "Billing Address" in l:
            street_line = lines[i + 2] if i + 2 < len(lines) else ""
            combo_line  = lines[i + 3] if i + 3 < len(lines) else ""
            # Street: right-column part (after the * seller street)
            # The billing street is the left-most entry on line i+2
            data["address"] = street_line.lstrip("* ").strip()
            # City/State/PIN: pattern "CITY, STATE, 6DIGITS" appears in combo_line
            csp = re.search(
                r"([A-Z]{2}[A-Z ]+),\s*([A-Z]{2}[A-Z ]+),\s*(\d{6})",
                combo_line
            )
            if csp:
                data["city"]    = csp.group(1).strip()
                data["state"]   = csp.group(2).strip()
                data["pincode"] = csp.group(3).strip()
            else:
                data["city"]    = ""
                data["state"]   = find(r"Place of supply:\s*([A-Z]+)", text)
                data["pincode"] = find(r"\b(\d{6})\b", combo_line)
            break
    else:
        data["address"] = ""
        data["city"]    = ""
        data["state"]   = find(r"Place of supply:\s*([A-Z]+)", text)
        data["pincode"] = find(r"\b(\d{6})\b", text)

    # ── Product ──────────────────────────────────────────────────────────────
    # First item line starts with "1 ProductName..."
    prod_m = re.search(r"^1\s+((?:(?!₹).)+)", text, re.MULTILINE)
    if prod_m:
        raw = prod_m.group(1).strip()
        raw = re.sub(r"\s*\|\s*B[A-Z0-9]{9}.*", "", raw)  # cut at ASIN
        raw = re.sub(r"\s+", " ", raw).strip()
        data["product"] = raw
    else:
        data["product"] = ""

    # ── ASIN ─────────────────────────────────────────────────────────────────
    data["asin"] = find(r"\b(B[A-Z0-9]{9})\b", text)

    # ── SKU: line before "HSN:" line ─────────────────────────────────────────
    sku_m = re.search(r"^([A-Z0-9\-]+)\s*\)\s*\nHSN:", text, re.MULTILINE)
    data["sku"] = sku_m.group(1).strip() if sku_m else ""

    # ── Quantity & Unit Price ─────────────────────────────────────────────────
    qty_price = re.search(r"₹([\d,]+\.?\d*)\s+(\d+)\s+₹[\d,]+\.?\d*", text)
    data["unit_price"] = qty_price.group(1).strip() if qty_price else ""
    data["quantity"]   = qty_price.group(2).strip() if qty_price else ""

    # ── Tax ──────────────────────────────────────────────────────────────────
    data["cgst"] = find(r"[\d.]+%CGST\s+₹([\d,]+\.?\d*)", text)
    data["sgst"] = find(r"[\d.]+%SGST\s+₹([\d,]+\.?\d*)", text)
    data["igst"] = find(r"[\d.]+%IGST\s+₹([\d,]+\.?\d*)", text)

    try:
        total_gst = (
            float(data["cgst"].replace(",", "") or 0) +
            float(data["sgst"].replace(",", "") or 0) +
            float(data["igst"].replace(",", "") or 0)
        )
        data["total_gst"] = f"{total_gst:.2f}" if total_gst else ""
    except Exception:
        data["total_gst"] = ""

    # ── Order Value & Payment Mode ────────────────────────────────────────────
    # Payment footer line: "TXN_ID hrs 289.00 UPI"
    pay_line = re.search(
        r"^[A-Za-z0-9]+\s+hrs\s+([\d.]+)\s+(\w+)\s*$",
        text, re.MULTILINE
    )
    data["order_value"]  = pay_line.group(1).strip() if pay_line else \
                           find(r"TOTAL:.*?₹[\d.]+₹([\d.]+)", text)
    data["payment_mode"] = pay_line.group(2).strip() if pay_line else ""

    # ── GST Numbers ──────────────────────────────────────────────────────────
    data["seller_gstin"] = find(r"GST Registration No:\s*([0-9A-Z]{15})", text)
    data["buyer_gstin"]  = find(r"Buyer\s*GSTIN[:\s]+([0-9A-Z]{15})", text)

    return data


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.post("/api/extract")
async def extract(files: list[UploadFile] = File(...)):
    results = []
    errors  = []

    for f in files:
        file_bytes = await f.read()
        row = parse_invoice(f.filename, file_bytes)
        if "error" in row:
            errors.append(row)
        else:
            results.append(row)

    return JSONResponse({
        "success": True,
        "count": len(results),
        "error_count": len(errors),
        "data": results,
        "errors": errors
    })


@app.get("/api/health")
async def health():
    return {"status": "ok"}
