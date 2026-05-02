"""
Invoice Classification Module

Migrated from agent-processor Lambda.
Provides code-based classification logic for invoice documents.

Classification Priority:
1. docType special document types + core field detection
2. invoiceCode 12-digit -> TAXI
3. serviceType + Chinese invoice detection
4. IGNORED (v5.7 — see MODIFIED block below)

MODIFIED for gmail-invoice-downloader v5.3:
- Removed COFFEE_KEYWORDS and MEAL_TYPES (non-deterministic random assignment
  was useful for Concur reimbursement, irrelevant for Gmail aggregation use case).
- Removed is_coffee_vendor() and detect_meal_type() functions.
- All meal-service invoices now classified as MEAL (no early/mid/late/coffee subtype).

MODIFIED for gmail-invoice-downloader v5.7:
- Fallthrough default changed from 'UNKNOWN' to 'IGNORED' (whitelist the three
  reimbursable formats; filter everything else).
- is_hotel_folio_by_doctype narrow gate tightened: now requires >=2 of
  {hotelName, confirmationNo, internalCodes, roomNumber}. balance
  deliberately excluded (SaaS Amount-due conflict); arrival/departure
  deliberately excluded (covered by fields path + prompt-layer rule).
- classify_invoice is confidence-blind by design — downstream validation
  flags from validate_ocr_plausibility are additive and do not feed
  classification. Any future upstream confidence-aware logic must update
  scripts/dev/replay_classify.py.
"""

import re
from typing import Dict, Any, Optional


# =============================================================================
# Tax ID Validation (for Chinese invoice detection)
# =============================================================================

def is_valid_tax_id_format(tax_id: str) -> bool:
    """
    Validate tax ID format (18-digit unified social credit code).

    Args:
        tax_id: Tax ID string to validate

    Returns:
        True if valid 18-digit format
    """
    if not tax_id:
        return False

    # Clean tax ID
    clean_tax_id = tax_id.replace(' ', '').replace('-', '').upper()

    # Must be 18 characters with alphanumeric
    if not re.match(r'^[0-9A-Z]{18}$', clean_tax_id):
        return False

    # City code part (characters 3-6) must be 4 digits
    city_code_part = clean_tax_id[2:6]
    if not city_code_part.isdigit():
        return False

    return True


# =============================================================================
# Classification Helper Functions
# =============================================================================

def is_hotel_folio_by_fields(invoice: Dict[str, Any]) -> bool:
    """
    Determine if document is a hotel folio by core fields (3-choose-2 rule).

    Core fields: roomNumber, arrivalDate/checkInDate, departureDate/checkOutDate
    If 2 or more of these fields exist, it's a hotel folio.

    Args:
        invoice: Invoice data dictionary

    Returns:
        True if document is a hotel folio
    """
    field_count = 0

    # Check roomNumber
    if invoice.get('roomNumber'):
        field_count += 1

    # Check arrival/check-in date
    if invoice.get('arrivalDate') or invoice.get('checkInDate'):
        field_count += 1

    # Check departure/check-out date
    if invoice.get('departureDate') or invoice.get('checkOutDate'):
        field_count += 1

    return field_count >= 2


def is_ridehailing_receipt(doc_type: Optional[str]) -> bool:
    """
    Determine if document is a ride-hailing trip receipt.

    Keywords: 行程单, 行程报销单, TRIP TABLE, 报销单

    Args:
        doc_type: Document type string

    Returns:
        True if document is a ridehailing receipt
    """
    if not doc_type:
        return False
    keywords = ['行程单', '行程报销单', 'TRIP TABLE', '报销单']
    return any(kw in doc_type for kw in keywords)


def is_taxi_invoice_by_doctype(doc_type: Optional[str]) -> bool:
    """
    Determine if document is a taxi invoice by docType.

    Args:
        doc_type: Document type string

    Returns:
        True if document is a taxi invoice
    """
    if not doc_type:
        return False
    return '出租' in doc_type


def is_train_ticket(doc_type: Optional[str]) -> bool:
    """
    Determine if document is a train ticket by docType.

    Keywords: 铁路, 火车票, 电子客票, 高铁票, 动车票

    Args:
        doc_type: Document type string

    Returns:
        True if document is a train ticket
    """
    if not doc_type:
        return False
    keywords = ['铁路', '火车', '客票', '高铁', '动车']
    doc_type_lower = doc_type.lower()
    return any(kw in doc_type_lower for kw in keywords)


def is_hotel_folio_by_doctype(doc_type: Optional[str]) -> bool:
    """
    Determine if document is a hotel folio by docType keywords.

    Keywords: Guest Folio, Folio, 宾客账单, 消费账单, INFORMATION INVOICE/BILL, Statement

    Args:
        doc_type: Document type string

    Returns:
        True if document is a hotel folio
    """
    if not doc_type:
        return False
    keywords = [
        'Guest Folio', 'Folio', '宾客账单', '消费账单',
        'INFORMATION INVOICE', 'INFORMATION BILL', 'Information Bill',
        'Statement', 'Guest Statement', '客人账单'
    ]
    doc_type_lower = doc_type.lower()
    return any(kw.lower() in doc_type_lower for kw in keywords)


def is_chinese_invoice_document(is_chinese_invoice: Optional[bool], tax_id: Optional[str]) -> bool:
    """
    Determine if document is a Chinese tax invoice.

    Priority:
    1. LLM-extracted isChineseInvoice field
    2. Valid 18-digit unified social credit code (using format validation)

    Args:
        is_chinese_invoice: LLM-extracted boolean field
        tax_id: Vendor tax ID

    Returns:
        True if document is a Chinese tax invoice
    """
    # Primary: LLM judgment
    if is_chinese_invoice is True:
        return True
    if is_chinese_invoice is False:
        return False

    # Fallback: Check for valid 18-digit tax ID format (not just length)
    if tax_id and is_valid_tax_id_format(tax_id):
        return True

    return False


def is_hotel_service(service_type: Optional[str]) -> bool:
    """
    Determine if service type is hotel-related.

    Keywords: 住宿服务, 住宿, 房费

    Args:
        service_type: Service type string

    Returns:
        True if hotel service
    """
    if not service_type:
        return False
    keywords = ['住宿服务', '住宿', '房费']
    return any(kw in service_type for kw in keywords)


def is_ridehailing_service(service_type: Optional[str]) -> bool:
    """
    Determine if service type is ride-hailing related.

    Keywords: 运输服务, 客运服务费, 客运, 代订车, 代驾

    Args:
        service_type: Service type string

    Returns:
        True if ridehailing service
    """
    if not service_type:
        return False
    # Standard transport service
    if any(kw in service_type for kw in ['运输服务', '客运服务费', '客运']):
        return True
    # Travel service with car booking
    if '旅游服务' in service_type and '代订车' in service_type:
        return True
    # Designated driver service
    if '代驾' in service_type:
        return True
    return False


def is_meal_service(service_type: Optional[str]) -> bool:
    """
    Determine if service type is meal-related.

    Keywords: 餐饮服务, 餐饮

    Args:
        service_type: Service type string

    Returns:
        True if meal service
    """
    if not service_type:
        return False
    keywords = ['餐饮服务', '餐饮']
    return any(kw in service_type for kw in keywords)


def is_mobile_service(service_type: Optional[str]) -> bool:
    """
    Determine if service type is mobile/telecom-related.

    Keywords: 电信服务, 通信服务费, 通信费

    Args:
        service_type: Service type string

    Returns:
        True if mobile/telecom service
    """
    if not service_type:
        return False
    keywords = ['电信服务', '通信服务费', '通信费']
    return any(kw in service_type for kw in keywords)


def is_tolls_service(service_type: Optional[str], vendor_name: Optional[str]) -> bool:
    """
    Determine if service/vendor is tolls-related.

    Keywords: 过路费, 通行费, 高速

    Args:
        service_type: Service type string
        vendor_name: Vendor name string

    Returns:
        True if tolls service
    """
    keywords = ['过路费', '通行费', '高速']
    service_type = service_type or ''
    vendor_name = vendor_name or ''
    return any(kw in service_type or kw in vendor_name for kw in keywords)


# =============================================================================
# Main Classification Function
# =============================================================================

def classify_invoice(invoice: Dict[str, Any]) -> str:
    """
    Classify invoice based on docType, invoiceCode, serviceType, and isChineseInvoice.

    Classification Priority (per CLASSIFICATION_RULES.md):
    1. docType special document types + core field detection
    2. invoiceCode 12-digit -> TAXI
    3. serviceType + Chinese invoice detection
    4. IGNORED (v5.7: fallthrough renamed from UNKNOWN)

    Args:
        invoice: Extracted invoice data

    Returns:
        Invoice category string
    """
    service_type = invoice.get('serviceType', '') or ''
    doc_type = invoice.get('docType', '') or ''
    invoice_code = invoice.get('invoiceCode', '') or ''
    vendor_name = invoice.get('vendorName', '') or ''
    tax_id = invoice.get('vendorTaxId', '') or ''
    is_chinese_invoice = invoice.get('isChineseInvoice')

    category = None

    # ========== Priority 1: docType + Core Fields ==========

    # 1.1 Ride-hailing receipt (trip table)
    if is_ridehailing_receipt(doc_type):
        category = 'RIDEHAILING_RECEIPT'

    # 1.2 Taxi invoice by docType
    if not category and is_taxi_invoice_by_doctype(doc_type):
        category = 'TAXI'

    # 1.3 Train ticket by docType
    if not category and is_train_ticket(doc_type):
        category = 'TRAIN'

    # 1.4 Hotel folio by core fields (3-choose-2: roomNumber/arrivalDate/departureDate)
    if not category and is_hotel_folio_by_fields(invoice):
        category = 'HOTEL_FOLIO'

    # 1.4 Hotel folio by docType keywords — narrowed to >=2 of 4 fields
    # (v5.7): protects against Termius-style SaaS invoices where docType
    # hallucinates to "Statement" or "Invoice" but no hotel-domain
    # structural fields exist. balance deliberately excluded (SaaS
    # "Amount due" conflict); arrivalDate/departureDate excluded because
    # the fields path (is_hotel_folio_by_fields, 3-choose-2) already
    # covers them — re-adding here would widen attack surface against
    # subscription-range leakage.
    if not category and is_hotel_folio_by_doctype(doc_type):
        hotel_field_signals = sum([
            bool(invoice.get('hotelName')),
            bool(invoice.get('confirmationNo')),
            bool(invoice.get('internalCodes')),
            bool(invoice.get('roomNumber')),
        ])
        if hotel_field_signals >= 2:
            category = 'HOTEL_FOLIO'
        # else: fall through to IGNORED

    # ========== Priority 2: invoiceCode 12-digit -> TAXI ==========
    if not category and invoice_code and len(invoice_code) == 12 and invoice_code.isdigit():
        category = 'TAXI'

    # ========== Priority 3: serviceType + Chinese Invoice Detection ==========

    # 3.1 Hotel service -> HOTEL_INVOICE or HOTEL_FOLIO
    if not category and is_hotel_service(service_type):
        if is_chinese_invoice_document(is_chinese_invoice, tax_id):
            category = 'HOTEL_INVOICE'
        else:
            category = 'HOTEL_FOLIO'

    # 3.2 Ride-hailing service -> RIDEHAILING_INVOICE or RIDEHAILING_RECEIPT
    if not category and is_ridehailing_service(service_type):
        if is_chinese_invoice_document(is_chinese_invoice, tax_id):
            category = 'RIDEHAILING_INVOICE'
        else:
            category = 'RIDEHAILING_RECEIPT'

    # 3.3 Meal service -> MEAL
    if not category and is_meal_service(service_type):
        category = 'MEAL'

    # 3.4 Mobile/Telecom service -> MOBILE
    if not category and is_mobile_service(service_type):
        category = 'MOBILE'

    # 3.5 Tolls -> TOLLS
    if not category and is_tolls_service(service_type, vendor_name):
        category = 'TOLLS'

    # ========== Priority 4: IGNORED ==========
    # (v5.7) fallthrough renamed from UNKNOWN. Non-invoice / non-folio /
    # non-itinerary documents (SaaS subscriptions, marketing receipts,
    # bank statements) land here and are filtered out of CSV/zip/
    # missing.json.items[] in downstream units.
    if not category:
        category = 'IGNORED'

    return category


# NOTE: is_coffee_vendor() and detect_meal_type() removed in v5.3.
# Original random early/mid/late meal assignment was non-deterministic and
# served the Concur reimbursement UI which required a "meal type" field.
# In the Gmail aggregation use case, the information is meaningless noise.
# All meal-service invoices are now classified as MEAL without subtype.
