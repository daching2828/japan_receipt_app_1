"""
=============================================================
  receipt_api.py — 收據辨識核心 API 模組（Gemini 版）
  日本旅遊記帳 App  v2.0

  異動說明（從 Claude Sonnet → Gemini 1.5 Flash）：
  - 套件：anthropic  →  google-generativeai
  - 圖片傳入：base64 字串 → PIL Image 物件（Gemini 原生格式）
  - System Prompt 合併進 user message（Gemini 無獨立 system 參數）
  - 對外介面（recognize_receipt / pretty_print_receipt）完全不變
    → app.py 與 notion_sync.py 無需修改任何一行
  - receipt_prompt.py 的翻譯邏輯 100% 繼續有效
=============================================================
"""

import json
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

import google.generativeai as genai
from PIL import Image

from receipt_prompt import RECEIPT_SYSTEM_PROMPT, RECEIPT_USER_PROMPT


# ─────────────────────────────────────────────
#  設定區塊
# ─────────────────────────────────────────────
MODEL = "gemini-1.5-flash-latest"

# 預設匯率
DEFAULT_JPY_TO_TWD_RATE = 0.218  # 約 1 JPY ≈ 0.218 TWD（2025 參考值）

# ★ Gemini 無獨立 system 參數，需將 System Prompt 前置到 user message
# 兩段 Prompt 合併的分隔線
_PROMPT_SEPARATOR = "\n\n" + "─" * 40 + "\n\n"


# ─────────────────────────────────────────────
#  圖片載入（Gemini 使用 PIL Image 物件）
# ─────────────────────────────────────────────
def load_image(image_path: str) -> Image.Image:
    """
    讀取圖片並回傳 PIL Image 物件。
    Gemini API 直接接受 PIL Image，不需轉 base64。
    """
    return Image.open(image_path)


# ─────────────────────────────────────────────
#  核心辨識函數（對外介面與 Claude 版完全相同）
# ─────────────────────────────────────────────
def recognize_receipt(
    image_path: str,
    jpy_to_twd_rate: float = DEFAULT_JPY_TO_TWD_RATE,
    api_key: Optional[str] = None,
) -> dict:
    """
    主要辨識函數。

    Args:
        image_path:      收據圖片路徑
        jpy_to_twd_rate: 日圓兌台幣匯率
        api_key:         Gemini API Key（None 時使用環境變數 GEMINI_API_KEY）

    Returns:
        包含辨識結果與台幣換算的完整 dict（結構與 Claude 版完全相同）
    """

    # 初始化 Gemini client
    if api_key:
        genai.configure(api_key=api_key)
    else:
        import os
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ValueError(
                "找不到 Gemini API Key。\n"
                "請在 .streamlit/secrets.toml 設定 GEMINI_API_KEY，\n"
                "或設定環境變數 export GEMINI_API_KEY=你的金鑰"
            )
        genai.configure(api_key=key)

    model = genai.GenerativeModel(
        model_name=MODEL,
        generation_config=genai.GenerationConfig(
            # 要求 Gemini 輸出 JSON，降低夾雜說明文字的機率
            response_mime_type="application/json",
            temperature=0.1,      # 低溫度 = 較穩定的 JSON 輸出
            max_output_tokens=2048,
        ),
    )

    # 載入圖片
    print(f"📷 載入圖片：{image_path}")
    image = load_image(image_path)

    # ★ 關鍵差異：Gemini 沒有 system 參數
    #   做法：將 RECEIPT_SYSTEM_PROMPT 前置到 user message 最前面
    combined_prompt = (
        RECEIPT_SYSTEM_PROMPT
        + _PROMPT_SEPARATOR
        + RECEIPT_USER_PROMPT
    )

    print(f"🤖 呼叫 Gemini {MODEL} 進行辨識...")
    response = model.generate_content([combined_prompt, image])

    raw_response = response.text
    print(f"✅ 辨識完成（候選數：{len(response.candidates)}）")

    # 解析 JSON 回應
    receipt_data = parse_receipt_json(raw_response)

    # 附加台幣換算與元數據
    receipt_data = enrich_with_twd(receipt_data, jpy_to_twd_rate)
    receipt_data["_meta"] = {
        "recognized_at":   datetime.now().isoformat(),
        "model_used":      MODEL,
        "image_path":      str(image_path),
        "jpy_to_twd_rate": jpy_to_twd_rate,
        # Gemini 的 token 計數位置與 Claude 不同
        "input_tokens":    getattr(response.usage_metadata, "prompt_token_count", None),
        "output_tokens":   getattr(response.usage_metadata, "candidates_token_count", None),
    }

    return receipt_data


# ─────────────────────────────────────────────
#  JSON 解析（同 Claude 版，加強 Gemini 特有的邊界情況）
# ─────────────────────────────────────────────
def parse_receipt_json(raw_text: str) -> dict:
    """
    解析模型回傳的 JSON。
    Gemini 指定 response_mime_type="application/json" 後通常直接輸出純 JSON，
    但仍保留 fallback 防禦。
    """
    # 去除可能的 BOM 或前後空白
    text = raw_text.strip().lstrip("\ufeff")

    # 嘗試直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 去除 markdown code block（Gemini 偶爾還是會加）
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # 最後嘗試：用 regex 擷取最外層 JSON 物件
    json_pattern = re.search(r'\{[\s\S]*\}', text)
    if json_pattern:
        try:
            return json.loads(json_pattern.group())
        except json.JSONDecodeError:
            pass

    return {
        "error":        "JSON 解析失敗",
        "raw_response": raw_text,
    }


# ─────────────────────────────────────────────
#  台幣換算（與 Claude 版完全相同）
# ─────────────────────────────────────────────
def enrich_with_twd(receipt_data: dict, rate: float) -> dict:
    """加入台幣換算欄位"""
    if "error" in receipt_data:
        return receipt_data

    total_jpy = receipt_data.get("total_amount_jpy", 0) or 0
    receipt_data["total_amount_twd"]   = round(total_jpy * rate)
    receipt_data["exchange_rate_used"] = rate

    for item in receipt_data.get("items", []):
        subtotal = item.get("subtotal_jpy", 0) or 0
        item["subtotal_twd"] = round(subtotal * rate)

    return receipt_data


# ─────────────────────────────────────────────
#  格式化輸出（與 Claude 版完全相同）
# ─────────────────────────────────────────────
def pretty_print_receipt(receipt_data: dict):
    """在終端機格式化顯示辨識結果"""

    if "error" in receipt_data:
        print(f"❌ 辨識失敗：{receipt_data['error']}")
        print(receipt_data.get("raw_response", ""))
        return

    store = receipt_data.get("store_name", {})
    print("\n" + "═" * 50)
    print(f"  🏪 店名：{store.get('chinese', 'N/A')}（{store.get('japanese', '')}）")
    if store.get("address"):
        print(f"  📍 地址：{store['address']}")
    print(f"  📅 日期：{receipt_data.get('date', 'N/A')}  ⏰ {receipt_data.get('time', 'N/A')}")
    print("═" * 50)

    print("\n  📋 消費明細：")
    for item in receipt_data.get("items", []):
        tax_badge = f"[{item.get('tax_category', '')}]" if item.get("tax_category") else ""
        print(f"  　{item.get('name_chinese', 'N/A')}")
        print(f"  　　({item.get('name_japanese', '')}) {tax_badge}")
        print(f"  　　× {item.get('quantity', 1)}  "
              f"¥{item.get('unit_price_jpy', 0):,}  "
              f"→ ¥{item.get('subtotal_jpy', 0):,} "
              f"≈ NT${item.get('subtotal_twd', 0):,}")

    print("\n" + "─" * 50)
    print(f"  消費稅 8%：  ¥{receipt_data.get('tax_8_amount_jpy', 0):,}")
    print(f"  消費稅 10%： ¥{receipt_data.get('tax_10_amount_jpy', 0):,}")
    print(f"  💴 總金額：  ¥{receipt_data.get('total_amount_jpy', 0):,}"
          f"  ≈  💰 NT${receipt_data.get('total_amount_twd', 0):,}")
    print(f"  💳 支付方式：{receipt_data.get('payment_method', 'N/A')}")

    meta = receipt_data.get("_meta", {})
    print(f"\n  🤖 模型：{meta.get('model_used', MODEL)}")
    if meta.get("input_tokens"):
        print(f"  📊 Token：{meta['input_tokens']} in / {meta['output_tokens']} out")

    confidence = receipt_data.get("confidence", {})
    print(f"\n  🎯 辨識信心：{confidence.get('overall', 'N/A')}/100")
    if confidence.get("notes"):
        print(f"  ⚠️  備註：{confidence['notes']}")
    print("═" * 50 + "\n")


# ─────────────────────────────────────────────
#  命令列快速測試入口
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法：python receipt_api.py <圖片路徑> [匯率]")
        print("範例：python receipt_api.py 13.jpg 0.218")
        sys.exit(1)

    image_path = sys.argv[1]
    rate = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_JPY_TO_TWD_RATE

    result = recognize_receipt(image_path, jpy_to_twd_rate=rate)
    pretty_print_receipt(result)

    output_path = Path(image_path).stem + "_result.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"📄 JSON 已儲存：{output_path}")
