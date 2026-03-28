"""
=============================================================
  notion_sync.py — Notion 同步模組
  日本旅遊記帳 App — 第二階段

  功能：將收據辨識結果同步到 Notion Database
  Database ID: 3307a8578fa180b6832bc992203e5a81

  Notion 欄位對應：
  ┌─────────────────┬──────────────┬───────────────────────┐
  │ 欄位名稱        │ Notion 類型  │ 來源                  │
  ├─────────────────┼──────────────┼───────────────────────┤
  │ 店名            │ Title        │ store_name.chinese     │
  │ 日期            │ Date         │ date                   │
  │ 消費明細        │ Rich Text    │ items 陣列格式化       │
  │ 日幣金額        │ Number(Yen)  │ total_amount_jpy       │
  │ 台幣預估        │ Formula      │ 自動計算（Notion端）   │
  │ 分類            │ Select       │ category（App選擇）    │
  │ 支付方式        │ Select       │ payment_method         │
  └─────────────────┴──────────────┴───────────────────────┘
=============================================================
"""

import json
import urllib.request
import urllib.error
from datetime import datetime, date
from typing import Optional


# ─────────────────────────────────────────────
#  設定
# ─────────────────────────────────────────────
NOTION_DATABASE_ID = "3307a8578fa180b6832bc992203e5a81"
NOTION_API_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"


# ─────────────────────────────────────────────
#  核心同步函數（純標準庫，無需安裝 notion-client）
# ─────────────────────────────────────────────
def sync_to_notion(
    receipt_data: dict,
    category: str,
    notion_token: str,
    database_id: str = NOTION_DATABASE_ID,
    jpy_to_twd_rate: float = 0.218,
) -> dict:
    """
    將單筆收據辨識結果同步到 Notion Database。

    Args:
        receipt_data:    receipt_api.py 回傳的辨識結果 dict
        category:        消費分類（如「🍜 餐飲」）
        notion_token:    Notion Integration Token（secret_xxx）
        database_id:     Notion Database ID
        jpy_to_twd_rate: 日圓兌台幣匯率

    Returns:
        Notion API 回傳的 page 物件（包含 page id、url 等）

    Raises:
        NotionSyncError: 同步失敗時拋出，附帶詳細錯誤訊息
    """
    payload = _build_payload(receipt_data, category, database_id, jpy_to_twd_rate)
    return _post_to_notion(payload, notion_token)


# ─────────────────────────────────────────────
#  Payload 建構
# ─────────────────────────────────────────────
def _build_payload(
    receipt: dict,
    category: str,
    database_id: str,
    jpy_to_twd_rate: float,
) -> dict:
    """將辨識結果轉換成 Notion API 所需的 JSON 格式"""

    store = receipt.get("store_name", {})
    store_chinese = store.get("chinese", "未知店家")
    store_japanese = store.get("japanese", "")

    # 日期處理：有辨識到就用，否則用今天
    receipt_date = receipt.get("date")
    if not receipt_date:
        receipt_date = date.today().isoformat()

    # 品項清單格式化為 Rich Text
    items_text = _format_items_as_text(receipt.get("items", []))

    # 金額
    total_jpy = receipt.get("total_amount_jpy", 0) or 0
    # total_twd 由 Notion Formula 自動計算，這裡不需要傳入

    # 支付方式：去除 emoji 前綴以符合 Notion Select 格式
    payment = receipt.get("payment_method") or "現金"

    # 分類：去除 emoji 前綴
    category_clean = category.split(" ", 1)[-1] if " " in category else category

    properties = {
        # ── Title（店名）──────────────────────────
        "店名": {
            "title": [
                {
                    "text": {
                        "content": store_chinese
                    }
                }
            ]
        },

        # ── Date（日期）──────────────────────────
        "日期": {
            "date": {
                "start": receipt_date
            }
        },

        # ── Rich Text（消費明細）──────────────
        "消費明細": {
            "rich_text": [
                {
                    "text": {
                        "content": items_text[:2000]  # Notion Rich Text 上限 2000 字
                    }
                }
            ]
        },

        # ── Number / Yen（日幣金額）──────────────
        "日幣金額": {
            "number": total_jpy
        },

        # ── Select（分類）────────────────────────
        "分類": {
            "select": {
                "name": category_clean
            }
        },

        # ── Select（支付方式）────────────────────
        "支付方式": {
            "select": {
                "name": payment
            }
        },

    }

    # 若有店家日文名稱，附加在 Rich Text 備註欄
    if store_japanese:
        properties["消費明細"]["rich_text"][0]["text"]["content"] = (
            f"【{store_japanese}】\n" + items_text
        )[:2000]

    return {
        "parent": {"database_id": database_id},
        "properties": properties,
        # ── 頁面內容（收合區塊，顯示完整 JSON）──
        "children": _build_page_content(receipt, jpy_to_twd_rate),
    }


def _format_items_as_text(items: list) -> str:
    """將品項陣列格式化為易讀的純文字"""
    if not items:
        return "（無品項明細）"

    lines = []
    for item in items:
        name_zh = item.get("name_chinese", "")
        name_jp = item.get("name_japanese", "")
        qty = item.get("quantity", 1)
        subtotal = item.get("subtotal_jpy", 0) or 0
        tax = item.get("tax_category", "")
        tax_badge = f"[{tax}]" if tax else ""

        line = f"• {name_zh}（{name_jp}）{tax_badge}"
        if qty > 1:
            unit = item.get("unit_price_jpy", 0) or 0
            line += f"\n  × {qty}  @¥{unit:,}  = ¥{subtotal:,}"
        else:
            line += f"  ¥{subtotal:,}"

        lines.append(line)

    return "\n".join(lines)


def _build_page_content(receipt: dict, jpy_to_twd_rate: float) -> list:
    """建構 Notion 頁面內容區塊（摺疊式 JSON 原始資料）"""
    total_jpy = receipt.get("total_amount_jpy", 0) or 0
    total_twd = round(total_jpy * jpy_to_twd_rate)
    recognized_at = receipt.get("_meta", {}).get("recognized_at", "")

    return [
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"¥{total_jpy:,}  ≈  NT${total_twd:,}　｜　匯率 {jpy_to_twd_rate}　｜　辨識時間 {recognized_at[:16]}"
                }}],
                "icon": {"emoji": "🧾"},
                "color": "orange_background"
            }
        },
        {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": "🔧 原始辨識 JSON（展開查看）"}}],
                "children": [
                    {
                        "object": "block",
                        "type": "code",
                        "code": {
                            "rich_text": [{"type": "text", "text": {
                                "content": json.dumps(receipt, ensure_ascii=False, indent=2)[:2000]
                            }}],
                            "language": "json"
                        }
                    }
                ]
            }
        }
    ]


# ─────────────────────────────────────────────
#  HTTP 請求（純標準庫）
# ─────────────────────────────────────────────
def _post_to_notion(payload: dict, token: str) -> dict:
    """發送 POST 請求到 Notion API"""
    url = f"{NOTION_API_BASE}/pages"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_API_VERSION,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            err = json.loads(body)
            msg = err.get("message", body)
            code = err.get("code", "unknown")
        except Exception:
            msg, code = body, "parse_error"
        raise NotionSyncError(
            f"Notion API 錯誤 {e.code} [{code}]：{msg}\n"
            f"請確認：\n"
            f"  1. Token 是否正確（secret_xxx）\n"
            f"  2. Integration 是否已連接到此 Database\n"
            f"  3. 欄位名稱是否與 Notion 完全一致（含全形/半形）"
        ) from e

    except urllib.error.URLError as e:
        raise NotionSyncError(f"網路連線失敗：{e.reason}") from e

    except TimeoutError as e:
        raise NotionSyncError("Notion API 請求逾時（15 秒），請稍後再試") from e


class NotionSyncError(Exception):
    """Notion 同步失敗的自訂例外"""
    pass


# ─────────────────────────────────────────────
#  查詢現有頁面（防止重複同步）
# ─────────────────────────────────────────────
def check_duplicate(
    store_name: str,
    receipt_date: str,
    total_jpy: int,
    notion_token: str,
    database_id: str = NOTION_DATABASE_ID,
) -> Optional[str]:
    """
    檢查同日期、同店名、同金額的記錄是否已存在。
    存在則回傳 page_id，不存在回傳 None。
    """
    url = f"{NOTION_API_BASE}/databases/{database_id}/query"
    payload = {
        "filter": {
            "and": [
                {"property": "日期", "date": {"equals": receipt_date}},
                {"property": "日幣金額", "number": {"equals": total_jpy}},
            ]
        }
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {notion_token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            pages = result.get("results", [])
            if pages:
                return pages[0]["id"]
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
#  命令列測試入口
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys, os

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("請設定環境變數 NOTION_TOKEN=secret_xxx")
        sys.exit(1)

    # 讀取辨識結果 JSON（用 13_result.json 測試）
    json_path = sys.argv[1] if len(sys.argv) > 1 else "13_result.json"
    with open(json_path, encoding="utf-8") as f:
        receipt = json.load(f)

    print(f"📤 同步到 Notion Database {NOTION_DATABASE_ID}...")

    page = sync_to_notion(
        receipt_data=receipt,
        category="🛍️ 購物",
        notion_token=token,
    )

    page_id = page.get("id", "")
    page_url = page.get("url", "")
    print(f"✅ 同步成功！")
    print(f"   Page ID : {page_id}")
    print(f"   開啟連結: {page_url}")
