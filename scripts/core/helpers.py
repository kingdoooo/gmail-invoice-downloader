"""
Helper functions for AI Agent Assistant

These functions are separated from the main agent module
to allow testing without Strands SDK dependencies.
"""

from typing import Dict, Any, List, Optional
import re
from datetime import datetime


# =============================================================================
# City Extraction Functions
# =============================================================================

def extract_city_from_tax_id(tax_id: Optional[str]) -> Optional[str]:
    """
    Extract city from 18-digit unified social credit code.
    Delegates to utils.location for the canonical city code mapping.
    """
    from utils.location import extract_city_from_tax_id as _extract
    return _extract(tax_id)


def match_city(city1: Optional[str], city2: Optional[str]) -> bool:
    """
    Match two city names, handling variations like "杭州" vs "杭州市".
    Delegates to utils.matching for the canonical implementation.
    """
    from utils.matching import match_city as _match
    return _match(city1, city2)


def extract_hotel_keywords(hotel_name: Optional[str]) -> List[str]:
    """
    Extract keywords from hotel name for matching.
    
    Args:
        hotel_name: Hotel name string
    
    Returns:
        List of keywords
    """
    if not hotel_name:
        return []
    
    keywords = []
    
    # Brand keywords
    brands = ['万豪', '希尔顿', '洲际', '喜来登', '香格里拉', '凯悦',
              'Marriott', 'Hilton', 'InterContinental', 'Sheraton', 'Hyatt']
    
    for brand in brands:
        if brand.lower() in hotel_name.lower():
            keywords.append(brand)
    
    return keywords


# =============================================================================
# Validation Functions
# =============================================================================

def is_valid_date(date_str: str) -> bool:
    """
    Check if date string is valid YYYY-MM-DD format.
    
    Args:
        date_str: Date string to validate
    
    Returns:
        True if valid date format
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return False
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
        return True
    except ValueError:
        return False


def validate_expense(expense: Dict[str, Any], index: int) -> Dict[str, Any]:
    """
    Validate a single expense entry.
    
    Args:
        expense: Expense data dictionary
        index: Index of expense in list
    
    Returns:
        Dict with 'valid' boolean and 'errors' list
    """
    errors = []
    required_fields = ['transactionDate', 'transactionAmount', 'category']
    
    for field in required_fields:
        if not expense.get(field):
            errors.append({
                'index': index,
                'field': field,
                'message': f'缺少必填字段: {field}'
            })
    
    # Validate amount is positive
    amount = expense.get('transactionAmount', 0)
    if amount <= 0:
        errors.append({
            'index': index,
            'field': 'transactionAmount',
            'message': '金额必须大于0'
        })
    
    # Validate date format
    date = expense.get('transactionDate', '')
    if date and not is_valid_date(date):
        errors.append({
            'index': index,
            'field': 'transactionDate',
            'message': '日期格式无效，应为 YYYY-MM-DD'
        })
    
    return {
        'valid': len(errors) == 0,
        'errors': errors
    }


# =============================================================================
# Mapping Functions
# =============================================================================

def get_meal_type_mapping(meal_type: Optional[str]) -> Optional[str]:
    """
    Map meal type to Concur custom2 field value.
    
    Args:
        meal_type: Meal type string (早餐/中餐/晚餐/咖啡)
    
    Returns:
        Concur custom2 field value or None
    """
    mapping = {
        "早餐": "4444D2B064C93D48AEBAC07BE42F2192",
        "中餐": "5102075A4C673641B64D6BA1B3A65C14",
        "晚餐": "BA79073C46BE1B458F12CA0463E5C524",
        "咖啡": "A1635C9199616949AA3091CF4073CAE1"
    }
    return mapping.get(meal_type)


def get_taxi_subtype_mapping(category: str) -> str:
    """
    Map taxi category to Concur custom14 field value.
    
    Args:
        category: Category string (TAXI or TOLLS)
    
    Returns:
        Concur custom14 field value
    """
    if category == 'TOLLS':
        return "675039494226AC4EB88B08A0A4BB6089"  # Tolls/Road Charges
    return "79B4E05DD42EC241B61CDE6AC5B428D0"  # Taxi - Other Merchant


# =============================================================================
# Business Purpose Generation
# =============================================================================

def generate_business_purpose(expense: Dict[str, Any]) -> str:
    """
    Generate business purpose string based on expense type.
    
    Args:
        expense: Expense data dictionary
    
    Returns:
        Business purpose string
    """
    category = expense.get('category', '')
    date = expense.get('transactionDate', '')
    
    if category == 'MOBILE':
        # Format: YYYYMM话费报销
        if date:
            return f"{date[:7].replace('-', '')}话费报销"
        return "话费报销"
    
    return ""


# =============================================================================
# Summary Generation
# =============================================================================

def generate_processing_summary(invoices: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Generate summary of processed invoices by category.
    
    Args:
        invoices: List of processed invoice data
    
    Returns:
        Summary dictionary
    """
    summary = {
        'total': len(invoices),
        'byCategory': {},
        'matched': 0,
        'unmatched': 0
    }
    
    for invoice in invoices:
        category = invoice.get('category', 'UNKNOWN')
        summary['byCategory'][category] = summary['byCategory'].get(category, 0) + 1
        
        # Check for key presence (not truthiness) since matched objects could be empty dicts
        if 'matchedReceipt' in invoice or 'matchedFolio' in invoice:
            summary['matched'] += 1
        elif category not in ('RIDEHAILING_RECEIPT', 'HOTEL_RECEIPT'):
            summary['unmatched'] += 1
    
    return summary


def generate_expense_summary(expenses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Generate summary of prepared expenses.
    
    Args:
        expenses: List of expense data
    
    Returns:
        Summary dictionary
    """
    summary = {
        'total': len(expenses),
        'valid': sum(1 for e in expenses if not e.get('hasErrors')),
        'withErrors': sum(1 for e in expenses if e.get('hasErrors')),
        'totalAmount': sum(e.get('transactionAmount', 0) for e in expenses if not e.get('hasErrors')),
        'byType': {}
    }
    
    for expense in expenses:
        exp_type = expense.get('category', 'UNKNOWN')
        summary['byType'][exp_type] = summary['byType'].get(exp_type, 0) + 1
    
    return summary


# =============================================================================
# Expense Formatting
# =============================================================================

def format_expense_for_concur(expense: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format expense data for Concur GraphQL mutation.
    
    Args:
        expense: Expense data dictionary
    
    Returns:
        Formatted expense data for Concur
    """
    # Map category to Concur expense type
    expense_type_mapping = {
        'MEAL': '01221',
        'RIDEHAILING': '01263',
        'TAXI': '01263',
        'TOLLS': '01263',
        'HOTEL': 'LODNG',
        'MOBILE': 'CELPH'
    }
    
    category = expense.get('category', 'UNKNOWN')
    
    formatted = {
        'expenseType': expense_type_mapping.get(category, '01221'),
        'transactionDate': expense.get('transactionDate'),
        'transactionAmount': expense.get('transactionAmount'),
        'vendorName': expense.get('vendorName'),
        'locationCity': expense.get('locationCity'),
        'businessPurpose': generate_business_purpose(expense),
        's3Key': expense.get('s3Key'),
        'mergedPdfKey': expense.get('mergedPdfKey'),
        'category': category
    }
    
    # Add category-specific fields
    if category == 'MEAL':
        formatted['mealType'] = expense.get('mealType')
        formatted['custom2'] = get_meal_type_mapping(expense.get('mealType'))
    
    elif category in ('TAXI', 'TOLLS'):
        formatted['taxiSubType'] = 'tolls' if category == 'TOLLS' else 'taxi'
        formatted['custom14'] = get_taxi_subtype_mapping(category)
    
    elif category == 'HOTEL':
        formatted['hotelCheckinDate'] = expense.get('checkInDate')
        formatted['hotelCheckoutDate'] = expense.get('checkOutDate')
        formatted['hasVAT'] = True
    
    elif category == 'MOBILE':
        formatted['billingPeriod'] = expense.get('billingPeriod')
        formatted['phoneNumber'] = expense.get('phoneNumber')
    
    return formatted
