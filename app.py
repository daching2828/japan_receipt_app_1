"""
=============================================================
  app.py — Streamlit UI（v1.4 修正版）

  v1.4 修正：
  - _do_notion_sync 移到呼叫點之前（修正 Python 定義順序問題）
  - date 為 null 時改用今天日期，不傳空字串給 Notion
  - 同步失敗時展開完整錯誤訊息，不再靜默吞掉
=============================================================
"""

import streamlit as st
import json
import os
import sys
import tempfile
from pathlib import Path


# ─────────────────────────────────────────────
#  Secret 讀取（toml 備案 + st.secrets + 環境變數）
# ─────────────────────────────────────────────
def _load_toml_secrets() -> dict:
    candidates = [
        Path.cwd() / ".streamlit" / "secrets.toml",
        Path(__file__).parent / ".streamlit" / "secrets.toml",
        Path.home() / ".streamlit" / "secrets.toml",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            import tomllib
            with open(p, "rb") as f:
                return tomllib.load(f)
        except ImportError:
            pass
        try:
            import tomli
            with open(p, "rb") as f:
                return tomli.load(f)
        except ImportError:
            pass
        try:
            import toml
            return toml.load(str(p))
        except ImportError:
            pass
        return {}
    return {}


_TOML_SECRETS: dict = _load_toml_secrets()


def get_secret(key: str) -> str | None:
    try:
        return st.secrets[key]
    except Exception:
        pass
    if key in _TOML_SECRETS:
        return str(_TOML_SECRETS[key])
    return os.environ.get(key, None)


def _secrets_debug_info() -> str:
    cwd        = Path.cwd()
    script_dir = Path(__file__).parent
    found      = "找到 ✅" if _TOML_SECRETS else "找不到 ❌"
    return "\n".join([
        "#### 程式找尋 `secrets.toml` 的路徑：",
        f"1. `{cwd / '.streamlit' / 'secrets.toml'}`",
        f"2. `{script_dir / '.streamlit' / 'secrets.toml'}`",
        f"3. `{Path.home() / '.streamlit' / 'secrets.toml'}`",
        f"\n手動讀取結果：{found}",
        f"執行目錄：`{cwd}`",
    ])


# ─────────────────────────────────────────────
#  Import
# ─────────────────────────────────────────────
from receipt_api import recognize_receipt, DEFAULT_JPY_TO_TWD_RATE
from notion_sync import sync_to_notion, check_duplicate, NotionSyncError

NOTION_DATABASE_ID = "3307a8578fa180b6832bc992203e5a81"

st.set_page_config(
    page_title="日本旅遊記帳 App",
    page_icon="🗾",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
    .main-title { font-size: 2rem; font-weight: bold; color: #D63031;
                  text-align: center; padding: 1rem 0; }
    .subtitle   { text-align: center; color: #636e72; margin-bottom: 2rem; }
    .total-box  { background: linear-gradient(135deg, #D63031, #FF7675);
                  color: white; padding: 1.5rem; border-radius: 1rem;
                  text-align: center; margin: 1rem 0; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
#  ★ Notion 同步函數（必須在呼叫點之前定義）
# ─────────────────────────────────────────────
def _do_notion_sync(result: dict, category: str, token: str, rate: float):
    """執行 Notion 同步，含重複偵測與完整錯誤顯示"""
    from datetime import date as _date

    store_name   = result.get("store_name", {}).get("chinese", "未知店家")
    receipt_date = result.get("date") or _date.today().isoformat()  # null → 今天
    total_jpy    = result.get("total_amount_jpy", 0) or 0

    with st.spinner("📤 正在同步到 Notion..."):

        # 重複偵測（只在有日期時做）
        try:
            dup_id = check_duplicate(store_name, receipt_date, total_jpy, token)
            if dup_id:
                st.warning(
                    f"⚠️ 偵測到可能重複記錄（同日期 + 同金額），已略過。\n"
                    f"Page ID：{dup_id}"
                )
                return
        except Exception as e:
            # 重複偵測失敗不阻擋寫入，只記錄警告
            st.warning(f"⚠️ 重複偵測略過（{e}），繼續同步...")

        # 寫入 Notion
        try:
            page = sync_to_notion(
                receipt_data=result,
                category=category,
                notion_token=token,
                jpy_to_twd_rate=rate,
            )
            st.session_state["notion_synced"]   = True
            st.session_state["notion_page_url"] = page.get("url", "")
            st.success("✅ 同步成功！")
            st.rerun()

        except NotionSyncError as e:
            st.error(f"❌ Notion 同步失敗")
            st.code(str(e), language=None)   # 展開完整錯誤，方便 debug

        except Exception as e:
            st.error(f"❌ 未知錯誤")
            st.code(f"{type(e).__name__}: {e}", language=None)


# ─────────────────────────────────────────────
#  側欄
# ─────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙️ 設定")

    gemini_key   = get_secret("GEMINI_API_KEY")
    notion_token = get_secret("NOTION_TOKEN")

    if gemini_key:
        st.success("✅ Gemini API Key 已設定")
    else:
        gemini_key = st.text_input(
            "Gemini API Key", type="password", placeholder="AIza..."
        )
        if not gemini_key:
            with st.expander("❓ 找不到 Key？點此除錯"):
                st.markdown(_secrets_debug_info())

    if notion_token:
        st.success("✅ Notion Token 已設定")
    else:
        notion_token = st.text_input(
            "Notion Token", type="password", placeholder="secret_..."
        )

    st.divider()
    st.markdown("### 💱 匯率設定")
    jpy_rate = st.number_input(
        "日圓 → 台幣匯率",
        min_value=0.1, max_value=1.0,
        value=DEFAULT_JPY_TO_TWD_RATE, step=0.001, format="%.3f"
    )
    st.caption(f"¥1,000 ≈ NT${1000 * jpy_rate:.0f}")

    st.divider()
    category = st.selectbox("消費分類", [
        "🍜 餐飲", "🚃 交通", "🏨 住宿",
        "🛍️ 購物", "🎭 娛樂", "💊 醫藥", "📦 其他"
    ])

    st.divider()
    st.markdown("**開發進度**")
    st.markdown("- ✅ 收據辨識 + AI 翻譯")
    st.markdown("- ✅ Notion 同步")
    st.markdown("- 🔲 旅遊帳本總覽（第三階段）")


# ─────────────────────────────────────────────
#  主頁面
# ─────────────────────────────────────────────
st.markdown('<div class="main-title">🗾 日本旅遊記帳 App</div>', unsafe_allow_html=True)
st.markdown('<div class="subtitle">拍照即辨識 · 自動翻中文 · 同步 Notion</div>',
            unsafe_allow_html=True)

col1, col2 = st.columns([1, 1], gap="large")

# ── 左欄 ────────────────────────────────────
with col1:
    st.markdown("### 📷 上傳收據")
    st.info("📱 手機用戶：點擊下方按鈕可直接選擇「相機拍照」！")
    uploaded_file = st.file_uploader(
        "選擇收據圖片", type=["jpg", "jpeg", "png", "webp"]
    )

    if uploaded_file:
        st.image(uploaded_file, caption="上傳的收據", use_container_width=True)
        recognize_btn = st.button("🔍 開始辨識", type="primary", use_container_width=True)
    else:
        st.markdown("""
        <div style="border:2px dashed #ccc;border-radius:1rem;
                    padding:3rem;text-align:center;color:#aaa;">
            <div style="font-size:3rem;">📄</div>
            <div>拖曳或點擊上傳</div>
            <div style="font-size:0.8rem;margin-top:0.5rem;">JPG / PNG / WebP</div>
        </div>""", unsafe_allow_html=True)
        recognize_btn = False


# ── 右欄 ────────────────────────────────────
with col2:
    st.markdown("### 📋 辨識結果")

    if uploaded_file and recognize_btn:
        if not gemini_key:
            st.error("❌ 請先設定 Gemini API Key（見左側側欄）")
            st.markdown(_secrets_debug_info())
        else:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=Path(uploaded_file.name).suffix
            ) as tmp:
                tmp.write(uploaded_file.getbuffer())
                tmp_path = tmp.name
            try:
                with st.spinner("🤖 AI 正在辨識收據中..."):
                    result = recognize_receipt(
                        tmp_path,
                        jpy_to_twd_rate=jpy_rate,
                        api_key=gemini_key,
                    )
                st.session_state["last_result"]   = result
                st.session_state["last_category"] = category
                st.session_state["notion_synced"] = False
            except Exception as e:
                st.error(f"❌ 辨識失敗：{e}")
            finally:
                os.unlink(tmp_path)

    if "last_result" in st.session_state:
        result = st.session_state["last_result"]

        if "error" in result:
            st.error(f"解析錯誤：{result['error']}")
            st.code(result.get("raw_response", ""), language=None)
        else:
            store = result.get("store_name", {})
            st.markdown(f"""
            <div style="background:#f8f9fa;border-left:4px solid #D63031;
                        padding:1rem 1.5rem;border-radius:0.5rem;margin:0.5rem 0">
                <strong>🏪 {store.get('chinese','N/A')}</strong>
                （{store.get('japanese','')}）<br>
                📅 {result.get('date','N/A')} &nbsp; ⏰ {result.get('time','N/A')}
            </div>""", unsafe_allow_html=True)

            items = result.get("items", [])
            if items:
                import pandas as pd
                df = pd.DataFrame([{
                    "品項（中文）": i.get("name_chinese", ""),
                    "品項（日文）": i.get("name_japanese", ""),
                    "數量": i.get("quantity", 1),
                    "小計（¥）": f"¥{i.get('subtotal_jpy', 0):,}",
                    "小計（NT$）": f"NT${i.get('subtotal_twd', 0):,}",
                    "稅率": i.get("tax_category", ""),
                } for i in items])
                st.dataframe(df, use_container_width=True, hide_index=True)

            total_jpy = result.get("total_amount_jpy", 0) or 0
            total_twd = result.get("total_amount_twd", 0) or 0
            st.markdown(f"""
            <div class="total-box">
                <div style="font-size:0.9rem;opacity:0.9">總金額</div>
                <div style="font-size:2rem;font-weight:bold">¥{total_jpy:,}</div>
                <div style="font-size:1.2rem;opacity:0.9">≈ NT$ {total_twd:,}</div>
                <div style="font-size:0.8rem;margin-top:0.5rem;opacity:0.8">
                    💳 {result.get('payment_method','N/A')} &nbsp;|&nbsp;
                    🏷️ {st.session_state.get('last_category','')}
                </div>
            </div>""", unsafe_allow_html=True)

            st.divider()

            c1, c2, c3 = st.columns(3)
            with c1:
                st.download_button(
                    "📥 下載 JSON",
                    data=json.dumps(result, ensure_ascii=False, indent=2),
                    file_name=f"receipt_{result.get('date','unknown')}.json",
                    mime="application/json",
                    use_container_width=True,
                )
            with c2:
                if st.session_state.get("notion_synced"):
                    st.success("✅ 已同步")
                    url = st.session_state.get("notion_page_url", "")
                    if url:
                        st.markdown(f"[🔗 開啟 Notion 頁面]({url})")
                else:
                    if st.button("📝 同步到 Notion", type="primary",
                                 use_container_width=True):
                        if not notion_token:
                            st.error("❌ 請先設定 Notion Token（見左側側欄）")
                        else:
                            _do_notion_sync(   # ← 此時函數已定義 ✅
                                result,
                                st.session_state.get("last_category", category),
                                notion_token,
                                jpy_rate,
                            )
            with c3:
                if st.button("🔄 重新辨識", use_container_width=True):
                    for k in ["last_result", "notion_synced", "notion_page_url"]:
                        st.session_state.pop(k, None)
                    st.rerun()
    else:
        st.info("👈 上傳收據並點擊「開始辨識」")


# ─────────────────────────────────────────────
#  底部：原始 JSON
# ─────────────────────────────────────────────
if "last_result" in st.session_state:
    with st.expander("🔧 查看原始 JSON"):
        st.json(st.session_state["last_result"])
