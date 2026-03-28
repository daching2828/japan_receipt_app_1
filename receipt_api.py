"""
=============================================================
  receipt_api.py — 收據辨識核心 API 模組（Gemini 2.5 Flash 版）
  日本旅遊記帳 App  v2.1

  v2.1 修正：
  - 移除 response_mime_type（與 Gemini 2.5 thinking 模式衝突）
  - 新增 _extract_text_from_response()，正確跳過思考過程 parts
  - 強化 parse_receipt_json：多層 markdown、CRLF、非標準引號全處理
  - 解析失敗時印出完整原始回應，方便 debug
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
#  設定
# ─────────────────────────────────────────────
MODEL = "gemini-2.5-flash"
DEFAULT_JPY_TO_TWD_RATE = 0.218
_PROMPT_SEPARATOR = "\n\n" + "─" * 40 + "\n\n"


# ─────────────────────────────────────────────
#  圖片載入
# ─────────────────────────────────────────────
def load_image(image_path: str) -> Image.Image:
    return Image.open(image_path)


# ─────────────────────────────────────────────
#  ★ Gemini 2.5 回應文字擷取
#    thinking 模式會產生多個 parts，其中 thought=True 的是思考過程
#    必須只取 thought != True 的 text part
# ─────────────────────────────────────────────
def _extract_text_from_response(response) -> str:
    """
    從 Gemini response 正確取出輸出文字，跳過 thinking parts。
    fallback 到 response.text（舊版 SDK 無 thought 屬性時）。
    """
    try:
        parts = response.candidates[0].content.parts
        text_parts = []
        for part in parts:
            # thought=True 的 part 是思考過程，跳過
            if getattr(part, "thought", False):
                continue
            if hasattr(part, "text") and part.text:
                text_parts.append(part.text)
        if text_parts:
            return "\n".join(text_parts).strip()
    except Exception:
        pass

    # fallback
    return response.text.strip()


# ─────────────────────────────────────────────
#  核心辨識函數
# ─────────────────────────────────────────────
def recognize_receipt(
    image_path: str,
    jpy_to_twd_rate: float = DEFAULT_JPY_TO_TWD_RATE,
    api_key: Optional[str] = None,
) -> dict:

    # 初始化
    if api_key:
        genai.configure(api_key=api_key)
    else:
        import os
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ValueError(
                "找不到 Gemini API Key。\n"
                "請在 .streamlit/secrets.toml 設定 GEMINI_API_KEY"
            )
        genai.configure(api_key=key)

    model = genai.GenerativeModel(
        model_name=MODEL,
        generation_config=genai.GenerationConfig(
            temperature=0.1,
            max_output_tokens=8192,
            # ★ 不設定 response_mime_type：
            #   Gemini 2.5 Flash 的 thinking 模式與 application/json 衝突，
            #   改為在 Prompt 層強制要求純 JSON，在 parse 端清洗
        ),
    )

    print(f"📷 載入圖片：{image_path}")
    image = load_image(image_path)

    combined_prompt = (
        RECEIPT_SYSTEM_PROMPT
        + _PROMPT_SEPARATOR
        + RECEIPT_USER_PROMPT
    )

    print(f"🤖 呼叫 {MODEL} 進行辨識...")
    response = model.generate_content([combined_prompt, image])

    # ★ 正確跳過 thinking parts
    raw_response = _extract_text_from_response(response)
    print(f"✅ 辨識完成")
    print(f"   原始回應前 300 字：\n{raw_response[:300]}\n")  # debug，確認 OK 後可刪

    receipt_data = parse_receipt_json(raw_response)

    receipt_data = enrich_with_twd(receipt_data, jpy_to_twd_rate)
    receipt_data["_meta"] = {
        "recognized_at":   datetime.now().isoformat(),
        "model_used":      MODEL,
        "image_path":      str(image_path),
        "jpy_to_twd_rate": jpy_to_twd_rate,
        "input_tokens":    getattr(response.usage_metadata, "prompt_token_count", None),
        "output_tokens":   getattr(response.usage_metadata, "candidates_token_count", None),
    }

    return receipt_data


# ─────────────────────────────────────────────
#  ★ 強化版 JSON 解析
#    處理 Gemini 2.5 常見的各種包裝方式：
#    1. 純 JSON（理想狀況）
#    2. ```json ... ```（markdown code block）
#    3. 前後夾雜說明文字，JSON 在中間
#    4. 非標準引號、CRLF 換行
# ─────────────────────────────────────────────
def parse_receipt_json(raw_text: str) -> dict:

    # 預處理：去除 BOM、統一換行、去頭尾空白
    text = raw_text.strip().lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")

    # Step 1：直接解析（最理想）
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Step 2：剝掉 markdown code block（含多行情況）
    #   支援：```json、```JSON、``` 等變體
    stripped = re.sub(r"^```[a-zA-Z]*\s*\n?", "", text, flags=re.MULTILINE)
    stripped = re.sub(r"\n?```\s*$", "", stripped, flags=re.MULTILINE)
    stripped = stripped.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Step 3：從任意位置擷取最外層 {} 物件（處理前後有說明文字的情況）
    #   找第一個 { 到最後一個 } 之間的內容
    first_brace = text.find("{")
    last_brace  = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Step 4：同上但用 stripped 版本再試一次
    first_brace = stripped.find("{")
    last_brace  = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = stripped[first_brace:last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 全部失敗：印出完整原始回應供 debug
    print("=" * 60)
    print("❌ JSON 解析全部失敗，完整原始回應如下：")
    print(raw_text)
    print("=" * 60)

    return {
        "error":        "JSON 解析失敗",
        "raw_response": raw_text,
    }


# ─────────────────────────────────────────────
#  台幣換算
# ─────────────────────────────────────────────
def enrich_with_twd(receipt_data: dict, rate: float) -> dict:
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
#  格式化輸出（命令列用）
# ─────────────────────────────────────────────
def pretty_print_receipt(receipt_data: dict):
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
#  命令列測試入口
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
