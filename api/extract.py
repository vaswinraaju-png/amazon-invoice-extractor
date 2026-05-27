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
    data["file"]         = filename
    data["order_id"]     = find(r"Order\s*(?:Number|ID|#)[:\s]+([A-Z0-9\-]+)", text)
    data["invoice_id"]   = find(r"Invoice\s*(?:Number|No\.?)\s*[:\s]+([A-Z0-9\-\/]+)", text)
    data["invoice_date"] = find(r"Invoice\s*Date[:\s]+([\d]{1,2}[\/\-\.]\w+[\/\-\.]\d{2,4})", text)

    # Customer
    data["customer_name"]   = find(r"Billing\s*Address\s*[:\-]?\s*\n\s*([^\n]+)", text)
    data["billing_address"] = find(r"Billing\s*Address\s*[:\-]?\s*\n[^\n]+\n([^\n]+)", text)
    data["billing_city"]    = find(r"(?:,\s*)([A-Za-z\s]+?)\s*[-–]\s*\d{6}", text)
    data["billing_state"]   = find(r"(?:State[:\s]+)([A-Za-z\s]+?)(?:\n|,|\d)", text)
    data["billing_pincode"] = find(r"\b(\d{6})\b", text)

    # Shipping
    data["shipping_name"]    = find(r"Shipping\s*Address\s*[:\-]?\s*\n\s*([^\n]+)", text)
    data["shipping_address"] = find(r"Shipping\s*Address\s*[:\-]?\s*\n[^\n]+\n([^\n]+)", text)
    data["shipping_city"]    = find(r"Shipping.*?\n.*?\n.*?(?:,\s*)([A-Za-z\s]+?)\s*[-–]\s*\d{6}", text)
    data["shipping_pincode"] = find(r"Shipping.*?(\d{6})", text)

    # Product
    data["product_name"] = find(r"(?:Description|Product\s*Name)[:\s]+([^\n]+)", text)
    data["sku"]          = find(r"SKU[:\s]+([A-Z0-9\-]+)", text)
    data["asin"]         = find(r"ASIN[:\s]+([A-Z0-9]{10})", text)
    data["qty"]          = find(r"(?:Qty|Quantity)[:\s]+(\d+)", text)
    data["unit_price"]   = find(r"Unit\s*Price[:\s]+[₹Rs\.]*\s*([\d,]+\.?\d*)", text)

    # Financials
    data["taxable_amount"] = find(r"(?:Taxable\s*Amount|Sub.?total)[:\s]+[₹Rs\.]*\s*([\d,]+\.?\d*)", text)
    data["cgst"]           = find(r"CGST[^₹\d]*([\d,]+\.?\d*)", text)
    data["sgst"]           = find(r"SGST[^₹\d]*([\d,]+\.?\d*)", text)
    data["igst"]           = find(r"IGST[^₹\d]*([\d,]+\.?\d*)", text)
    data["total_amount"]   = find(r"(?:Grand\s*Total|Total\s*Amount)[:\s]+[₹Rs\.]*\s*([\d,]+\.?\d*)", text)
    data["payment_mode"]   = find(r"Payment\s*(?:Mode|Method)[:\s]+([^\n]+)", text)

    # GST
    data["buyer_gstin"]  = find(r"Buyer\s*GSTIN[:\s]+([0-9A-Z]{15})", text)
    data["seller_gstin"] = find(r"(?:Seller|Supplier)\s*GSTIN[:\s]+([0-9A-Z]{15})", text)

    return data


# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.post("/api/extract")
async def extract(files: list[UploadFile] = File(...)):
    results  = []
    errors   = []

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
