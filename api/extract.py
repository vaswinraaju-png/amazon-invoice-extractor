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

def find(pattern, text, group=1, default=""):
    m = re.search(pattern, text, re.IGNORECASE)
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

    data = {}

    # ── File & Order ─────────────────────────────────────────────────────────
    data["file"]       = filename
    data["order_id"]   = find(r"Order\s*(?:Number|ID|#)[:\s]+([A-Z0-9\-]+)", text)
    data["order_date"] = find(r"Order\s*Date[:\s]+([\d]{1,2}[\/\-\.]\w+[\/\-\.]\d{2,4})", text)
    data["order_time"] = find(r"Order\s*(?:Date|Time).*?(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|IST)?)", text)
    data["invoice_id"] = find(r"Invoice\s*(?:Number|No\.?)\s*[:\s]+([A-Z0-9\-\/]+)", text)

    # ── Customer Name ────────────────────────────────────────────────────────
    # Try billing address block first line
    data["customer_name"] = (
        find(r"(?:Billing\s*Address|Bill\s*To)\s*[:\-]?\s*\n\s*([^\n]+)", text)
        or find(r"(?:Sold\s*To|Customer\s*Name)[:\s]+([^\n]+)", text)
    )

    # ── Email ────────────────────────────────────────────────────────────────
    data["email"] = find(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text, group=0)

    # ── Mobile ───────────────────────────────────────────────────────────────
    data["mobile"] = (
        find(r"(?:Mobile|Phone|Contact)[:\s]+(\+?[\d\s\-]{10,14})", text)
        or find(r"(?<!\d)(\+91[\s\-]?\d{10}|\b[6-9]\d{9}\b)", text)
    )

    # ── Full Address (billing) ───────────────────────────────────────────────
    # Capture 3 lines after billing address name
    billing_match = re.search(
        r"(?:Billing\s*Address|Bill\s*To)\s*[:\-]?\s*\n\s*[^\n]+\n([^\n]+)\n([^\n]+)\n?([^\n]*)",
        text, re.IGNORECASE
    )
    if billing_match:
        parts = [g.strip() for g in billing_match.groups() if g and g.strip()]
        data["address"] = ", ".join(parts)
    else:
        data["address"] = find(r"(?:Address)[:\s]+([^\n]+)", text)

    data["city"]    = find(r"(?:,\s*)([A-Za-z\s]+?)\s*[-–]\s*\d{6}", text)
    data["state"]   = find(r"(?:State[:\s]+)([A-Za-z\s]+?)(?:\n|,|\d)", text)
    data["pincode"] = find(r"\b(\d{6})\b", text)

    # ── Product ──────────────────────────────────────────────────────────────
    data["product"] = (
        find(r"(?:Description|Product\s*Name|Item)[:\s]+([^\n]+)", text)
        or find(r"(?:^\s*\d+\.\s*)([A-Za-z].*?)(?:\s{2,}|\t)", text)
    )
    data["sku"]      = find(r"SKU[:\s]+([A-Z0-9\-]+)", text)
    data["asin"]     = find(r"ASIN[:\s]+([A-Z0-9]{10})", text)
    data["quantity"] = find(r"(?:Qty|Quantity)[:\s]+(\d+)", text)
    data["unit_price"] = find(r"Unit\s*Price[:\s]+[₹Rs\.]*\s*([\d,]+\.?\d*)", text)

    # ── Financials ───────────────────────────────────────────────────────────
    data["taxable_amount"] = find(r"(?:Taxable\s*Amount|Sub.?total)[:\s]+[₹Rs\.]*\s*([\d,]+\.?\d*)", text)
    data["cgst"]           = find(r"CGST[^₹\d]*([\d,]+\.?\d*)", text)
    data["sgst"]           = find(r"SGST[^₹\d]*([\d,]+\.?\d*)", text)
    data["igst"]           = find(r"IGST[^₹\d]*([\d,]+\.?\d*)", text)
    # Total GST = CGST + SGST + IGST
    try:
        cgst = float(data["cgst"].replace(",","")) if data["cgst"] else 0
        sgst = float(data["sgst"].replace(",","")) if data["sgst"] else 0
        igst = float(data["igst"].replace(",","")) if data["igst"] else 0
        total_gst = cgst + sgst + igst
        data["total_gst"] = f"{total_gst:.2f}" if total_gst else ""
    except Exception:
        data["total_gst"] = ""

    data["order_value"]  = find(r"(?:Grand\s*Total|Total\s*Amount)[:\s]+[₹Rs\.]*\s*([\d,]+\.?\d*)", text)
    data["payment_mode"] = find(r"Payment\s*(?:Mode|Method)[:\s]+([^\n]+)", text)

    # ── GST Numbers ──────────────────────────────────────────────────────────
    data["buyer_gstin"]  = find(r"Buyer\s*GSTIN[:\s]+([0-9A-Z]{15})", text)
    data["seller_gstin"] = find(r"(?:Seller|Supplier)\s*GSTIN[:\s]+([0-9A-Z]{15})", text)

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
