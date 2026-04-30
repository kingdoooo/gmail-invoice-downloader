"""
City Extraction and Location Module

Migrated from agent-processor Lambda.
Provides city extraction from tax IDs, invoice codes, and LLM-extracted fields.
"""

import re
from typing import Dict, Any, Optional
from datetime import datetime


# =============================================================================
# Constants
# =============================================================================

# Default city configuration
DEFAULT_CITY = "杭州"

# Complete city code mapping (100+ cities)
CITY_CODE_MAPPING = {
    # 北京市
    "北京": "1101",
    # 天津市
    "天津": "1201",
    # 河北省
    "石家庄": "1301", "唐山": "1302", "秦皇岛": "1303", "邯郸": "1304",
    "邢台": "1305", "保定": "1306", "张家口": "1307", "承德": "1308",
    "沧州": "1309", "廊坊": "1310", "衡水": "1311",
    # 山西省
    "太原": "1401", "大同": "1402", "阳泉": "1403", "长治": "1404",
    "晋城": "1405", "朔州": "1406", "晋中": "1407", "运城": "1408",
    "忻州": "1409", "临汾": "1410", "吕梁": "1411",
    # 内蒙古自治区
    "呼和浩特": "1501", "包头": "1502", "乌海": "1503", "赤峰": "1504",
    "通辽": "1505", "鄂尔多斯": "1506", "呼伦贝尔": "1507", "巴彦淖尔": "1508",
    "乌兰察布": "1509",
    # 辽宁省
    "沈阳": "2101", "大连": "2102", "鞍山": "2103", "抚顺": "2104",
    "本溪": "2105", "丹东": "2106", "锦州": "2107", "营口": "2108",
    "阜新": "2109", "辽阳": "2110", "盘锦": "2111", "铁岭": "2112",
    "朝阳": "2113", "葫芦岛": "2114",
    # 吉林省
    "长春": "2201", "吉林": "2202", "四平": "2203", "辽源": "2204",
    "通化": "2205", "白山": "2206", "松原": "2207", "白城": "2208",
    # 黑龙江省
    "哈尔滨": "2301", "齐齐哈尔": "2302", "鸡西": "2303", "鹤岗": "2304",
    "双鸭山": "2305", "大庆": "2306", "伊春": "2307", "佳木斯": "2308",
    "七台河": "2309", "牡丹江": "2310", "黑河": "2311", "绥化": "2312",
    "大兴安岭": "2327",
    # 上海市
    "上海": "3101",
    # 江苏省
    "南京": "3201", "无锡": "3202", "徐州": "3203", "常州": "3204",
    "苏州": "3205", "南通": "3206", "连云港": "3207", "淮安": "3208",
    "盐城": "3209", "扬州": "3210", "镇江": "3211", "泰州": "3212",
    "宿迁": "3213",
    # 浙江省
    "杭州": "3301", "宁波": "3302", "温州": "3303", "嘉兴": "3304",
    "湖州": "3305", "绍兴": "3306", "金华": "3307", "衢州": "3308",
    "舟山": "3309", "台州": "3310", "丽水": "3311",
    # 安徽省
    "合肥": "3401", "芜湖": "3402", "蚌埠": "3403", "淮南": "3404",
    "马鞍山": "3405", "淮北": "3406", "铜陵": "3407", "安庆": "3408",
    "黄山": "3410", "滁州": "3411", "阜阳": "3412", "宿州": "3413",
    "六安": "3415", "亳州": "3416", "池州": "3417", "宣城": "3418",
    # 福建省
    "福州": "3501", "厦门": "3502", "莆田": "3503", "三明": "3504",
    "泉州": "3505", "漳州": "3506", "南平": "3507", "龙岩": "3508",
    "宁德": "3509",
    # 江西省
    "南昌": "3601", "景德镇": "3602", "萍乡": "3603", "九江": "3604",
    "新余": "3605", "鹰潭": "3606", "赣州": "3607", "吉安": "3608",
    "宜春": "3609", "抚州": "3610", "上饶": "3611",
    # 山东省
    "济南": "3701", "青岛": "3702", "淄博": "3703", "枣庄": "3704",
    "东营": "3705", "烟台": "3706", "潍坊": "3707", "济宁": "3708",
    "泰安": "3709", "威海": "3710", "日照": "3711", "临沂": "3713",
    "德州": "3714", "聊城": "3715", "滨州": "3716", "菏泽": "3717",
    # 河南省
    "郑州": "4101", "开封": "4102", "洛阳": "4103", "平顶山": "4104",
    "安阳": "4105", "鹤壁": "4106", "新乡": "4107", "焦作": "4108",
    "濮阳": "4109", "许昌": "4110", "漯河": "4111", "三门峡": "4112",
    "南阳": "4113", "商丘": "4114", "信阳": "4115", "周口": "4116",
    "驻马店": "4117", "济源": "4190",
    # 湖北省
    "武汉": "4201", "黄石": "4202", "十堰": "4203", "宜昌": "4205",
    "襄阳": "4206", "鄂州": "4207", "荆门": "4208", "孝感": "4209",
    "荆州": "4210", "黄冈": "4211", "咸宁": "4212", "随州": "4213",
    "恩施": "4228",
    # 湖南省
    "长沙": "4301", "株洲": "4302", "湘潭": "4303", "衡阳": "4304",
    "邵阳": "4305", "岳阳": "4306", "常德": "4307", "张家界": "4308",
    "益阳": "4309", "郴州": "4310", "永州": "4311", "怀化": "4312",
    "娄底": "4313", "湘西": "4331",
    # 广东省
    "广州": "4401", "韶关": "4402", "深圳": "4403", "珠海": "4404",
    "汕头": "4405", "佛山": "4406", "江门": "4407", "湛江": "4408",
    "茂名": "4409", "肇庆": "4412", "惠州": "4413", "梅州": "4414",
    "汕尾": "4415", "河源": "4416", "阳江": "4417", "清远": "4418",
    "东莞": "4419", "中山": "4420", "潮州": "4451", "揭阳": "4452",
    "云浮": "4453",
    # 广西壮族自治区
    "南宁": "4501", "柳州": "4502", "桂林": "4503", "梧州": "4504",
    "北海": "4505", "防城港": "4506", "钦州": "4507", "贵港": "4508",
    "玉林": "4509", "百色": "4510", "贺州": "4511", "河池": "4512",
    "来宾": "4513", "崇左": "4514",
    # 海南省
    "海口": "4601", "三亚": "4602", "三沙": "4603", "儋州": "4604",
    # 重庆市
    "重庆": "5001",
    # 四川省
    "成都": "5101", "自贡": "5103", "攀枝花": "5104", "泸州": "5105",
    "德阳": "5106", "绵阳": "5107", "广元": "5108", "遂宁": "5109",
    "内江": "5110", "乐山": "5111", "南充": "5113", "眉山": "5114",
    "宜宾": "5115", "广安": "5116", "达州": "5117", "雅安": "5118",
    "巴中": "5119", "资阳": "5120",
    # 贵州省
    "贵阳": "5201", "六盘水": "5202", "遵义": "5203", "安顺": "5204",
    "毕节": "5205", "铜仁": "5206",
    # 云南省
    "昆明": "5301", "曲靖": "5303", "玉溪": "5304", "保山": "5305",
    "昭通": "5306", "丽江": "5307", "普洱": "5308", "临沧": "5309",
    "楚雄": "5323", "红河": "5325", "文山": "5326", "西双版纳": "5328",
    "大理": "5329", "迪庆": "5334",
    # 西藏自治区
    "拉萨": "5401", "日喀则": "5402", "昌都": "5403", "林芝": "5404",
    "山南": "5405", "那曲": "5406",
    # 陕西省
    "西安": "6101", "铜川": "6102", "宝鸡": "6103", "咸阳": "6104",
    "渭南": "6105", "延安": "6106", "汉中": "6107", "榆林": "6108",
    "安康": "6109", "商洛": "6110",
    # 甘肃省
    "兰州": "6201", "嘉峪关": "6202", "金昌": "6203", "白银": "6204",
    "天水": "6205", "武威": "6206", "张掖": "6207", "平凉": "6208",
    "酒泉": "6209", "庆阳": "6210", "定西": "6211", "陇南": "6212",
    # 青海省
    "西宁": "6301", "海东": "6302",
    # 宁夏回族自治区
    "银川": "6401", "石嘴山": "6402", "吴忠": "6403", "固原": "6404",
    "中卫": "6405",
    # 新疆维吾尔自治区
    "乌鲁木齐": "6501", "克拉玛依": "6502", "吐鲁番": "6504", "哈密": "6505",
}

# Reverse mapping: city code -> city name
CODE_TO_CITY_MAPPING = {v: k for k, v in CITY_CODE_MAPPING.items()}

# English to Chinese city name mapping
ENGLISH_TO_CHINESE_CITY = {
    "Beijing": "北京", "Peking": "北京", "Shanghai": "上海", "Tianjin": "天津",
    "Chongqing": "重庆", "Hangzhou": "杭州", "Nanjing": "南京", "Wuxi": "无锡",
    "Suzhou": "苏州", "Ningbo": "宁波", "Wenzhou": "温州", "Fuzhou": "福州",
    "Xiamen": "厦门", "Guangzhou": "广州", "Canton": "广州", "Shenzhen": "深圳",
    "Dongguan": "东莞", "Foshan": "佛山", "Zhuhai": "珠海", "Wuhan": "武汉",
    "Changsha": "长沙", "Chengdu": "成都", "Xian": "西安", "Xi'an": "西安",
    "Kunming": "昆明", "Harbin": "哈尔滨", "Shenyang": "沈阳", "Dalian": "大连",
    "Qingdao": "青岛", "Jinan": "济南", "Zhengzhou": "郑州", "Hefei": "合肥",
    "Nanchang": "南昌", "Guiyang": "贵阳", "Nanning": "南宁", "Haikou": "海口",
    "Sanya": "三亚", "Lanzhou": "兰州", "Xining": "西宁", "Yinchuan": "银川",
    "Urumqi": "乌鲁木齐", "Lhasa": "拉萨", "Hohhot": "呼和浩特", "Taiyuan": "太原",
    "Shijiazhuang": "石家庄", "Changchun": "长春", "Guilin": "桂林",
}


# =============================================================================
# Tax ID Validation
# =============================================================================

def is_valid_tax_id_format(tax_id: Optional[str]) -> bool:
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
# City Code Mapping
# =============================================================================

def get_city_name_by_code(city_code: Optional[str]) -> Optional[str]:
    """
    Map 4-digit city code to city name.

    Args:
        city_code: 4-digit city administrative code (e.g., "3502")

    Returns:
        City name (e.g., "厦门") or None if not found
    """
    if not city_code or len(city_code) != 4:
        return None

    # Direct match
    if city_code in CODE_TO_CITY_MAPPING:
        return CODE_TO_CITY_MAPPING[city_code]

    # Handle direct municipalities (xx00 -> xx01)
    if city_code.endswith('00'):
        alternative_code = city_code[:2] + '01'
        if alternative_code in CODE_TO_CITY_MAPPING:
            return CODE_TO_CITY_MAPPING[alternative_code]

    # Handle municipality xx02 codes (北京1102, 天津1202, 上海3102, 重庆5002)
    # xx02 for non-municipalities is a real city (e.g., 3202=无锡), so restrict to municipality prefixes
    MUNICIPALITY_PREFIXES = ('11', '12', '31', '50')
    if city_code.endswith('02') and city_code[:2] in MUNICIPALITY_PREFIXES:
        alternative_code = city_code[:2] + '01'
        if alternative_code in CODE_TO_CITY_MAPPING:
            return CODE_TO_CITY_MAPPING[alternative_code]

    return None


# =============================================================================
# City Extraction from Tax ID
# =============================================================================

def extract_city_from_tax_id(tax_id: Optional[str]) -> Optional[str]:
    """
    Extract city from 18-digit unified social credit code.

    Format: 91[CityCode(4)]...
    Example: 91330110MA2H0BC10Q -> 3301 -> Hangzhou

    Args:
        tax_id: 18-digit unified social credit code

    Returns:
        City name or None if extraction fails
    """
    if not tax_id:
        return None

    if not is_valid_tax_id_format(tax_id):
        return None

    # Clean tax ID
    clean_tax_id = tax_id.replace(' ', '').replace('-', '').upper()

    # Extract city code (characters 3-6, index 2-5)
    city_code = clean_tax_id[2:6]

    return get_city_name_by_code(city_code)


# =============================================================================
# English City Name Normalization
# =============================================================================

def normalize_english_city_name(city_name: Optional[str]) -> Optional[str]:
    """
    Normalize English city name to Chinese.

    Args:
        city_name: English city name

    Returns:
        Chinese city name or None if not found
    """
    if not city_name:
        return None

    # Try direct match
    if city_name in ENGLISH_TO_CHINESE_CITY:
        return ENGLISH_TO_CHINESE_CITY[city_name]

    # Try case-insensitive match
    normalized = city_name.strip().replace(' ', '')
    normalized = normalized[0].upper() + normalized[1:].lower() if normalized else ''

    if normalized in ENGLISH_TO_CHINESE_CITY:
        return ENGLISH_TO_CHINESE_CITY[normalized]

    # Try uppercase match
    for key, value in ENGLISH_TO_CHINESE_CITY.items():
        if key.upper() == city_name.upper():
            return value

    return None


# =============================================================================
# LLM City Extraction
# =============================================================================

def get_llm_city(invoice: Dict[str, Any]) -> Optional[str]:
    """
    Extract city from LLM-extracted fields.

    Handles:
    - English to Chinese normalization
    - "市" suffix removal

    Args:
        invoice: Invoice data dictionary

    Returns:
        Normalized city name or None
    """
    llm_city = invoice.get('city')
    if not llm_city:
        return None

    # Try to normalize English city name
    if re.match(r'^[A-Za-z\s]+$', llm_city):
        chinese_city = normalize_english_city_name(llm_city)
        if chinese_city:
            return chinese_city
        # Return original if not in mapping
        return llm_city

    # Remove "市" suffix if present
    if llm_city.endswith('市'):
        return llm_city[:-1]

    return llm_city


# =============================================================================
# Main City Extraction Function
# =============================================================================

def extract_city(invoice: Dict[str, Any], category: Optional[str] = None) -> str:
    """
    Extract city from invoice with category-specific priority.

    Priority varies by category:
    - RIDEHAILING_INVOICE: LLM city only (taxId = company address, not trip location)
    - RIDEHAILING_RECEIPT: LLM city only
    - Others: taxId -> LLM city -> default

    Args:
        invoice: Invoice data dictionary
        category: Invoice category (optional, affects priority)

    Returns:
        City name (Chinese)
    """
    # Special handling for ride-hailing: use LLM city (taxId != trip location)
    if category in ('RIDEHAILING_INVOICE', 'RIDEHAILING_RECEIPT'):
        llm_city = get_llm_city(invoice)
        if llm_city:
            return llm_city
        return DEFAULT_CITY

    # Special handling for train: use arrival station (destination city)
    # Raw station name is kept as-is (e.g., "上海虹桥", "汉口")
    # Frontend getExpenseLocation() handles station-to-Concur-location-ID mapping
    if category == 'TRAIN':
        arrival_station = invoice.get('arrivalStation')
        if arrival_station:
            return arrival_station
        # Fallback to LLM city or default
        llm_city = get_llm_city(invoice)
        if llm_city:
            return llm_city
        return DEFAULT_CITY

    # Priority 1: Tax ID extraction (for other categories)
    tax_id = invoice.get('vendorTaxId')
    if tax_id:
        city = extract_city_from_tax_id(tax_id)
        if city:
            return city

    # Priority 2: LLM-extracted city
    llm_city = get_llm_city(invoice)
    if llm_city:
        return llm_city

    # Priority 3: Default city
    return DEFAULT_CITY


# =============================================================================
# Taxi Invoice Code Parsing
# =============================================================================

def parse_taxi_invoice_code(invoice_code: Optional[str]) -> Dict[str, Any]:
    """
    Parse taxi invoice code.

    Format: 12 digits - [Type(1)][CityCode(4)][Year(2)][Serial(5)]
    Example: 133010251234 -> city 3010 (Hangzhou), year 25 (2025)

    Args:
        invoice_code: 12-digit invoice code string

    Returns:
        Dict with parsing results:
        - valid: bool
        - error: str (if invalid)
        - invoice_code: str
        - city_code: str (characters 3-6, index 2-5)
        - year_code: str (characters 7-8, index 6-7)
        - print_year: int (2000 + year_code)
    """
    if not invoice_code or not isinstance(invoice_code, str):
        return {'valid': False, 'error': 'Invoice code is not a valid string'}

    if len(invoice_code) != 12:
        return {
            'valid': False,
            'error': f'Invoice code length error, expected 12 digits, got {len(invoice_code)}'
        }

    # Validate all digits
    if not invoice_code.isdigit():
        return {'valid': False, 'error': 'Invoice code must be 12 digits'}

    # Characters 3-6 (index 2-5) are city code
    city_code = invoice_code[2:6]
    # Characters 7-8 (index 6-7) are year code
    year_code = invoice_code[6:8]

    # Validate year code
    try:
        year_num = int(year_code)
        if year_num < 0 or year_num > 99:
            return {
                'valid': False,
                'error': f'Invalid year code in invoice: {year_code}'
            }
    except ValueError:
        return {
            'valid': False,
            'error': f'Invalid year code in invoice: {year_code}'
        }

    print_year = 2000 + year_num
    current_year = datetime.now().year

    # Validate print year reasonableness
    if print_year < 2000 or print_year > current_year + 1:
        return {
            'valid': False,
            'error': f'Unreasonable invoice print year: {print_year}'
        }

    return {
        'valid': True,
        'invoice_code': invoice_code,
        'city_code': city_code,
        'year_code': year_code,
        'print_year': print_year
    }
