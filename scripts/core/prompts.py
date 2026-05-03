"""
LLM prompts for invoice OCR extraction.

Extracted from reimbursement-helper's bedrock_ocr.py (commit a0e8515).
Any change here should be mirrored back to reimbursement-helper for
consistency — the OCR prompt is the shared contract.
"""


def get_ocr_prompt() -> str:
    """Return the OCR extraction prompt for invoice documents."""
    return """# 发票信息提取任务

请从发票图片中提取以下字段，返回 JSON 格式。

## 通用字段（所有发票类型）

| 字段名 | 类型 | 说明 |
|--------|------|------|
| transactionDate | string | 开票日期，格式 YYYY-MM-DD |
| transactionAmount | number | 价税合计金额 |
| vendorName | string | 销售方名称（见下方重要规则） |
| vendorTaxId | string | 销售方统一社会信用代码（18位） |
| serviceType | string | 服务类型，如 "*餐饮服务*餐饮费", "*运输服务*客运服务费", "*住宿服务*房费", "*电信服务*通信服务费", "*旅游服务*代订车服务费" |
| docType | string | 文档类型，如 "电子发票（普通发票）", "电子发票（增值税专用发票）", "Guest Folio", "INFORMATION INVOICE", "INFORMATION BILL", "行程单", "行程报销单", "出租汽车发票" |
| invoiceNo | string | 发票号码（中国电子发票右上角"发票号码"后的 20 位数字，例："25327000001619791763"）。不是 invoiceCode。水单/行程单等非税务发票无此字段时返回 null |
| isChineseInvoice | boolean | 是否为中国增值税发票, 有发票印章（税务局）, 发票号码, 销售方/购买方信息, 统一社会信用代码 |
| currency | string | ISO-4217 三字母币种代码。大陆增值税发票/普票/电子发票 = "CNY"；美元账单 = "USD"；欧元 = "EUR"；未写明或识别不出 → 返回 "CNY"（保守默认，避免误改人民币发票）|

## ⚠️ 重要规则：购买方/销售方区分

中国增值税发票布局：
```
┌─────────────────────────────────────────────────┐
│  购买方（左侧）          │  销售方（右侧）          │
│  名称：亚马逊信息服务...  │  名称：XX餐饮公司        │
│  税号：91110...         │  税号：91320...        │
└─────────────────────────────────────────────────┘
```

**必须同时提取两方信息：**

| 字段名 | 类型 | 说明 |
|--------|------|------|
| buyerName | string | 购买方名称（左侧）- 通常是亚马逊等公司 |
| buyerTaxId | string | 购买方税号（左侧） |
| sellerName | string | 销售方名称（右侧）- 提供服务的商家 |
| sellerTaxId | string | 销售方税号（右侧） |

**关键规则：**
- ✅ vendorName 必须等于 sellerName（销售方/右侧）
- ✅ vendorTaxId 必须等于 sellerTaxId（销售方/右侧）
- ❌ 绝对不能把购买方信息填入vendorName/vendorTaxId
- ❌ 如果vendorName包含"亚马逊"，说明提取错误

## 酒店发票专用字段

| 字段名 | 类型 | 说明 |
|--------|------|------|
| transactionAmountNoVAT | number | 金额栏数值（不含税），不是价税合计 |
| VAT | number | 税额栏数值 |
| remark | string | 备注栏中的确认号 |
| checkInDate | string | 入住日期 YYYY-MM-DD |
| checkOutDate | string | 离店日期 YYYY-MM-DD |

验证：transactionAmountNoVAT + VAT = transactionAmount（允许 ±0.01 误差）

## 酒店水单(Guest Folio)专用字段

| 字段名 | 类型 | 说明 |
|--------|------|------|
| balance | number | 应付总额 |
| hotelName | string | 酒店名称 |
| confirmationNo | string | 确认号/预订号 |
| internalCodes | array | 内部编码列表（用于匹配发票备注） |
| arrivalDate | string | Check-in date, 到达日期 YYYY-MM-DD |
| departureDate | string | Check-out date, 离开日期 YYYY-MM-DD |
| city | string | 城市名（可能是英文如 "WUXI"） |
| roomNumber | string | 房间号 |

**⚠️ transactionDate 取值规则（酒店水单）：**

统一使用 **departureDate（退房日）** 作为 transactionDate。
理由：退房日是住宿服务完成的日期，与发票开票日对齐，是 P2 匹配依赖的字段。
不要取 arrivalDate、check-in、room charge date 等其他日期。

若 departureDate 无法识别（水单残缺或信息缺失），则 transactionDate 填 null，不要猜测或用 arrivalDate 代替。

## ⚠️ Hotel-specific field conditional extraction (v5.7)

`arrivalDate / departureDate / checkInDate / checkOutDate / roomNumber` MUST be populated **only** when the source PDF contains explicit hotel-domain labels near the value, including any of:
- English: `Arrival`, `Departure`, `Check-in`, `Check-out`, `Check in`, `Check out`, `Room No.`, `Room Number`
- Chinese: `入住日期`, `抵店日期`, `离店日期`, `退房日期`, `到达日期`, `离开日期`, `入离日期`, `房号`, `房间号`, `房间号码`

If a date or number appears only in non-hotel contexts — such as subscription period, service period, billing cycle, date of issue, date due, date paid, or payment history — these fields MUST remain `null`. Do NOT infer, guess, or transcribe subscription ranges (e.g. "Nov 12, 2025 – Nov 12, 2026") into arrivalDate/departureDate.

Rationale: downstream classifier uses these fields to distinguish hotel folios from SaaS invoices. Filling them without hotel-domain textual evidence causes non-reimbursable SaaS receipts to be misrouted into the hotel matching pipeline.

## 网约车发票专用字段

| 字段名 | 类型 | 说明 |
|--------|------|------|
| invoiceCode | string | 发票代码（12位，如有） |

## 网约车行程单专用字段

| 字段名 | 类型 | 说明 |
|--------|------|------|
| applicationDate | string | 申请日期（行程单专用），从表头「申请日期：YYYY-MM-DD」提取 |
| totalAmount | number | 合计金额 |
| tripCount | number | 行程数量 |
| city | string | 从表格"城市"列提取，去掉"市"后缀 |

**⚠️ transactionDate 取值规则（行程单）：**

将 applicationDate 同时填入 transactionDate。
理由：申请日期是行程单的结算时间点，与对应发票的开票日期最接近。
不要取各笔行程的上车时间（不同行程日期不同），也不要取行程起止范围的任一端点。

若 applicationDate 无法识别，applicationDate 和 transactionDate 都填 null，不要用行程起止范围猜测。

行程单表格示例：
```
序号 | 车型 | 上车时间 | 城市 | 起点 | 终点 | 金额
1    | 专车 | 01-10    | 南京市| ...  | ...  | 25.00
```
提取 city = "南京"（去掉"市"）

## 话费发票专用字段

| 字段名 | 类型 | 说明 |
|--------|------|------|
| billingPeriod | string | 账期，格式 YYYY-MM 或 YYYYMM-YYYYMM |
| phoneNumber | string | 手机号码 |

## 火车票/铁路电子客票专用字段

铁路电子客票（docType: "电子发票（铁路电子客票）"）没有传统的销售方/购买方结构。

| 字段名 | 类型 | 说明 |
|--------|------|------|
| trainNumber | string | 车次，如 "D952", "G747", "K123" |
| departureStation | string | 出发站，如 "上海", "无锡东" |
| arrivalStation | string | 到达站，如 "无锡", "上海虹桥" |
| seatClass | string | 座席等级，如 "二等座", "一等座", "商务座" |
| departureTime | string | 出发时间，格式 HH:MM，如 "08:30" |

**火车票特殊规则：**
- vendorName 填写 "中国铁路"（铁路票据由国家税务总局开具，无商业销售方）
- vendorTaxId 填写 null
- transactionDate 使用乘车日期（非开票日期）
- transactionAmount 使用票价金额
- docType 填写 "电子发票（铁路电子客票）"
- 购买方名称仍然提取到 buyerName 字段

## 输出格式

返回纯 JSON，不要添加任何说明文字：

```json
{
  "transactionDate": "2025-01-10",
  "transactionAmount": 156.00,
  "buyerName": "亚马逊信息服务（北京）有限公司上海分公司",
  "buyerTaxId": "91310115MA1K4XXXXX",
  "sellerName": "无锡茵赫餐饮管理有限公司",
  "sellerTaxId": "91320214MA1XXXXXX",
  "vendorName": "无锡茵赫餐饮管理有限公司",
  "vendorTaxId": "91320214MA1XXXXXX",
  "serviceType": "*餐饮服务*餐饮费",
  "docType": "电子发票（普通发票）",
  "currency": "CNY",
  "isChineseInvoice": true
}
```

**酒店水单示例**（注意 transactionDate == departureDate；仍需按通用字段表提取 transactionAmount、vendorName、vendorTaxId 等）：

```json
{
  "docType": "Guest Folio",
  "hotelName": "苏州万豪酒店",
  "arrivalDate": "2025-05-07",
  "departureDate": "2025-05-08",
  "transactionDate": "2025-05-08",
  "balance": 583.97,
  "confirmationNo": "4329092847491260840",
  "currency": "CNY"
}
```

**网约车行程单示例**（注意 transactionDate == applicationDate；仍需按通用字段表提取 transactionAmount、vendorName、docType 等）：

```json
{
  "docType": "行程报销单",
  "vendorName": "滴滴出行",
  "applicationDate": "2025-12-09",
  "transactionDate": "2025-12-09",
  "totalAmount": 245.50,
  "tripCount": 12,
  "city": "南京",
  "currency": "CNY"
}
```

注意：vendorName/vendorTaxId 必须与 sellerName/sellerTaxId 相同。缺失字段使用 null。"""
