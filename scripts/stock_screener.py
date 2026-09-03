# -*- coding: utf-8 -*-
"""
screener-agent <-> Moomoo Quote API (get_stock_filter) スクリーニングスクリプト

*** このスクリプトの位置づけ ***
以前の screener-agent は WebSearch のみで動作し、検索結果の日付が本日のものか
（さらにはETタイムゾーンで書かれたものか）を毎回裏取りする必要があった。
このスクリプトは Moomoo Quote API (get_stock_filter / get_stock_basicinfo) から
直接、当日の前日比データを取得する。API自体が正確な現在値・前日比を返すため、
検索結果のように「この数値がいつのものか」を推測する必要がない。

WebSearchは、ここで抽出した銘柄について「なぜ動いたか（材料）」を調べる
補助用途にのみ screener-agent 側で使う。数値のスクリーニング自体はこのスクリプトが
API から直接行う。

*** get_stock_filter() の非自明な癖（実機で確認、2026-09-03） ***
- SimpleFilter / AccumulateFilter のいずれも、`is_no_filter` を明示的に
  `False` にしない限り、`filter_min`/`filter_max` を設定していてもAPI側で
  無視される（フィルタが一切かからず、結果にその列も出力されない）。
  デフォルトは `None` であり、`if self.is_no_filter is False:` という判定の
  ため `None` は「フィルタなし」として扱われる。この関数を呼ぶすべての
  フィルタで `is_no_filter = False` を明示すること。
- CHANGE_RATE / AMPLITUDE / VOLUME / TURNOVER / TURNOVER_RATE は
  「累積(Accumulate)属性」であり `AccumulateFilter`（`days` パラメータを取る。
  `days=1` で前日比）を使う。CUR_PRICE / VOLUME_RATIO / MARKET_VAL 等は
  「単純(Simple)属性」であり `SimpleFilter` を使う。取り違えると
  `TypeError` にはならず、単に条件が無視される・結果に値が出ないという
  静かな失敗になるため注意。
- get_stock_filter は SPAC のユニット/ワラント/ライツ（例:
  "US.ATLQU"、"US.PNAQ.U"）も除外せずに返す。これらは get_stock_basicinfo
  上も stock_type="STOCK" となり、stock_type だけでは選別できない。
  実機確認では stock_name に "ACQUISITION CORP" や "UNIT" "WARRANT" "RIGHT"
  等の語が含まれていたため、本スクリプトでは銘柄名の正規表現で除外する。
- ETF は get_stock_basicinfo の stock_type="ETF" で判別できる
  （例: US.SPY, US.QQQ）。OTC/ピンクシートは exchange_type="US_PINK" で
  判別できる。stock_type!="STOCK" または exchange_type="US_PINK" の銘柄は
  【除外】に回す。

このスクリプトが行う条件は .claude/agents/screener-agent.md の記述と
必ず一致させること。片方だけ変更しないこと。
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import moomoo as ft

HOST = "127.0.0.1"
PORT = 11111

NEW_YORK = ZoneInfo("America/New_York")

# CLAUDE.md / screener-agent.md と同じ値。変更する場合は両方揃えること。
DEFAULT_CHANGE_RATE_MIN = 5.0   # 値上がり率（前日比） %
DEFAULT_AMPLITUDE_MIN = 5.0     # 値幅（前日比、高値-安値ベース） %
DEFAULT_VOLUME_RATIO_MIN = 2.0  # 出来高比（当日出来高 / 平均出来高）倍
DEFAULT_PRICE_MIN = 5.0         # 株価 USD（ペニーストック除外）
DEFAULT_NUM = 100

# SPAC のユニット/ワラント/ライツ等、普通株でない銘柄を銘柄名から検知する。
# stock_type（STOCK/ETF/WARRANT等）だけでは判別できないことを実機で確認済み。
_EXCLUDE_NAME_RE = re.compile(
    r"ACQUISITION\s+CORP|\bUNITS?\b|\bWARRANTS?\b|\bRIGHTS?\b|\bBLANK\s+CHECK\b",
    re.IGNORECASE,
)


def _now_et():
    return datetime.now(NEW_YORK)


def _build_filters(change_rate_min, amplitude_min, volume_ratio_min, price_min):
    f_change = ft.AccumulateFilter()
    f_change.stock_field = ft.StockField.CHANGE_RATE
    f_change.filter_min = change_rate_min
    f_change.days = 1
    f_change.is_no_filter = False
    f_change.sort = ft.SortDir.DESCEND

    f_amp = ft.AccumulateFilter()
    f_amp.stock_field = ft.StockField.AMPLITUDE
    f_amp.filter_min = amplitude_min
    f_amp.days = 1
    f_amp.is_no_filter = False

    f_vr = ft.SimpleFilter()
    f_vr.stock_field = ft.StockField.VOLUME_RATIO
    f_vr.filter_min = volume_ratio_min
    f_vr.is_no_filter = False

    f_price = ft.SimpleFilter()
    f_price.stock_field = ft.StockField.CUR_PRICE
    f_price.filter_min = price_min
    f_price.is_no_filter = False

    return [f_change, f_amp, f_vr, f_price]


def screen(change_rate_min=DEFAULT_CHANGE_RATE_MIN, amplitude_min=DEFAULT_AMPLITUDE_MIN,
           volume_ratio_min=DEFAULT_VOLUME_RATIO_MIN, price_min=DEFAULT_PRICE_MIN,
           num=DEFAULT_NUM):
    """米国市場（NASDAQ/NYSE等）を対象に、当日の値上がり率・値幅・出来高比・株価で
    get_stock_filter を実行し、SPAC/ETF/OTCを除外した候補リストを返す。

    戻り値: (result_dict, error_message)
      result_dict は None、または以下のキーを持つ辞書:
        total_matches: APIが報告する条件一致の総数（このスクリプトの除外処理前）
        is_last_page: このスクリプトが取得した num 件で全件を取得できたか
        candidates: 除外後の候補リスト（dictのリスト）
        excluded: 除外した銘柄のリスト（dictのリスト、exclude_reason付き）
    """
    quote_ctx = ft.OpenQuoteContext(host=HOST, port=PORT)
    try:
        filters = _build_filters(change_rate_min, amplitude_min, volume_ratio_min, price_min)
        ret, data = quote_ctx.get_stock_filter(market=ft.Market.US, filter_list=filters, begin=0, num=num)
        if ret != ft.RET_OK:
            return None, f"get_stock_filter失敗: {data}"

        is_last_page, total_count, rows = data

        codes = [r.stock_code for r in rows]
        info_by_code = {}
        if codes:
            ret2, info = quote_ctx.get_stock_basicinfo(market=ft.Market.US, code_list=codes)
            if ret2 != ft.RET_OK:
                return None, f"get_stock_basicinfo失敗: {info}"
            for _, row in info.iterrows():
                info_by_code[row["code"]] = row

        candidates = []
        excluded = []
        for r in rows:
            code = r.stock_code
            name = r.stock_name
            change_rate = r.__dict__.get(("change_rate", 1))
            amplitude = r.__dict__.get(("amplitude", 1))
            volume_ratio = r.__dict__.get("volume_ratio")
            cur_price = r.__dict__.get("cur_price")

            info_row = info_by_code.get(code)
            stock_type = info_row["stock_type"] if info_row is not None else None
            exchange_type = info_row["exchange_type"] if info_row is not None else None

            item = {
                "code": code,
                "name": name,
                "change_rate": change_rate,
                "amplitude": amplitude,
                "volume_ratio": volume_ratio,
                "cur_price": cur_price,
                "stock_type": stock_type,
                "exchange_type": exchange_type,
            }

            reason = None
            if stock_type is not None and stock_type != "STOCK":
                reason = f"stock_type={stock_type}（普通株でないため除外）"
            elif exchange_type == "US_PINK":
                reason = "exchange_type=US_PINK（OTC/ピンクシートのため除外）"
            elif _EXCLUDE_NAME_RE.search(name or ""):
                reason = "銘柄名にUNIT/WARRANT/RIGHT/ACQUISITION CORP等を含む（SPACユニット等の疑いのため除外）"

            if reason:
                item["exclude_reason"] = reason
                excluded.append(item)
            else:
                candidates.append(item)

        return {
            "total_matches": total_count,
            "is_last_page": is_last_page,
            "candidates": candidates,
            "excluded": excluded,
        }, None
    finally:
        quote_ctx.close()


def _fmt_row(code, name, change_rate, amplitude, volume_ratio, cur_price):
    name_disp = (name or "")[:38]
    def _f(v, suffix=""):
        return f"{v:.2f}{suffix}" if isinstance(v, (int, float)) else "N/A"
    return (f"{code:<10} {name_disp:<38} "
            f"{_f(change_rate, '%'):>8} {_f(amplitude, '%'):>8} "
            f"{_f(volume_ratio, 'x'):>8} {_f(cur_price, ''):>10}")


def print_report(result, change_rate_min, amplitude_min, volume_ratio_min, price_min, num):
    now = _now_et()
    print(f"取得時刻(ET): {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"条件: 値上がり率(前日比)>={change_rate_min:.1f}% / 値幅(前日比)>={amplitude_min:.1f}% / "
          f"出来高比>={volume_ratio_min:.1f}倍 / 株価>=${price_min:.2f}")
    print(f"APIが報告する条件一致の総数: {result['total_matches']}件"
          f"（このスクリプトの取得件数上限: {num}件、"
          f"{'全件取得済み' if result['is_last_page'] else '上限に達したため一部のみ取得（--numを増やして再実行可能）'}）")
    print()

    cands = result["candidates"]
    print(f"【候補】除外後 {len(cands)}件")
    if cands:
        print(f"{'CODE':<10} {'NAME':<38} {'値上率':>8} {'値幅':>8} {'出来高比':>8} {'株価':>10}")
        for c in cands:
            print(_fmt_row(c["code"], c["name"], c["change_rate"], c["amplitude"],
                           c["volume_ratio"], c["cur_price"]))
    else:
        print("（該当なし）")
    print()

    exc = result["excluded"]
    print(f"【除外（SPACユニット/ETF/OTC等）】 {len(exc)}件")
    if exc:
        for e in exc:
            print(f"{e['code']:<10} {(e['name'] or '')[:38]:<38} {e['exclude_reason']}")
    else:
        print("（該当なし）")


def _build_parser():
    import argparse

    p = argparse.ArgumentParser(description="screener-agent <-> Moomoo Quote API (get_stock_filter)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("screen", help="当日の値上がり率・値幅・出来高比・株価でスクリーニングする")
    sp.add_argument("--change-rate-min", type=float, default=DEFAULT_CHANGE_RATE_MIN,
                     help="値上がり率(前日比)の下限 %% (既定: %(default)s)")
    sp.add_argument("--amplitude-min", type=float, default=DEFAULT_AMPLITUDE_MIN,
                     help="値幅(前日比)の下限 %% (既定: %(default)s)")
    sp.add_argument("--volume-ratio-min", type=float, default=DEFAULT_VOLUME_RATIO_MIN,
                     help="出来高比の下限 倍 (既定: %(default)s)")
    sp.add_argument("--price-min", type=float, default=DEFAULT_PRICE_MIN,
                     help="株価の下限 USD (既定: %(default)s)")
    sp.add_argument("--num", type=int, default=DEFAULT_NUM,
                     help="取得件数上限 (既定: %(default)s)")

    return p


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.cmd == "screen":
        result, err = screen(
            change_rate_min=args.change_rate_min,
            amplitude_min=args.amplitude_min,
            volume_ratio_min=args.volume_ratio_min,
            price_min=args.price_min,
            num=args.num,
        )
        if err:
            print("取得できません:", err)
            import sys
            sys.exit(1)
        print_report(result, args.change_rate_min, args.amplitude_min,
                     args.volume_ratio_min, args.price_min, args.num)


if __name__ == "__main__":
    main()
