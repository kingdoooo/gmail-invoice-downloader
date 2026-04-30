"""
Invoice Matching Module

Migrated from agent-processor Lambda.
Provides code-based matching logic for:
- Ride-hailing invoices and trip receipts
- Hotel invoices and folios
"""

import re
from typing import Dict, Any, List, Optional

from .location import extract_city_from_tax_id


# =============================================================================
# Constants
# =============================================================================

# Amount matching tolerance (0.01)
AMOUNT_TOLERANCE = 0.01

# Hotel brand keywords for name matching
HOTEL_BRAND_KEYWORDS = [
    # Chinese brands
    '万豪', '希尔顿', '洲际', '喜来登', '香格里拉', '凯悦', '威斯汀',
    '丽思卡尔顿', '瑞吉', '华尔道夫', '康莱德', '艾美', '索菲特',
    '铂尔曼', '美居', '诺富特', '宜必思', '锦江', '如家', '汉庭',
    # English brands
    'Marriott', 'Hilton', 'InterContinental', 'IHG', 'Sheraton', 'Westin',
    'Hyatt', 'Shangri-La', 'Ritz-Carlton', 'Waldorf', 'Conrad', 'Le Meridien',
    'Sofitel', 'Pullman', 'Mercure', 'Novotel', 'Ibis', 'Four Seasons',
    'Park Hyatt', 'Grand Hyatt', 'St. Regis', 'W Hotel', 'Renaissance',
]

# Chinese to English brand mapping
HOTEL_BRAND_MAPPING = {
    '万豪': ['Marriott', 'Ritz-Carlton', 'Renaissance'],
    '希尔顿': ['Hilton', 'Waldorf', 'Conrad'],
    '洲际': ['InterContinental', 'IHG', 'Holiday Inn'],
    '喜来登': ['Sheraton'],
    '香格里拉': ['Shangri-La'],
    '凯悦': ['Hyatt', 'Park Hyatt', 'Grand Hyatt'],
    '威斯汀': ['Westin'],
    '丽思卡尔顿': ['Ritz-Carlton'],
    '瑞吉': ['St. Regis'],
    '华尔道夫': ['Waldorf'],
    '康莱德': ['Conrad'],
    '艾美': ['Le Meridien'],
    '索菲特': ['Sofitel'],
    '铂尔曼': ['Pullman'],
    '美居': ['Mercure'],
    '诺富特': ['Novotel'],
    '宜必思': ['Ibis'],
}


# =============================================================================
# Amount Matching
# =============================================================================

def is_amount_match(amount1: Optional[float], amount2: Optional[float]) -> bool:
    """
    Check if two amounts match within tolerance.

    Args:
        amount1: First amount
        amount2: Second amount

    Returns:
        True if amounts match within 0.01 tolerance
    """
    if amount1 is None or amount2 is None:
        return False

    return abs(amount1 - amount2) < AMOUNT_TOLERANCE


# =============================================================================
# City Matching
# =============================================================================

def match_city(city1: Optional[str], city2: Optional[str]) -> bool:
    """
    Check if two city names match, handling '市' suffix.

    Args:
        city1: First city name
        city2: Second city name

    Returns:
        True if cities match
    """
    if not city1 or not city2:
        return False

    # Normalize: remove '市' suffix
    c1 = city1.rstrip('市')
    c2 = city2.rstrip('市')

    return c1 == c2


# =============================================================================
# File Number Extraction
# =============================================================================

def extract_file_number(filename: Optional[str]) -> int:
    """
    Extract sequence number from filename for matching disambiguation.

    Supports formats:
    - Didi: '滴滴电子发票 (1).pdf' -> 1
    - Didi Chinese brackets: '滴滴电子发票（1）.pdf' -> 1
    - Gaode: '高德【北京-上海】.pdf' -> hash-based number (1000000+)

    Args:
        filename: File name or S3 key

    Returns:
        Extracted number, or 0 if no pattern found
    """
    if not filename:
        return 0

    # Extract just the filename from S3 key
    base_filename = filename.split('/')[-1]

    # Pattern 1: (N) - English parentheses
    match = re.search(r'\((\d+)\)', base_filename)
    if match:
        return int(match.group(1))

    # Pattern 2: （N） - Chinese parentheses
    match = re.search(r'（(\d+)）', base_filename)
    if match:
        return int(match.group(1))

    # Pattern 3: 【content】 - Gaode format (hash-based)
    match = re.search(r'【(.+?)】', base_filename)
    if match:
        # Use hash to generate a consistent high number
        content = match.group(1)
        return 1000000 + (hash(content) % 1000000)

    return 0


# =============================================================================
# Hotel Keyword Extraction
# =============================================================================

def extract_hotel_keywords(hotel_name: Optional[str]) -> List[str]:
    """
    Extract hotel brand keywords from hotel name for matching.

    Returns both the original keyword and its translations for cross-language matching.

    Args:
        hotel_name: Hotel name string

    Returns:
        List of matched brand keywords (including translations)
    """
    if not hotel_name:
        return []

    keywords = []
    name_lower = hotel_name.lower()

    for brand in HOTEL_BRAND_KEYWORDS:
        if brand.lower() in name_lower:
            keywords.append(brand)
            # Add English translations for Chinese brands
            if brand in HOTEL_BRAND_MAPPING:
                keywords.extend(HOTEL_BRAND_MAPPING[brand])

    return keywords


# =============================================================================
# Ride-Hailing Matching
# =============================================================================

def match_ride_hailing_pairs(
    invoices: List[Dict[str, Any]],
    receipts: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Match ride-hailing invoices with trip receipts.

    Matching rule:
    - Match by amount only (within 0.01 tolerance)
    - NO city matching (invoice taxId = platform address, receipt = trip location)
    - Use filename number as tiebreaker for multiple same-amount pairs

    Args:
        invoices: List of ride-hailing invoices (transactionAmount, s3Key)
        receipts: List of trip receipts (totalAmount, s3Key)

    Returns:
        Dict with:
        - matched: List of {invoice, receipt} pairs
        - unmatched_invoices: List of invoices without match
        - unmatched_receipts: List of receipts without match
    """
    matched = []
    unmatched_invoices = []
    used_receipt_indices = set()

    # Pre-extract file numbers for receipts
    receipt_numbers = [extract_file_number(r.get('s3Key')) for r in receipts]

    for invoice in invoices:
        invoice_amount = invoice.get('transactionAmount')
        invoice_number = extract_file_number(invoice.get('s3Key'))
        print(f"[RH Match] Processing invoice: amount={invoice_amount}, s3Key={invoice.get('s3Key')}")

        # Find all receipts with matching amount
        candidates = []
        for i, receipt in enumerate(receipts):
            if i in used_receipt_indices:
                continue

            receipt_amount = receipt.get('totalAmount')
            print(f"[RH Match]   Comparing with receipt #{i}: totalAmount={receipt_amount}, transactionAmount={receipt.get('transactionAmount')}, s3Key={receipt.get('s3Key')}")
            if is_amount_match(invoice_amount, receipt_amount):
                candidates.append((i, receipt, receipt_numbers[i]))
                print(f"[RH Match]   -> MATCH FOUND!")

        if not candidates:
            print(f"[RH Match]   -> No candidates found, adding to unmatched")
            unmatched_invoices.append(invoice)
            continue

        # Select best match using filename number as tiebreaker
        if len(candidates) == 1:
            best_idx, best_receipt, _ = candidates[0]
        else:
            # Sort by absolute difference in file number
            candidates.sort(key=lambda x: abs(x[2] - invoice_number))
            best_idx, best_receipt, _ = candidates[0]

        matched.append({
            'invoice': invoice,
            'receipt': best_receipt,
            'match_type': 'amount'
        })
        used_receipt_indices.add(best_idx)

    # Collect unmatched receipts
    unmatched_receipts = [
        r for i, r in enumerate(receipts)
        if i not in used_receipt_indices
    ]

    return {
        'matched': matched,
        'unmatched_invoices': unmatched_invoices,
        'unmatched_receipts': unmatched_receipts
    }


# =============================================================================
# Hotel Matching
# =============================================================================

def match_hotel_pairs(
    invoices: List[Dict[str, Any]],
    folios: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Match hotel invoices with folios.

    Matching priority:
    P1. remark == confirmationNo or remark in internalCodes
    P2. transactionDate == checkOutDate/departureDate AND amount match

    Args:
        invoices: List of hotel invoices
        folios: List of hotel folios

    Returns:
        Dict with:
        - matched: List of {invoice, folio, match_type} pairs
        - unmatched_invoices: List of invoices without match
        - unmatched_folios: List of folios without match
    """
    matched = []
    unmatched_invoices = []
    used_folio_indices = set()

    for invoice in invoices:
        invoice_amount = invoice.get('transactionAmount')
        invoice_date = invoice.get('transactionDate')
        invoice_remark = invoice.get('remark')
        print(f"[Hotel Match] Processing invoice: amount={invoice_amount}, date={invoice_date}, remark={invoice_remark}, s3Key={invoice.get('s3Key')}")

        best_match = None
        best_match_idx = None
        best_match_type = None

        for i, folio in enumerate(folios):
            if i in used_folio_indices:
                continue

            # Get folio amount: prefer balance, fallback to transactionAmount
            folio_amount = folio.get('balance') or folio.get('transactionAmount')
            # Get checkout date: support multiple field names
            folio_checkout = (
                folio.get('checkOutDate') or
                folio.get('departureDate') or
                folio.get('checkoutDate')
            )
            folio_confirmation = folio.get('confirmationNo')
            folio_internal_codes = folio.get('internalCodes', []) or []
            print(f"[Hotel Match]   Comparing with folio #{i}: amount={folio_amount}, checkOut={folio_checkout}, confirmNo={folio_confirmation}, s3Key={folio.get('s3Key')}")

            # P1: Remark match (highest priority)
            if invoice_remark:
                if invoice_remark == folio_confirmation:
                    best_match = folio
                    best_match_idx = i
                    best_match_type = 'remark'
                    print(f"[Hotel Match]   -> P1 MATCH: remark={invoice_remark} matches confirmationNo")
                    break  # P1 is highest priority, stop searching

                if folio_internal_codes and invoice_remark in folio_internal_codes:
                    best_match = folio
                    best_match_idx = i
                    best_match_type = 'remark'
                    print(f"[Hotel Match]   -> P1 MATCH: remark={invoice_remark} found in internalCodes")
                    break

            # P2: Date + amount match (simplified)
            if not best_match and invoice_date and folio_checkout == invoice_date:
                if is_amount_match(invoice_amount, folio_amount):
                    best_match = folio
                    best_match_idx = i
                    best_match_type = 'date_amount'
                    print(f"[Hotel Match]   -> P2 MATCH: date={invoice_date} and amount={invoice_amount}")
                    # Don't break - continue looking for P1

        if best_match:
            matched.append({
                'invoice': invoice,
                'folio': best_match,
                'match_type': best_match_type
            })
            used_folio_indices.add(best_match_idx)
            print(f"[Hotel Match]   -> Final match: type={best_match_type}, folio_s3Key={best_match.get('s3Key')}")
        else:
            unmatched_invoices.append(invoice)
            print(f"[Hotel Match]   -> No match found, adding to unmatched")

    # Collect unmatched folios
    unmatched_folios = [
        f for i, f in enumerate(folios)
        if i not in used_folio_indices
    ]

    return {
        'matched': matched,
        'unmatched_invoices': unmatched_invoices,
        'unmatched_folios': unmatched_folios
    }
