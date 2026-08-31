# -*- coding: utf-8 -*-
"""
execution-guard <-> Moomoo Trade API 接続スクリプト

*** このセッションでの絶対条件 ***
trd_env は _trd_env() 内の ft.TrdEnv.SIMULATE にハードコードされている。
発注・キャンセルの実行関数はいずれも trd_env を引数として受け取らず、
必ずこの内部定数を使う。TrdEnv.REAL への切り替えパスはこのファイルに
一切実装していない。実弾取引が必要な場合は、このファイルとは別に、
利用者の明示的な指示のもとで新規に実装すること。

根拠（moomoo-api 10.2.6218、公式ドキュメントではなく実際のSDKソースを直接確認）:
  /Users/Japan-Kaz/Library/Python/3.9/lib/python/site-packages/moomoo/
    - trade/open_trade_context.py (place_order, modify_order, get_acc_list,
      acctradinginfo_query の実引数と戻り値)
    - common/constant.py (TrdEnv, OrderType, TrdSide, ModifyOrderOp)

実機確認済みの制約（2026-08-30、公式チュートリアルには出てこない可能性がある）:
  - このOpenD配下のペーパー口座では、US市場の銘柄をこのゲートの要件どおりに
    発注することができない。
      acc_id 433736 (HK, acc_type=CASH, trdmarket_auth=['HK']) → HK銘柄のみ発注可
      acc_id 433735 (US, acc_type=MARGIN, trdmarket_auth=['US']) → US銘柄は発注可だが
        acc_type=MARGINのため「信用取引でないこと」（確認5）で必ず拒否される
  - *** 米国市場移行後の未解決の制約 ***
    対象市場が米国になったが、US銘柄を扱える口座(433735)はMARGINであり、
    確認5を通過できない。CASHのUS口座が用意されるまで、このスクリプトから
    米国株を実発注することはできない。
    確認1〜4（停止指示・重複・ポジション上限・日次損失上限）はAPI接続なしで
    動作するため、ロジックの検証には引き続き使える。
    今回はHK銘柄(acc 433736)でexecution-guard→API→発注→キャンセルの
    配線そのものを検証している。

このスクリプトが行う確認は .claude/agents/execution-guard.md の記述と
必ず一致させること。片方だけ変更しないこと。
"""

import csv
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import moomoo as ft

HOST = "127.0.0.1"
PORT = 11111

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXECUTION_LOG = os.path.join(REPO_ROOT, "execution_log.csv")
GUARD_STATE = os.path.join(REPO_ROOT, "guard_state.json")
TRADES_CSV = os.path.join(REPO_ROOT, "trades.csv")

# 対象市場は米国（NASDAQ/NYSE）。「本日」はET基準のレギュラー取引日で判定する。
# CLAUDE.md / risk-manager.md / trade-logger.md と同じ基準。
NEW_YORK = ZoneInfo("America/New_York")

# CLAUDE.md / risk-manager.md / execution-guard.md と同じ値。
# 変更する場合は3ファイルすべてを揃えること。
DAILY_LOSS_LIMIT_JPY = 30_000
POSITION_CAP_JPY = 300_000
DUPLICATE_WINDOW_MINUTES = 5


def _trd_env():
    """常にSIMULATE。この関数の外でTrdEnv.REALを組み立てないこと。"""
    return ft.TrdEnv.SIMULATE


def _now_et():
    return datetime.now(NEW_YORK)


# --- guard_state.json（停止/再開） -----------------------------------------

def load_guard_state():
    if not os.path.exists(GUARD_STATE):
        return {"status": "ACTIVE", "updated_at": None, "reason": None}
    with open(GUARD_STATE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_guard_state(state):
    with open(GUARD_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def set_stopped(reason):
    save_guard_state({
        "status": "STOPPED",
        "updated_at": _now_et().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": reason or "利用者指示（理由未記載）",
    })


def set_resumed():
    save_guard_state({
        "status": "ACTIVE",
        "updated_at": _now_et().strftime("%Y-%m-%d %H:%M:%S"),
        "reason": None,
    })


# --- 確認1: 停止指示 ---------------------------------------------------------

def check_stop():
    state = load_guard_state()
    if state.get("status") == "STOPPED":
        return False, f"停止中（{state.get('updated_at')} / 理由: {state.get('reason')}）"
    return True, "該当なし"


# --- 確認2: 重複発注 ---------------------------------------------------------

def check_duplicate(ticker):
    if not os.path.exists(EXECUTION_LOG):
        return True, "該当なし（ログなし）"
    cutoff = _now_et() - timedelta(minutes=DUPLICATE_WINDOW_MINUTES)
    with open(EXECUTION_LOG, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("ticker") != ticker or row.get("decision") != "発注可":
                continue
            ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=NEW_YORK)
            if ts >= cutoff:
                return False, f"該当（前回発注可: {row['timestamp']}）"
    return True, "該当なし"


# --- 確認3: ポジション上限 ---------------------------------------------------

def check_position_cap(qty, price, fx_rate_to_jpy):
    value_jpy = qty * price * fx_rate_to_jpy
    ok = value_jpy <= POSITION_CAP_JPY
    return ok, f"想定ポジション金額 ¥{value_jpy:,.0f}（上限¥{POSITION_CAP_JPY:,}）", value_jpy


# --- 確認4: 日次損失上限 ------------------------------------------------------

def todays_pnl_jpy():
    """risk-manager.md と同じロジック: trades.csv の date列(ET基準の約定日)が
    本日と一致する行の pnl_jpy を合計する。
    日本時間で日付を跨いでも、ET基準で同一取引日なら同じ日として合算される。"""
    if not os.path.exists(TRADES_CSV):
        return 0.0, "trades.csvなし"
    today = _now_et().strftime("%Y-%m-%d")
    total = 0.0
    n = 0
    with open(TRADES_CSV, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("date") == today and row.get("pnl_jpy"):
                total += float(row["pnl_jpy"])
                n += 1
    return total, f"{n}件（{today}）"


def check_daily_loss_limit():
    total, detail = todays_pnl_jpy()
    if total <= -DAILY_LOSS_LIMIT_JPY:
        return False, f"当日損失合計 ¥{total:,.0f}（{detail}）が上限-¥{DAILY_LOSS_LIMIT_JPY:,}に到達"
    return True, f"当日損失合計 ¥{total:,.0f}（{detail}）、残り¥{DAILY_LOSS_LIMIT_JPY + total:,.0f}"


# --- 確認5: 信用取引でないこと / 確認6: SIMULATE確認 ------------------------

def _acc_row(trd_ctx, acc_id):
    ret, data = trd_ctx.get_acc_list()
    if ret != ft.RET_OK:
        return None, str(data)
    rows = data[data["acc_id"] == acc_id]
    if len(rows) == 0:
        return None, f"acc_id {acc_id} が見つかりません"
    return rows.iloc[0], None


def check_not_margin(trd_ctx, acc_id):
    row, err = _acc_row(trd_ctx, acc_id)
    if row is None:
        return False, err
    if row["acc_type"] != "CASH":
        return False, f"acc_type={row['acc_type']}（信用口座のため不可）"
    return True, "acc_type=CASH"


def check_trd_env_is_simulate(trd_ctx, acc_id):
    row, err = _acc_row(trd_ctx, acc_id)
    if row is None:
        return False, err
    if row["trd_env"] != ft.TrdEnv.SIMULATE:
        return False, f"trd_env={row['trd_env']}（SIMULATE以外のため停止）"
    return True, "trd_env=SIMULATE"


# --- 全チェック実行 -----------------------------------------------------------

def run_checks(trd_ctx, acc_id, ticker, qty, price, fx_rate_to_jpy):
    results = []

    ok, msg = check_stop()
    results.append(("確認1:停止状態", ok, msg))
    if not ok:
        return False, results, 0.0

    ok, msg = check_duplicate(ticker)
    results.append(("確認2:重複発注", ok, msg))
    if not ok:
        return False, results, 0.0

    ok, msg, value_jpy = check_position_cap(qty, price, fx_rate_to_jpy)
    results.append(("確認3:ポジション上限", ok, msg))
    if not ok:
        return False, results, value_jpy

    ok, msg = check_daily_loss_limit()
    results.append(("確認4:日次損失上限", ok, msg))
    if not ok:
        return False, results, value_jpy

    ok, msg = check_not_margin(trd_ctx, acc_id)
    results.append(("確認5:信用取引でないこと", ok, msg))
    if not ok:
        return False, results, value_jpy

    ok, msg = check_trd_env_is_simulate(trd_ctx, acc_id)
    results.append(("確認6:SIMULATE確認", ok, msg))
    if not ok:
        return False, results, value_jpy

    return True, results, value_jpy


def log_execution(ticker, direction, qty, price, position_value_jpy, decision, reason):
    exists = os.path.exists(EXECUTION_LOG)
    with open(EXECUTION_LOG, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp", "ticker", "direction", "qty", "price_usd",
                        "position_value_jpy", "decision", "reason"])
        w.writerow([
            _now_et().strftime("%Y-%m-%d %H:%M:%S"),
            ticker, direction, qty, price,
            f"{position_value_jpy:.0f}", decision, reason,
        ])


def place_order_guarded(acc_id, ticker, direction, qty, price, fx_rate_to_jpy):
    """全チェックを通過した場合のみ、SIMULATE環境で成行注文を出す。"""
    trd_ctx = ft.OpenSecTradeContext(host=HOST, port=PORT)
    try:
        ok, results, value_jpy = run_checks(trd_ctx, acc_id, ticker, qty, price, fx_rate_to_jpy)
        for name, c_ok, msg in results:
            print(f"[{'OK' if c_ok else 'NG'}] {name}: {msg}")

        if not ok:
            reason = "; ".join(f"{n}:{m}" for n, o, m in results if not o)
            log_execution(ticker, direction, qty, price, value_jpy, "発注不可", reason)
            print("最終判定: 発注不可")
            return None

        trd_side = ft.TrdSide.BUY if direction == "long" else ft.TrdSide.SELL
        ret, data = trd_ctx.place_order(
            price=price,
            qty=qty,
            code=ticker,
            trd_side=trd_side,
            order_type=ft.OrderType.MARKET,
            trd_env=_trd_env(),
            acc_id=acc_id,
            remark="execution-guard smoke test",
        )
        if ret != ft.RET_OK:
            log_execution(ticker, direction, qty, price, value_jpy, "発注不可", f"API拒否: {data}")
            print("発注API拒否:", data)
            return None

        order_id = data["order_id"].iloc[0]
        log_execution(ticker, direction, qty, price, value_jpy, "発注可", "全チェック通過")
        print("最終判定: 発注可 / order_id =", order_id)
        print(data.to_string())
        return order_id
    finally:
        trd_ctx.close()


def query_order(acc_id, order_id):
    trd_ctx = ft.OpenSecTradeContext(host=HOST, port=PORT)
    try:
        ret, data = trd_ctx.order_list_query(order_id=str(order_id), trd_env=_trd_env(), acc_id=acc_id)
        return ret, data
    finally:
        trd_ctx.close()


def cancel_order(acc_id, order_id):
    trd_ctx = ft.OpenSecTradeContext(host=HOST, port=PORT)
    try:
        ret, data = trd_ctx.modify_order(
            ft.ModifyOrderOp.CANCEL, str(order_id), 0, 0,
            trd_env=_trd_env(), acc_id=acc_id,
        )
        return ret, data
    finally:
        trd_ctx.close()


def close_position_market(acc_id, ticker, qty, price):
    """反対売買（成行SELL）でポジションを手仕舞いする。"""
    trd_ctx = ft.OpenSecTradeContext(host=HOST, port=PORT)
    try:
        ret, data = trd_ctx.place_order(
            price=price,
            qty=qty,
            code=ticker,
            trd_side=ft.TrdSide.SELL,
            order_type=ft.OrderType.MARKET,
            trd_env=_trd_env(),
            acc_id=acc_id,
            remark="execution-guard close",
        )
        return ret, data
    finally:
        trd_ctx.close()


# --- CLI ---------------------------------------------------------------------
# execution-guardエージェント（.claude/agents/execution-guard.md）はBashツールから
# このCLIを呼び出す。acc_idを省略した場合、確認5(CASHであること)を通過できる唯一の
# 口座であるHK/CASH口座(433736)をデフォルトとする。
# 米国株を扱うUS口座(433735)はMARGINのため確認5で拒否される。上記docstring参照。
# 対象市場が米国に移行しても、CASHのUS口座が用意されるまでこのデフォルトは変更しない
# （433735に変えても確認5で必ず止まるため、実発注は成立しない）。

DEFAULT_ACC_ID = 433736


def _build_parser():
    import argparse

    p = argparse.ArgumentParser(description="execution-guard <-> Moomoo Trade API")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_order_args(sp):
        sp.add_argument("--acc-id", type=int, default=DEFAULT_ACC_ID)
        sp.add_argument("--ticker", required=True)
        sp.add_argument("--direction", choices=["long", "short"], required=True)
        sp.add_argument("--qty", type=float, required=True)
        sp.add_argument("--price", type=float, required=True)
        sp.add_argument("--fx-rate", type=float, required=True,
                         help="対象銘柄の通貨からJPYへの換算レート（米国株ならUSD/JPY）")

    add_order_args(sub.add_parser("check", help="発注はせず全チェックのみ行う"))
    add_order_args(sub.add_parser("place", help="全チェック通過後、SIMULATE環境で成行発注する"))

    sp = sub.add_parser("cancel", help="未約定注文をキャンセルする")
    sp.add_argument("--acc-id", type=int, default=DEFAULT_ACC_ID)
    sp.add_argument("--order-id", required=True)

    sp = sub.add_parser("query", help="注文状態を照会する")
    sp.add_argument("--acc-id", type=int, default=DEFAULT_ACC_ID)
    sp.add_argument("--order-id", required=True)

    sp = sub.add_parser("close", help="反対売買（成行）でポジションを手仕舞いする")
    sp.add_argument("--acc-id", type=int, default=DEFAULT_ACC_ID)
    sp.add_argument("--ticker", required=True)
    sp.add_argument("--qty", type=float, required=True)
    sp.add_argument("--price", type=float, required=True)

    sp = sub.add_parser("stop", help="利用者の停止指示を記録する")
    sp.add_argument("--reason", default=None)

    sub.add_parser("resume", help="利用者の再開指示を記録する")

    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.cmd == "check":
        trd_ctx = ft.OpenSecTradeContext(host=HOST, port=PORT)
        try:
            ok, results, value_jpy = run_checks(
                trd_ctx, args.acc_id, args.ticker, args.qty, args.price, args.fx_rate)
        finally:
            trd_ctx.close()
        for name, c_ok, msg in results:
            print(f"[{'OK' if c_ok else 'NG'}] {name}: {msg}")
        print("最終判定:", "発注可" if ok else "発注不可")

    elif args.cmd == "place":
        order_id = place_order_guarded(
            args.acc_id, args.ticker, args.direction, args.qty, args.price, args.fx_rate)
        if order_id is None:
            sys.exit(1)

    elif args.cmd == "cancel":
        ret, data = cancel_order(args.acc_id, args.order_id)
        print(ret)
        print(data)
        if ret != ft.RET_OK:
            sys.exit(1)

    elif args.cmd == "query":
        ret, data = query_order(args.acc_id, args.order_id)
        print(ret)
        print(data.to_string() if ret == ft.RET_OK else data)
        if ret != ft.RET_OK:
            sys.exit(1)

    elif args.cmd == "close":
        ret, data = close_position_market(args.acc_id, args.ticker, args.qty, args.price)
        print(ret)
        print(data.to_string() if ret == ft.RET_OK else data)
        if ret != ft.RET_OK:
            sys.exit(1)

    elif args.cmd == "stop":
        set_stopped(args.reason)
        print("guard_state.json を STOPPED に更新しました。")

    elif args.cmd == "resume":
        set_resumed()
        print("guard_state.json を ACTIVE に更新しました。")


if __name__ == "__main__":
    main()
