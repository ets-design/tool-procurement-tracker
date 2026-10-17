import os
import json
import uuid
from datetime import datetime
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import gspread
from google.oauth2.service_account import Credentials

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Workshop Tool Procurement & Price Tracker",
    page_icon="🛠️",
    layout="wide"
)

# --- CONSTANTS & SCHEMA DEFINITIONS ---
VAT_RATE = 0.18  # 18% Israeli VAT

TAB_SCHEMAS = {
    "wishlist": [
        "item_id", "tool_name", "exact_model_or_asin", "workshop_task",
        "client_project", "importance", "urgency", "price_tier",
        "230v_only", "target_price", "manual_local_quote",
        "used_market_price", "purchased_status"
    ],
    "item_sources": [
        "item_id", "store_name", "url"
    ],
    "price_history": [
        "item_id", "date", "store_name", "currency",
        "raw_price", "shipping_cost", "landed_ils_price", "is_out_of_stock"
    ],
    "purchased_archive": [
        "item_id", "tool_name", "exact_model_or_asin", "workshop_task",
        "client_project", "importance", "urgency", "price_tier",
        "230v_only", "target_price", "manual_local_quote",
        "used_market_price", "purchased_status", "date_purchased", "final_price_paid"
    ]
}

WORKSHOP_TASKS = [
    "Joinery", "Rough Milling", "Sanding & Finishing", "Sharpening",
    "Layout & Measuring", "Dust Collection", "Routing & Shaping",
    "Assembly & Clamping", "Transport & Site", "General Shop"
]

IMPORTANCE_LEVELS = ["Essential", "Upgrade", "Nice-to-Have"]
URGENCY_LEVELS = ["Immediate", "30 Days", "Long-term"]
PRICE_TIERS = [
    "Small Consumable (<₪300)",
    "Hand / Power Tool (₪300–₪3,000)",
    "Heavy Machinery (>₪3,000)"
]

PRESET_STORES = [
    "Amazon (Auto Multi-Locale)",
    "Dictum (DE)",
    "Axminster Tools (UK)",
    "Knives & Tools (EU)",
    "Hiomakivi (FI)",
    "Fine Tools / Dieter Schmid (DE)",
    "Lee Valley (CA/US)",
    "Eli Taieb (IL)",
    "Dimar Online (IL)",
    "ElectroSlil (IL)",
    "M.G. HaTzafon (IL)",
    "Dovetools (IL)",
    "Dror Tools (IL)",
    "Z-Tools (IL)",
    "eBay",
    "Other / Custom Store"
]

# --- GOOGLE SHEETS CONNECTION & CACHE ---
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])
    elif os.environ.get("GCP_SERVICE_ACCOUNT_JSON"):
        creds_dict = json.loads(os.environ["GCP_SERVICE_ACCOUNT_JSON"])
    else:
        st.error("Missing Google Cloud credentials! Add `gcp_service_account` to Streamlit Secrets.")
        st.stop()

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)


def get_spreadsheet():
    client = get_gspread_client()
    sheet_id = st.secrets.get("SHEET_ID", os.environ.get("SHEET_ID", ""))
    if not sheet_id:
        st.error("Missing `SHEET_ID` in Streamlit Secrets or environment variables.")
        st.stop()
    return client.open_by_key(sheet_id)


def ensure_tabs_initialized(sh):
    existing_titles = [ws.title for ws in sh.worksheets()]
    for tab_name, headers in TAB_SCHEMAS.items():
        if tab_name not in existing_titles:
            ws = sh.add_worksheet(title=tab_name, rows=1000, cols=max(20, len(headers)))
            ws.append_row(headers)
        else:
            ws = sh.worksheet(tab_name)
            first_row = ws.row_values(1)
            if not first_row:
                ws.append_row(headers)


@st.cache_data(ttl=600)
def load_all_tables():
    sh = get_spreadsheet()
    ensure_tabs_initialized(sh)
    data = {}
    for tab_name, headers in TAB_SCHEMAS.items():
        ws = sh.worksheet(tab_name)
        records = ws.get_all_records(expected_headers=headers)
        df = pd.DataFrame(records)
        if df.empty:
            df = pd.DataFrame(columns=headers)
        else:
            for h in headers:
                if h not in df.columns:
                    df[h] = ""
            df = df[headers]
        data[tab_name] = df
    return data


def clear_cache_and_reload():
    st.cache_data.clear()
    st.rerun()


def save_dataframe_to_tab(tab_name: str, df: pd.DataFrame):
    sh = get_spreadsheet()
    ws = sh.worksheet(tab_name)
    headers = TAB_SCHEMAS[tab_name]
    out_df = df.copy()
    for h in headers:
        if h not in out_df.columns:
            out_df[h] = ""
    out_df = out_df[headers].fillna("")
    # Convert booleans and numbers cleanly for gspread
    values = [headers] + out_df.astype(object).values.tolist()
    ws.clear()
    ws.update(values, value_input_option="USER_ENTERED")


def get_saved_monthly_budget(sources_df: pd.DataFrame) -> float:
    cfg = sources_df[sources_df["item_id"] == "CONFIG_BUDGET"]
    if not cfg.empty:
        try:
            return float(cfg.iloc[-1]["url"])
        except ValueError:
            return 3500.0
    return 3500.0


def update_saved_monthly_budget(sources_df: pd.DataFrame, new_budget: float):
    clean_df = sources_df[sources_df["item_id"] != "CONFIG_BUDGET"].copy()
    cfg_row = pd.DataFrame([{
        "item_id": "CONFIG_BUDGET",
        "store_name": "MONTHLY_BUDGET_ILS",
        "url": str(round(new_budget, 2))
    }])
    updated = pd.concat([clean_df, cfg_row], ignore_index=True)
    save_dataframe_to_tab("item_sources", updated)
    clear_cache_and_reload()


# --- HELPER LOGIC: TYPING & PRIORITY SCORING ---
def parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().upper() in ("TRUE", "1", "YES", "Y")


def clean_tables(raw_data: dict):
    wl = raw_data["wishlist"].copy()
    src = raw_data["item_sources"].copy()
    ph = raw_data["price_history"].copy()
    arch = raw_data["purchased_archive"].copy()

    # Filter out system config row from item_sources
    budget_val = get_saved_monthly_budget(src)
    src = src[src["item_id"] != "CONFIG_BUDGET"].copy()

    if not wl.empty:
        wl["230v_only"] = wl["230v_only"].apply(parse_bool)
        for col in ["target_price", "manual_local_quote", "used_market_price"]:
            wl[col] = pd.to_numeric(wl[col], errors="coerce").fillna(0.0)
        wl["purchased_status"] = wl["purchased_status"].astype(str).str.strip()
        wl = wl[wl["purchased_status"].str.upper() != "PURCHASED"].copy()

    if not ph.empty:
        for col in ["raw_price", "shipping_cost", "landed_ils_price"]:
            ph[col] = pd.to_numeric(ph[col], errors="coerce").fillna(0.0)
        ph["is_out_of_stock"] = ph["is_out_of_stock"].apply(parse_bool)
        ph["date_dt"] = pd.to_datetime(ph["date"], errors="coerce")

    if not arch.empty:
        for col in ["target_price", "manual_local_quote", "used_market_price", "final_price_paid"]:
            arch[col] = pd.to_numeric(arch[col], errors="coerce").fillna(0.0)

    return wl, src, ph, arch, budget_val


def compute_priority_dashboard(wl: pd.DataFrame, ph: pd.DataFrame, net_vat_mode: bool) -> pd.DataFrame:
    if wl.empty:
        return pd.DataFrame()

    urgency_weights = {"Immediate": 40, "30 Days": 25, "Long-term": 10}
    importance_weights = {"Essential": 35, "Upgrade": 20, "Nice-to-Have": 10}
    vat_divisor = (1.0 + VAT_RATE) if net_vat_mode else 1.0

    rows = []
    for _, item in wl.iterrows():
        iid = str(item["item_id"])
        item_ph = ph[ph["item_id"].astype(str) == iid].copy()

        best_online_price = None
        best_online_store = "No Data Yet"
        avg_historical_price = None
        all_oos = False

        if not item_ph.empty:
            # Get latest record per store
            item_ph = item_ph.sort_values("date_dt", ascending=True)
            latest_per_store = item_ph.groupby("store_name", as_index=False).last()
            in_stock = latest_per_store[
                (~latest_per_store["is_out_of_stock"]) & (latest_per_store["landed_ils_price"] > 0)
            ]
            if not in_stock.empty:
                min_idx = in_stock["landed_ils_price"].idxmin()
                best_online_price = float(in_stock.loc[min_idx, "landed_ils_price"])
                best_online_store = str(in_stock.loc[min_idx, "store_name"])
                avg_historical_price = float(item_ph[item_ph["landed_ils_price"] > 0]["landed_ils_price"].mean())
            else:
                all_oos = True

        # Apply Net Business Cost (Excl. VAT) adjustment if toggled
        adj_online = round(best_online_price / vat_divisor, 2) if best_online_price else 0.0
        adj_target = round(float(item["target_price"]) / vat_divisor, 2) if item["target_price"] > 0 else 0.0
        adj_local = round(float(item["manual_local_quote"]) / vat_divisor, 2) if item["manual_local_quote"] > 0 else 0.0
        # Used market generally has no VAT invoice (private sale), so keep raw used price
        used_price = round(float(item["used_market_price"]), 2) if item["used_market_price"] > 0 else 0.0

        # Determine overall best acquisition option
        candidates = []
        if adj_online > 0:
            candidates.append((adj_online, f"Online ({best_online_store})"))
        if adj_local > 0:
            candidates.append((adj_local, "Local Quote (IL)"))
        if used_price > 0:
            candidates.append((used_price, "Used Market (Yad2/FB)"))

        if candidates:
            best_overall_price, best_overall_channel = min(candidates, key=lambda x: x[0])
        else:
            best_overall_price, best_overall_channel = 0.0, "Pending Check"

        # Calculate Discount % vs Target or Historical Average
        reference_price = adj_target if adj_target > 0 else (
            round(avg_historical_price / vat_divisor, 2) if avg_historical_price else 0.0
        )
        if reference_price > 0 and best_overall_price > 0:
            discount_pct = round(((reference_price - best_overall_price) / reference_price) * 100.0, 1)
        else:
            discount_pct = 0.0

        # Calculate Priority Score (0 - 100)
        u_score = urgency_weights.get(str(item["urgency"]), 15)
        i_score = importance_weights.get(str(item["importance"]), 15)
        deal_bonus = max(0.0, min(25.0, discount_pct * 1.25)) if best_overall_price > 0 else 0.0
        priority_score = round(u_score + i_score + deal_bonus, 1)

        # Status badge
        if all_oos and adj_local == 0:
            deal_status = "🔴 Out of Stock"
        elif best_overall_price > 0 and adj_target > 0 and best_overall_price <= adj_target:
            deal_status = "🟢 Below Target!"
        elif best_overall_price > 0 and adj_target > 0 and best_overall_price <= adj_target * 1.10:
            deal_status = "🟡 Near Target (≤10%)"
        elif best_overall_price > 0:
            deal_status = "⚪ Above Target"
        else:
            deal_status = "⏳ Awaiting Scrape"

        rows.append({
            "item_id": iid,
            "Priority Score": priority_score,
            "Status": deal_status,
            "Tool Name": item["tool_name"],
            "Model / ASIN": item["exact_model_or_asin"],
            "Task": item["workshop_task"],
            "Project": item["client_project"],
            "Importance": item["importance"],
            "Urgency": item["urgency"],
            "Price Tier": item["price_tier"],
            "230V Only": "⚡ 230V" if item["230v_only"] else "Global",
            "Target (₪)": adj_target,
            "Best Landed Online (₪)": adj_online,
            "Best Online Store": best_online_store,
            "Local Quote (₪)": adj_local,
            "Used Market (₪)": used_price,
            "Lowest Overall (₪)": best_overall_price,
            "Best Channel": best_overall_channel,
            "Vs. Target (%)": -discount_pct  # Negative means % below target
        })

    dash_df = pd.DataFrame(rows)
    dash_df = dash_df.sort_values(by=["Priority Score", "Vs. Target (%)"], ascending=[False, True]).reset_index(drop=True)
    return dash_df


# --- LOAD DATA ---
raw_tables = load_all_tables()
wishlist_df, sources_df, history_df, archive_df, saved_budget = clean_tables(raw_tables)

# --- HEADER & TOP CONTROLS ---
st.title("🛠️ Smart Tool Procurement & Price-Tracking Hub")
st.caption("Automated Landed ILS (₪) Price Intelligence • Multi-Amazon ASIN Multiplier • Workshop Priority Ranker")

top_c1, top_c2, top_c3, top_c4 = st.columns([2, 2, 2, 2])

with top_c1:
    new_budget_val = st.number_input(
        "💰 Current Monthly Tool Budget (₪)",
        min_value=0.0,
        value=float(saved_budget),
        step=250.0,
        help="Used by the dashboard and daily email advisor to recommend the optimal bundle of tools to buy now."
    )
    if new_budget_val != saved_budget:
        if st.button("💾 Save New Budget"):
            update_saved_monthly_budget(raw_tables["item_sources"], new_budget_val)

with top_c2:
    price_view_mode = st.radio(
        "🧾 Accounting Price View",
        options=["Out-of-Pocket Price (incl. 18% VAT)", "Net Business Cost (excl. VAT)"],
        index=0,
        help="Toggle between total cashflow price at checkout vs. net cost after business VAT deduction."
    )
    is_net_vat = price_view_mode.startswith("Net Business")

with top_c3:
    if st.button("🔄 Refresh Live Data from Google Sheets", use_container_width=True):
        clear_cache_and_reload()

dash_df = compute_priority_dashboard(wishlist_df, history_df, net_vat_mode=is_net_vat)

# Compute Budget Recommendations
recommended_tools = []
running_spend = 0.0
if not dash_df.empty:
    for _, r in dash_df.iterrows():
        price_to_use = r["Lowest Overall (₪)"]
        if 0 < price_to_use <= (new_budget_val - running_spend):
            recommended_tools.append(f"{r['Tool Name']} (₪{price_to_use:,.0f} via {r['Best Channel']})")
            running_spend += price_to_use

with top_c4:
    below_target_count = len(dash_df[dash_df["Status"] == "🟢 Below Target!"]) if not dash_df.empty else 0
    st.metric(
        label="🔥 Active Deals Below Target",
        value=f"{below_target_count} / {len(dash_df)} Tools",
        delta=f"₪{new_budget_val - running_spend:,.0f} budget remaining after recs"
    )

if recommended_tools:
    st.success(
        f"**🎯 Recommended 'Buy Next' Allocation (Within ₪{new_budget_val:,.0f} Budget):** "
        + "  •  ".join(recommended_tools)
        + f"  *(Total: ₪{running_spend:,.0f})*"
    )

st.markdown("---")

# --- MAIN NAVIGATION TABS ---
tab_dash, tab_manage, tab_archive = st.tabs([
    "📊 Priority Wishlist & Price Graphs",
    "➕ Add Item & Manage Store Links (10+ Stores)",
    "🗄️ Purchased Archive"
])

# ==============================================================================
# TAB 1: PRIORITY WISHLIST & INTERACTIVE GRAPHS
# ==============================================================================
with tab_dash:
    if dash_df.empty:
        st.info("Your wishlist is currently empty! Switch to the **➕ Add Item & Manage Store Links** tab to add your first tool.")
    else:
        # --- FILTERS BAR ---
        st.subheader("🔍 Filter & Sort Wishlist")
        f_col1, f_col2, f_col3, f_col4, f_col5 = st.columns(5)
        with f_col1:
            f_task = st.multiselect("Workshop Task", options=sorted(dash_df["Task"].unique()), default=[])
        with f_col2:
            f_imp = st.multiselect("Importance", options=IMPORTANCE_LEVELS, default=[])
        with f_col3:
            f_urg = st.multiselect("Urgency", options=URGENCY_LEVELS, default=[])
        with f_col4:
            f_tier = st.multiselect("Price Tier", options=PRICE_TIERS, default=[])
        with f_col5:
            f_proj = st.multiselect("Client Project", options=sorted(dash_df["Project"].unique()), default=[])

        filtered_df = dash_df.copy()
        if f_task:
            filtered_df = filtered_df[filtered_df["Task"].isin(f_task)]
        if f_imp:
            filtered_df = filtered_df[filtered_df["Importance"].isin(f_imp)]
        if f_urg:
            filtered_df = filtered_df[filtered_df["Urgency"].isin(f_urg)]
        if f_tier:
            filtered_df = filtered_df[filtered_df["Price Tier"].isin(f_tier)]
        if f_proj:
            filtered_df = filtered_df[filtered_df["Project"].isin(f_proj)]

        # --- CONDITIONAL FORMATTING STYLER ---
        def highlight_deals(row):
            styles = [""] * len(row)
            status = row["Status"]
            if status == "🟢 Below Target!":
                styles = ["background-color: rgba(34, 197, 94, 0.18); font-weight: 600;" for _ in row]
            elif status == "🟡 Near Target (≤10%)":
                styles = ["background-color: rgba(234, 179, 8, 0.15);" for _ in row]
            elif status == "🔴 Out of Stock":
                styles = ["background-color: rgba(239, 68, 68, 0.15); color: #ef4444;" for _ in row]
            return styles

        display_cols = [
            "Priority Score", "Status", "Tool Name", "Model / ASIN", "Task",
            "Project", "Importance", "Urgency", "230V Only", "Target (₪)",
            "Best Landed Online (₪)", "Best Online Store", "Local Quote (₪)",
            "Used Market (₪)", "Lowest Overall (₪)", "Best Channel"
        ]

        styled_df = (
            filtered_df[display_cols]
            .style.apply(highlight_deals, axis=1)
            .format({
                "Priority Score": "{:.1f}",
                "Target (₪)": "₪{:,.0f}",
                "Best Landed Online (₪)": "₪{:,.0f}",
                "Local Quote (₪)": "₪{:,.0f}",
                "Used Market (₪)": "₪{:,.0f}",
                "Lowest Overall (₪)": "₪{:,.0f}"
            })
        )

        st.dataframe(styled_df, use_container_width=True, hide_index=True, height=400)

        st.markdown("---")

        # --- INTERACTIVE PRICE GRAPH & PURCHASE ACTION ---
        g_col, act_col = st.columns([2, 1])

        with g_col:
            st.subheader("📈 Multi-Store Landed Price History (₪)")
            tool_options = {
                f"{r['Tool Name']} ({r['Model / ASIN']}) — Lowest: ₪{r['Lowest Overall (₪)']:,.0f}": r["item_id"]
                for _, r in filtered_df.iterrows()
            }
            selected_label = st.selectbox("Select a tool to inspect its historical landed price trajectory:", options=list(tool_options.keys()))
            selected_item_id = tool_options[selected_label]
            selected_row = dash_df[dash_df["item_id"] == selected_item_id].iloc[0]

            item_hist = history_df[
                (history_df["item_id"].astype(str) == str(selected_item_id)) &
                (history_df["landed_ils_price"] > 0)
            ].copy()

            if item_hist.empty:
                st.info("No historical price points logged yet for this item. Once `scraper.py` runs, daily price lines for all stores and Amazon locales will appear here.")
            else:
                vat_div = (1.0 + VAT_RATE) if is_net_vat else 1.0
                item_hist["Display Price (₪)"] = (item_hist["landed_ils_price"] / vat_div).round(2)
                item_hist = item_hist.sort_values("date_dt")

                fig = px.line(
                    item_hist,
                    x="date",
                    y="Display Price (₪)",
                    color="store_name",
                    markers=True,
                    title=f"Landed Price History: {selected_row['Tool Name']} ({'Net Excl. VAT' if is_net_vat else 'Incl. VAT'})",
                    labels={"date": "Date Checked", "Display Price (₪)": "Landed Price (₪)", "store_name": "Store / Locale"}
                )
                # Reference horizontal lines for Target, Local Quote, Used Market
                if selected_row["Target (₪)"] > 0:
                    fig.add_hline(
                        y=selected_row["Target (₪)"],
                        line_dash="dash",
                        line_color="green",
                        annotation_text=f"Target: ₪{selected_row['Target (₪)']:,.0f}"
                    )
                if selected_row["Local Quote (₪)"] > 0:
                    fig.add_hline(
                        y=selected_row["Local Quote (₪)"],
                        line_dash="dot",
                        line_color="orange",
                        annotation_text=f"Local Quote: ₪{selected_row['Local Quote (₪)']:,.0f}"
                    )
                if selected_row["Used Market (₪)"] > 0:
                    fig.add_hline(
                        y=selected_row["Used Market (₪)"],
                        line_dash="dashdot",
                        line_color="purple",
                        annotation_text=f"Used Market: ₪{selected_row['Used Market (₪)']:,.0f}"
                    )
                fig.update_layout(height=420, legend_title_text="Store")
                st.plotly_chart(fig, use_container_width=True)

        with act_col:
            st.subheader("✅ Mark Tool as Purchased")
            st.write(f"**Selected Tool:** {selected_row['Tool Name']}")
            st.write(f"**Best Current Deal:** ₪{selected_row['Lowest Overall (₪)']:,.0f} via *{selected_row['Best Channel']}*")

            # Show direct clickable store links for the selected item
            item_links = sources_df[sources_df["item_id"].astype(str) == str(selected_item_id)]
            if not item_links.empty:
                st.markdown("**🔗 Saved Store Links:**")
                for _, srow in item_links.iterrows():
                    st.markdown(f"- [{srow['store_name']}]({srow['url']})")

            with st.form("mark_purchased_form"):
                default_paid = float(selected_row["Lowest Overall (₪)"]) if selected_row["Lowest Overall (₪)"] > 0 else float(selected_row["Target (₪)"])
                final_paid = st.number_input("Final Price Paid (₪)", min_value=0.0, value=default_paid, step=10.0)
                purchase_date = st.date_input("Date Purchased", value=datetime.today())
                confirm_buy = st.form_submit_button("Purchased ✓ (Move to Archive)", use_container_width=True)

                if confirm_buy:
                    full_wl = raw_tables["wishlist"].copy()
                    target_mask = full_wl["item_id"].astype(str) == str(selected_item_id)
                    if target_mask.any():
                        bought_row = full_wl[target_mask].iloc[0].to_dict()
                        bought_row["purchased_status"] = "PURCHASED"
                        bought_row["date_purchased"] = purchase_date.strftime("%Y-%m-%d")
                        bought_row["final_price_paid"] = round(final_paid, 2)

                        # Append to purchased_archive
                        new_arch = pd.concat([raw_tables["purchased_archive"], pd.DataFrame([bought_row])], ignore_index=True)
                        save_dataframe_to_tab("purchased_archive", new_arch)

                        # Remove from active wishlist
                        remaining_wl = full_wl[~target_mask].copy()
                        save_dataframe_to_tab("wishlist", remaining_wl)

                        st.success(f"Moved '{selected_row['Tool Name']}' to Purchased Archive!")
                        clear_cache_and_reload()


# ==============================================================================
# TAB 2: ADD ITEM & MANAGE STORE LINKS (1 TO 10+ STORES)
# ==============================================================================
with tab_manage:
    st.subheader("➕ Add New Tool to Wishlist")
    st.caption(
        "Pro-Tip: Enter an Amazon ASIN (e.g., `B001V9K4FC`) or paste any single Amazon link, and the morning scraper "
        "will automatically check Amazon US, UK, DE, FR, IT, and ES!"
    )

    # Initialize session state for dynamic store rows in the Add Item form
    if "new_item_links" not in st.session_state:
        st.session_state.new_item_links = pd.DataFrame([
            {"store_name": "Amazon (Auto Multi-Locale)", "url": ""},
            {"store_name": "Dictum (DE)", "url": ""},
            {"store_name": "Eli Taieb (IL)", "url": ""}
        ])

    c1, c2, c3, c4 = st.columns(4)
    n_name = c1.text_input("Tool Name *", placeholder="e.g., Festool Domino DF 500 Q-Plus")
    n_model = c2.text_input("Exact Brand & Model # or ASIN *", placeholder="e.g., B001V9K4FC or Atoma #400")
    n_task = c3.selectbox("Workshop Task", options=WORKSHOP_TASKS)
    n_proj = c4.text_input("Client Project", value="General Shop", placeholder="e.g., Noa's Table")

    c5, c6, c7, c8 = st.columns(4)
    n_imp = c5.selectbox("Importance", options=IMPORTANCE_LEVELS)
    n_urg = c6.selectbox("Urgency", options=URGENCY_LEVELS)
    n_tier = c7.selectbox("Price Tier", options=PRICE_TIERS)
    n_230v = c8.checkbox("⚡ 230V Only (Ignore US 110V Corded Listings)", value=False)

    c9, c10, c11 = st.columns(3)
    n_target = c9.number_input("Target Landed Price (₪) *", min_value=0.0, value=500.0, step=25.0)
    n_local = c10.number_input("Manual Phone/WhatsApp Local Quote (₪)", min_value=0.0, value=0.0, step=25.0)
    n_used = c11.number_input("Used Market Benchmark (Yad2 / FB) (₪)", min_value=0.0, value=0.0, step=25.0)

    st.markdown("#### 🌐 Attach Store Product Links (Add 1 to 10+ Stores)")
    st.caption("Click the `+` row at the bottom of the table to add as many international or Israeli store links as you want.")

    edited_new_links = st.data_editor(
        st.session_state.new_item_links,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key="add_links_editor",
        column_config={
            "store_name": st.column_config.SelectboxColumn(
                "Store Name (Select or Type in URL)",
                options=PRESET_STORES,
                required=True,
                width="medium"
            ),
            "url": st.column_config.TextColumn(
                "Direct Product URL",
                help="Paste the exact product page URL",
                width="large"
            )
        }
    )

    if st.button("💾 Save Tool & Store Links to Wishlist", type="primary"):
        if not n_name.strip():
            st.error("Please enter a Tool Name.")
        else:
            new_id = f"TOOL-{uuid.uuid4().hex[:6].upper()}"
            new_wl_row = pd.DataFrame([{
                "item_id": new_id,
                "tool_name": n_name.strip(),
                "exact_model_or_asin": n_model.strip(),
                "workshop_task": n_task,
                "client_project": n_proj.strip() or "General Shop",
                "importance": n_imp,
                "urgency": n_urg,
                "price_tier": n_tier,
                "230v_only": "TRUE" if n_230v else "FALSE",
                "target_price": round(n_target, 2),
                "manual_local_quote": round(n_local, 2),
                "used_market_price": round(n_used, 2),
                "purchased_status": "ACTIVE"
            }])

            updated_wl = pd.concat([raw_tables["wishlist"], new_wl_row], ignore_index=True)
            save_dataframe_to_tab("wishlist", updated_wl)

            # Save all non-empty URLs to item_sources
            valid_links = edited_new_links[
                edited_new_links["url"].astype(str).str.strip() != ""
            ].copy()
            if not valid_links.empty:
                valid_links["item_id"] = new_id
                valid_links = valid_links[["item_id", "store_name", "url"]]
                updated_src = pd.concat([raw_tables["item_sources"], valid_links], ignore_index=True)
                save_dataframe_to_tab("item_sources", updated_src)

            st.success(f"Added '{n_name}' ({new_id}) with {len(valid_links)} store links!")
            clear_cache_and_reload()

    st.markdown("---")
    st.subheader("✏️ Edit Existing Wishlist Items & Store Links")
    if not wishlist_df.empty:
        edit_wl = st.data_editor(
            wishlist_df,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key="edit_wl_grid",
            column_config={
                "item_id": st.column_config.TextColumn("ID", disabled=True, width="small"),
                "tool_name": st.column_config.TextColumn("Tool Name", width="medium"),
                "exact_model_or_asin": st.column_config.TextColumn("Model / ASIN", width="small"),
                "workshop_task": st.column_config.SelectboxColumn("Task", options=WORKSHOP_TASKS),
                "importance": st.column_config.SelectboxColumn("Importance", options=IMPORTANCE_LEVELS),
                "urgency": st.column_config.SelectboxColumn("Urgency", options=URGENCY_LEVELS),
                "price_tier": st.column_config.SelectboxColumn("Price Tier", options=PRICE_TIERS),
                "230v_only": st.column_config.CheckboxColumn("230V Only")
            }
        )
        if st.button("💾 Save Wishlist Edits"):
            save_dataframe_to_tab("wishlist", edit_wl)
            st.success("Wishlist updated!")
            clear_cache_and_reload()

        st.markdown("#### 🔗 Manage Store Links (`item_sources`)")
        edit_src = st.data_editor(
            sources_df,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key="edit_src_grid",
            column_config={
                "item_id": st.column_config.SelectboxColumn("Item ID", options=wishlist_df["item_id"].tolist(), width="small"),
                "store_name": st.column_config.TextColumn("Store Name", width="medium"),
                "url": st.column_config.LinkColumn("Product URL", width="medium")
            }
        )
        if st.button("💾 Save Store Link Changes"):
            # Preserve the CONFIG_BUDGET row when saving item_sources
            cfg_row = pd.DataFrame([{
                "item_id": "CONFIG_BUDGET",
                "store_name": "MONTHLY_BUDGET_ILS",
                "url": str(round(saved_budget, 2))
            }])
            combined_src = pd.concat([edit_src, cfg_row], ignore_index=True)
            save_dataframe_to_tab("item_sources", combined_src)
            st.success("Store links updated!")
            clear_cache_and_reload()


# ==============================================================================
# TAB 3: PURCHASED ARCHIVE
# ==============================================================================
with tab_archive:
    st.subheader("🗄️ Purchased Tool Archive & Business Expense Log")
    if archive_df.empty:
        st.info("No tools have been archived yet. When you buy a tool and click **Purchased ✓**, it will be recorded here.")
    else:
        total_invested = archive_df["final_price_paid"].sum()
        net_invested = total_invested / (1.0 + VAT_RATE)
        a_c1, a_c2, a_c3 = st.columns(3)
        a_c1.metric("Tools Acquired", len(archive_df))
        a_c2.metric("Total Out-of-Pocket Paid (incl. VAT)", f"₪{total_invested:,.2f}")
        a_c3.metric("Net Business Expense (excl. 18% VAT)", f"₪{net_invested:,.2f}")

        st.dataframe(
            archive_df[[
                "date_purchased", "tool_name", "exact_model_or_asin",
                "workshop_task", "client_project", "final_price_paid",
                "target_price", "manual_local_quote", "used_market_price"
            ]],
            use_container_width=True,
            hide_index=True
        )
