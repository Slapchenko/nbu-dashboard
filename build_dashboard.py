#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_dashboard.py — збирає самодостатній HTML-дашборд з даних, завантажених nbu_fetch.py.

Читає:  data/banks_long.csv, data/system_long.csv, data/_meta.json
Пише:   dashboard.html  (один файл, працює офлайн, без інтернету і без бібліотек)

Запуск:
    python3 build_dashboard.py
    python3 build_dashboard.py --bank "оксі"          # який банк у фокусі
    python3 build_dashboard.py --list-indicators      # показати всі показники з API
"""

import argparse
import base64
import csv
import json
import os
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# Логотип банку. Якщо поруч зі скриптом немає файлу logo.*, він один раз
# завантажується звідси й зберігається локально — далі мережа вже не потрібна.
LOGO_URL = "https://oxibank.ua/wp-content/uploads/2025/01/logo.svg"


# --------------------------------------------------------------------- показники
# Набір banksfinrep використовує коди BS1_* (довідник:
# bank.gov.ua/admin_uploads/article/Data_set_fields_BS1.pdf).
# Спочатку шукаємо точний код зі списку codes, потім — за назвою, потім — за шаблоном коду.
# type="pct" означає відсоток: для нього не рахуються частка ринку й сума по системі.
METRICS = [
    # Основний показник розміру банку — ЧИСТІ активи: саме за ними НБУ ранжує банки
    # і саме вони збігаються з рівністю «зобов'язання + капітал».
    dict(key="assets", label="Чисті активи",
         codes=["BS1_AssetsNet"], name=[r"^активи\s*[-–—]\s*чисті активи"], code=[r"assetsnet$"], exclude=[]),
    dict(key="assets_total", label="Активи загальні (до резервів)",
         codes=["BS1_AssetsTotal"], name=[r"^активи\s*[-–—]\s*(усього|загальні) актив"], code=[r"assetstotal$"],
         exclude=[r"іноземн", r"валют"]),
    dict(key="liabilities", label="Зобов'язання",
         codes=["BS1_LiabTotal"], name=[r"^зобов.язання\s*[-–—]\s*усього зобов"], code=[r"liabtotal$"],
         exclude=[r"іноземн", r"валют"]),
    dict(key="equity", label="Власний капітал",
         codes=["BS1_CapitalTotal"], name=[r"^капітал\s*[-–—]\s*усього власного"], code=[r"capitaltotal$"],
         exclude=[]),
    dict(key="loans_total", label="Кредити клієнтам",
         codes=["BS1_AssetsLoansClients"], name=[r"кредити та заборгованість клієнтів$"],
         code=[r"loansclients$"], exclude=[r"резерв", r"іноземн"]),
    # Резерви під кредити потрібні для розрахунку чистого портфеля; у списку показників
    # їх не показуємо (hidden), щоб не засмічувати перемикач.
    dict(key="prov_loans_le", label="Резерви під кредити юрособам", hidden=True,
         codes=["BS1_AssetsProvLoansLE"], name=[r"резерви під знецінення кредитів.*юридичних осіб"],
         code=[r"provloansle$"], exclude=[]),
    dict(key="prov_loans_indiv", label="Резерви під кредити фізособам", hidden=True,
         codes=["BS1_AssetsProvLoansIndiv", "BS1_AssetsProvLoanslndiv"],
         name=[r"резерви під знецінення кредитів.*фізичних осіб"], code=[r"provloansindiv$"], exclude=[]),
    dict(key="loans_corp", label="Кредити юридичним особам",
         codes=["BS1_AssetsLoansLE"], name=[r"кредити та заборгованість клієнтів-?\s*юридичних осіб$"],
         code=[r"loansle$"], exclude=[r"резерв", r"іноземн", r"валют"]),
    dict(key="loans_retail", label="Кредити фізичним особам",
         codes=["BS1_AssetsLoansIndiv", "BS1_AssetsLoanslndiv"],
         name=[r"кредити та заборгованість клієнтів-?\s*фізичних осіб$"], code=[r"loansindiv$"],
         exclude=[r"резерв", r"іноземн", r"валют"]),
    dict(key="dep_total", label="Кошти клієнтів",
         codes=["BS1_LiabCust"], name=[r"^зобов.язання\s*[-–—]\s*кошти клієнтів$"], code=[r"liabcust$"],
         exclude=[r"іноземн", r"вимог", r"фізичн", r"юридичн"]),
    dict(key="dep_retail", label="Кошти фізичних осіб",
         codes=["BS1_LiabIndiv", "BS1_Liablndiv"],
         name=[r"кошти клієнтів\s*-?\s*фізичних осіб$"], code=[r"liabindiv$"],
         exclude=[r"вимог", r"іноземн", r"валют"]),
    dict(key="dep_corp", label="Кошти юросіб і небанківських фінустанов",
         codes=["BS1_LiabLE"], name=[r"кошти суб.єктів господарювання та небанківських"],
         code=[r"liable$"], exclude=[r"вимог", r"іноземн", r"валют"]),
    dict(key="provisions", label="Резерви за активами (усього)",
         codes=["BS1_AssetsProvTotal"], name=[r"^активи\s*[-–—]\s*резерви, усього"], code=[r"assetsprovtotal$"],
         exclude=[]),
    dict(key="net_interest", label="Чистий процентний дохід",
         codes=["BS1_NetInterIncomeCosts"], name=[r"чистий процентний дохід"], code=[r"netinterincomecosts$"],
         exclude=[]),
    dict(key="net_comm", label="Чистий комісійний дохід",
         codes=["BS1_NetCommIncomeCosts", "BS1_NetCommlncomeCosts"], name=[r"чистий комісійний дохід"],
         code=[r"netcomm.ncomecosts$"], exclude=[]),
    dict(key="admin_costs", label="Адміністративні та операційні витрати",
         codes=["BS1_AdminOperCosts"], name=[r"адміністративні та інші операційні витрати"],
         code=[r"adminopercosts$"], exclude=[]),
    dict(key="fin_result", label="Прибуток після оподаткування",
         codes=["BS1_ProfitLossAfterTax"], name=[r"прибуток/\(?збиток\)? після оподаткування"],
         code=[r"profitlossaftertax$"], exclude=[]),
    dict(key="risk_assets", label="Активи під кредитним ризиком",
         codes=["BS1_SelIndAssets", "BS1_SellndAssets"],
         name=[r"активні банківські операції банку, за якими визначається розмір кредитного ризику$"],
         code=[r"se[li]indassets$"], exclude=[r"непрацю", r"величина"]),
    dict(key="npl_assets", label="Непрацюючі активи",
         codes=["BS1_SelIndAssetsNonperf", "BS1_SellndAssetsNonperf"], name=[r"непрацюючі активи"],
         code=[r"assetsnonperf$"], exclude=[]),
    dict(key="roa_official", label="Рентабельність активів, % (за НБУ)", type="pct",
         codes=["BS1_IndReturnAssets"], name=[r"рентабельність активів"], code=[r"returnassets$"], exclude=[]),

    # --- додатково: структура ресурсів, ліквідність, доходи
    dict(key="share_capital", label="Статутний капітал", group="Капітал",
         codes=["BS1_ShareCapital"], name=[r"^капітал\s*[-–—]\s*статутний"], code=[r"sharecapital$"], exclude=[]),
    dict(key="dep_retail_demand", label="Кошти фізосіб на вимогу", group="Ресурси",
         codes=["BS1_LiabIndivDemand"], name=[r"кошти клієнтів.*фізичних осіб на вимогу"],
         code=[r"liabindivdemand$"], exclude=[r"іноземн"]),
    dict(key="dep_corp_demand", label="Кошти юросіб на вимогу", group="Ресурси",
         codes=["BS1_LiabLEDemand"], name=[r"кошти клієнтів.*юридичних осіб на вимогу"],
         code=[r"liabledemand$"], exclude=[r"іноземн"]),
    dict(key="liab_banks", label="Кошти банків", group="Ресурси",
         codes=["BS1_LiabBanks"], name=[r"^зобов.язання\s*[-–—]\s*кошти банків$"], code=[r"liabbanks$"],
         exclude=[r"іноземн"]),
    dict(key="sub_debt", label="Субординований борг", group="Ресурси",
         codes=["BS1_LiabSubDebt"], name=[r"субординований борг"], code=[r"liabsubdebt$"], exclude=[]),
    dict(key="cash", label="Готівка та еквіваленти", group="Активи",
         codes=["BS1_AssetsCash"], name=[r"^активи\s*[-–—]\s*грошові кошти та їх еквіваленти$"],
         code=[r"assetscash$"], exclude=[r"в т\.ч", r"резерв"]),
    dict(key="due_banks", label="Кошти в інших банках", group="Активи",
         codes=["BS1_AssetsOtherBanks"], name=[r"кошти в інших банках, усього"], code=[r"assetsotherbanks$"],
         exclude=[r"резерв"]),
    dict(key="iglb", label="ОВДП, що рефінансуються НБУ", group="Активи",
         codes=["BS1_AssetsIGLBRefinNBU"], name=[r"^активи\s*[-–—]\s*овдп"], code=[r"iglbrefinnbu$"], exclude=[]),
    dict(key="interest_income", label="Процентні доходи", group="Доходи і витрати",
         codes=["BS1_InterestIncome"], name=[r"^доходи і витрати\s*[-–—]\s*процентні доходи$"],
         code=[r"interestincome$"], exclude=[]),
    dict(key="interest_costs", label="Процентні витрати", group="Доходи і витрати",
         codes=["BS1_InterestCosts"], name=[r"^доходи і витрати\s*[-–—]\s*процентні витрати$"],
         code=[r"interestcosts$"], exclude=[]),
    dict(key="comm_income", label="Комісійні доходи", group="Доходи і витрати",
         codes=["BS1_CommIncome"], name=[r"^доходи і витрати\s*[-–—]\s*комісійні доходи$"],
         code=[r"commincome$"], exclude=[]),
    dict(key="income_total", label="Всього доходів", group="Доходи і витрати",
         codes=["BS1_IncomeTotal"], name=[r"всього доходів"], code=[r"incometotal$"], exclude=[]),
    dict(key="expen_total", label="Всього витрат", group="Доходи і витрати",
         codes=["BS1_ExpenTotal"], name=[r"всього витрат"], code=[r"expentotal$"], exclude=[]),
    dict(key="alloc_prov", label="Відрахування до резервів", group="Доходи і витрати",
         codes=["BS1_AllocProv"], name=[r"відрахування до резервів"], code=[r"allocprov$"], exclude=[]),
    dict(key="payroll", label="Заробітна плата персоналу", group="Доходи і витрати",
         codes=["BS1_Payroll"], name=[r"заробітна плата персоналу"], code=[r"payroll$"], exclude=[r"нарахуванн"]),
    dict(key="trade_result", label="Торговий результат", group="Доходи і витрати",
         codes=["BS1_NetIncome"], name=[r"^доходи і витрати\s*[-–—]\s*торговий результат$"],
         code=[r"netincome$"], exclude=[]),
    dict(key="other_oper_income", label="Інші операційні доходи", group="Доходи і витрати",
         codes=["BS1_OtherOperIncome"], name=[r"інші операційні доходи"], code=[r"otheroperincome$"], exclude=[]),
    dict(key="tax", label="Витрати на податок на прибуток", group="Доходи і витрати",
         codes=["BS1_IncomeTaxExpen"], name=[r"витрати на податок на прибуток"], code=[r"incometaxexpen$"],
         exclude=[]),
]

# Похідні показники, які рахуються з уже завантажених (а не беруться з API).
# op="ratio" — num/den*factor; op="net" — base мінус модулі решти складових.
DERIVED = [
    dict(key="loans_net", label="Кредити клієнтам за мінусом резервів", group="Кредити", op="net",
         base="loans_total", minus=["prov_loans_le", "prov_loans_indiv"]),
    dict(key="npl_ratio", label="Частка непрацюючих активів, %", type="pct", lower_better=True,
         group="Коефіцієнти", op="ratio", num="npl_assets", den="risk_assets", factor=100.0),

    # Коефіцієнти. annualize=True приводить показник з початку року до річного виміру.
    dict(key="capital_ratio", label="Капітал / чисті активи, %", type="pct", group="Коефіцієнти",
         op="ratio", num="equity", den="assets", factor=100.0),
    dict(key="nim", label="Чиста процентна маржа (NIM), %", type="pct", group="Коефіцієнти",
         op="ratio", num="net_interest", den="assets", factor=100.0, ltm_num=True),
    dict(key="cir", label="Витрати / доходи (CIR), %", type="pct", lower_better=True, group="Коефіцієнти",
         op="ratio", num="admin_costs", den="oper_income", factor=100.0),
    dict(key="coverage", label="Покриття кредитів резервами, %", type="pct", group="Коефіцієнти",
         op="ratio", num="prov_loans_total", den="loans_total", factor=100.0),
    dict(key="ltd", label="Кредити / кошти клієнтів (LTD), %", type="pct", group="Коефіцієнти",
         op="ratio", num="loans_net", den="dep_total", factor=100.0),
    dict(key="demand_share", label="Частка коштів на вимогу, %", type="pct", group="Коефіцієнти",
         op="ratio", num="dep_demand_total", den="dep_total", factor=100.0),
    dict(key="liquid_share", label="Готівка, НБУ та банки / активи, %", type="pct", group="Коефіцієнти",
         op="ratio", num="liquid_total", den="assets", factor=100.0),

    # Спред: скільки заробляємо на активах і скільки платимо за ресурси
    dict(key="asset_yield", label="Дохідність активів, %", type="pct", group="Коефіцієнти",
         op="ratio", num="interest_income", den="assets", factor=100.0, ltm_num=True, abs_num=True,
         feature="spread"),
    dict(key="funding_cost", label="Вартість фондування, %", type="pct", lower_better=True,
         group="Коефіцієнти",
         op="ratio", num="interest_costs", den="liabilities", factor=100.0, ltm_num=True, abs_num=True,
         feature="spread"),
    dict(key="spread", label="Процентний спред, %", type="pct", group="Коефіцієнти",
         op="diff", a="asset_yield", b="funding_cost", feature="spread"),

    # Ковзні 12 місяців — прибирають сезонність показників «з початку року»
    dict(key="net_interest_ltm", label="Чистий процентний дохід, 12 міс", group="Доходи і витрати (12 міс)",
         op="ltm", src="net_interest", feature="ltm"),
    dict(key="net_comm_ltm", label="Чистий комісійний дохід, 12 міс", group="Доходи і витрати (12 міс)",
         op="ltm", src="net_comm", feature="ltm"),
    dict(key="admin_costs_ltm", label="Адміністративні витрати, 12 міс", group="Доходи і витрати (12 міс)",
         op="ltm", src="admin_costs", feature="ltm"),
    dict(key="fin_result_ltm", label="Прибуток, 12 міс", group="Доходи і витрати (12 міс)",
         op="ltm", src="fin_result", feature="ltm"),
]

# Проміжні суми, потрібні лише для коефіцієнтів (у перемикач не потрапляють)
INTERMEDIATE = [
    dict(key="prov_loans_total", parts=["prov_loans_le", "prov_loans_indiv"], absolute=True),
    dict(key="dep_demand_total", parts=["dep_retail_demand", "dep_corp_demand"]),
    dict(key="liquid_total", parts=["cash", "due_banks"]),
    dict(key="oper_income", parts=["net_interest", "net_comm", "trade_result"]),
]

GROUP_BY_KEY = {
    "assets": "Баланс", "assets_total": "Баланс", "liabilities": "Баланс", "provisions": "Баланс",
    "equity": "Капітал", "share_capital": "Капітал",
    "loans_total": "Кредити", "loans_net": "Кредити", "loans_corp": "Кредити", "loans_retail": "Кредити",
    "prov_loans_le": "Кредити", "prov_loans_indiv": "Кредити",
    "dep_total": "Ресурси", "dep_retail": "Ресурси", "dep_corp": "Ресурси",
    "net_interest": "Доходи і витрати", "net_comm": "Доходи і витрати",
    "admin_costs": "Доходи і витрати", "fin_result": "Доходи і витрати",
    "risk_assets": "Якість активів", "npl_assets": "Якість активів", "roa_official": "Коефіцієнти",
}

SYSTEM_MAP = {
    "n_banks": [r"^кількість діючих банків$", r"numberbanks$"],
    "assets": [r"^активи$", r"^bs3_assets$"],
    "equity": [r"^капітал$", r"^bs3_equity$"],
    "roa": [r"рентабельність активів", r"returnassets$"],
    "roe": [r"рентабельність капіталу", r"returnequity$"],
    "assets_fc": [r"^активи в іноземній валюті$", r"^bs3_assetsfc$"],
}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).lower().strip()
    s = s.replace("’", "'").replace("`", "'").replace("ʼ", "'").replace("ʼ", "'")
    return re.sub(r"\s+", " ", s)


def read_long(path, cols):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return [dict(zip(cols, row)) for row in list(csv.reader(f))[1:] if row]


def resolve_metric(spec, catalog):
    """catalog: {ind_code: ind_name}. Повертає (code, name) або (None, None)."""
    def ok(name, code):
        blob = norm(name) + " " + norm(code)
        return not any(re.search(x, blob) for x in spec.get("exclude", []))

    # 1) точний код із довідника НБУ
    lower = {norm(c): c for c in catalog}
    for c in spec.get("codes", []):
        hit = lower.get(norm(c))
        if hit:
            return (hit, catalog[hit])
    # 2) за назвою, 3) за шаблоном коду
    for pat in spec["name"]:
        hits = [(c, n) for c, n in catalog.items() if re.search(pat, norm(n)) and ok(n, c)]
        if hits:
            return sorted(hits, key=lambda x: len(norm(x[1])))[0]
    for pat in spec["code"]:
        hits = [(c, n) for c, n in catalog.items() if re.search(pat, norm(c)) and ok(n, c)]
        if hits:
            return sorted(hits, key=lambda x: len(norm(x[0])))[0]
    return (None, None)


def short_name(name):
    s = re.sub(r'^(ПУБЛІЧНЕ |ПРИВАТНЕ )?(АКЦІОНЕРНЕ ТОВАРИСТВО|АТ|ПАТ|ПРАТ|ТОВ)\b\.?\s*', "", name.strip(), flags=re.I)
    s = s.replace('"', "").replace("«", "").replace("»", "").strip()
    s = re.sub(r"^(КБ|АКБ|КОМЕРЦІЙНИЙ БАНК)\s+", "", s, flags=re.I).strip()
    return s.title() if s.isupper() else s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bank", default="оксі", help="підрядок назви банку у фокусі")
    ap.add_argument("--out", default=os.path.join(HERE, "dashboard.html"))
    ap.add_argument("--list-indicators", action="store_true")
    ap.add_argument("--logo", default=None,
                    help="файл логотипа (png/jpg/svg). Типово шукається logo.png / logo.svg поруч зі скриптом")
    ap.add_argument("--features", default="all",
                    help="блоки: waterfall,spread,movers,structure,ltm,compare,period,board,market,npl,fx,guar; "
                         "типово — all (усі)")
    args = ap.parse_args()
    feats = ([f.strip() for f in args.features.split(",") if f.strip()]
             if args.features not in ("all", "") else
             ["waterfall", "spread", "movers", "structure", "ltm", "compare", "period", "board", "market", "npl", "fx", "guar"])

    rows = read_long(os.path.join(DATA, "banks_long.csv"),
                     ["dt", "bank_id", "bank_name", "ind_code", "ind_name", "value", "dim"])
    if not rows:
        print("Немає data/banks_long.csv — спочатку запустіть nbu_fetch.py")
        sys.exit(1)

    catalog = {}
    for r in rows:
        catalog.setdefault(r["ind_code"], r["ind_name"])

    if args.list_indicators:
        for c, n in sorted(catalog.items()):
            print(f"{c:40s} {n}")
        return

    # --- мапимо показники
    override_path = os.path.join(HERE, "metrics_override.json")
    override = {}
    if os.path.exists(override_path):
        with open(override_path, encoding="utf-8") as f:
            override = json.load(f)

    metrics, code_by_key = [], {}
    for spec in METRICS:
        if spec["key"] in override:
            code = override[spec["key"]]
            name = catalog.get(code, "")
        else:
            code, name = resolve_metric(spec, catalog)
        if not code:
            print(f"  ! показник '{spec['label']}' не знайдено в даних API — пропущено")
            continue
        code_by_key[spec["key"]] = code
        metrics.append({"key": spec["key"], "label": spec["label"], "code": code, "name": name,
                        "type": spec.get("type", "money"),
                        "group": spec.get("group", GROUP_BY_KEY.get(spec["key"], "Інше"))})
    metric_type = {m["key"]: m["type"] for m in metrics}
    print("Мапінг показників:")
    for m in metrics:
        print(f"  {m['key']:14s} -> {m['code']:30s} {m['name']}")

    # --- осі
    dates = sorted({r["dt"] for r in rows})
    didx = {d: i for i, d in enumerate(dates)}

    banks, id_by_name = {}, {}
    for r in rows:
        bid = r["bank_id"] or r["bank_name"]
        if bid not in banks:
            banks[bid] = {"name": r["bank_name"], "short": short_name(r["bank_name"])}
        id_by_name.setdefault(norm(r["bank_name"]), bid)

    # У наборі НБУ кожен показник розбитий на кілька рядків за виміром r034 (вид валюти),
    # тому значення на одну дату/банк/показник ПІДСУМОВУЮТЬСЯ, а не перезаписуються.
    series = {m["key"]: {} for m in metrics}
    by_dim = {}                       # (key, bank) -> {dim: [значення по датах]}
    want = {m["code"]: m["key"] for m in metrics}
    merged = 0
    for r in rows:
        key = want.get(r["ind_code"])
        if not key:
            continue
        bid = r["bank_id"] or r["bank_name"]
        try:
            v, i = round(float(r["value"]), 3), didx[r["dt"]]
        except (ValueError, KeyError):
            continue
        arr = series[key].setdefault(bid, [None] * len(dates))
        if arr[i] is None:
            arr[i] = v
        else:
            arr[i] = round(arr[i] + v, 3)
            merged += 1
        d = (r.get("dim") or "").strip()
        if d:
            by_dim.setdefault((key, bid), {}).setdefault(d, [None] * len(dates))[i] = v
    if merged:
        print(f"  * підсумовано {merged:,} рядків, розбитих за виміром валюти (r034)")

    # --- банк у фокусі
    focus = None
    needle = norm(args.bank)
    for bid, b in banks.items():
        if needle in norm(b["name"]):
            focus = bid
            break
    if not focus:
        print(f"  ! банк '{args.bank}' не знайдено. Беру найбільший за активами.")
        last = len(dates) - 1
        focus = max(series.get("assets", {}), key=lambda b: series["assets"][b][last] or 0)
    print(f"Банк у фокусі: {banks[focus]['name']}")

    # --- агрегати системи
    srows = read_long(os.path.join(DATA, "system_long.csv"), ["dt", "ind_code", "ind_name", "value"])
    system = {}
    for key, pats in SYSTEM_MAP.items():
        arr = [None] * len(dates)
        for r in srows:
            blob_n, blob_c = norm(r["ind_name"]), norm(r["ind_code"])
            if any(re.search(p, blob_n) or re.search(p, blob_c) for p in pats):
                if r["dt"] in didx:
                    try:
                        arr[didx[r["dt"]]] = round(float(r["value"]), 3)
                    except ValueError:
                        pass
        if any(v is not None for v in arr):
            system[key] = arr

    # --- одиниці виміру: звіряємо суму активів банків з агрегатом НБУ (той — у млн грн).
    # Беремо медіану співвідношення по всіх датах і масштабуємо, лише якщо це явно 1000x.
    scale, ratios = 1.0, []
    if "assets" in series and system.get("assets"):
        for i, _ in enumerate(dates):
            sys_a = system["assets"][i]
            sum_a = sum(v[i] for v in series["assets"].values() if v[i] is not None)
            if sys_a and sum_a:
                ratios.append(sum_a / sys_a)
    if not ratios:
        print("  ! звірку з агрегатом НБУ зробити не вдалося (у basindbank не знайшлися активи системи) — "
              "перевірте одиниці виміру вручну")
    if ratios:
        ratios.sort()
        r = ratios[len(ratios) // 2]
        print(f"  * звірка одиниць: сума активів усіх банків / активи системи за НБУ = {r:.3f}")
        if 300 < r < 3000:
            scale = 1 / 1000.0
            print(f"  * дані по банках у тис. грн (співвідношення до агрегату НБУ {r:.0f}x) — перевів у млн грн")
        elif 0.0003 < r < 0.003:
            scale = 1000.0
            print(f"  * дані по банках у млрд грн — перевів у млн грн")
        elif not (0.5 < r < 2.0):
            print(f"  ! сума активів банків відрізняється від агрегату НБУ у {r:.2f}x — "
                  f"масштаб не змінював, перевірте мапінг показника «Активи»")
    if scale != 1.0:
        for k in series:
            if metric_type.get(k) == "pct":
                continue
            for b in series[k]:
                series[k][b] = [None if v is None else round(v * scale, 3) for v in series[k][b]]

    # Примітка: у CSV зберігається колонка dim (r034) з розбивкою за видом валюти —
    # її можна використати для аналізу валютної структури, коли буде звірено значення кодів.

    # --- самоперевірка: чисті активи мають дорівнювати зобов'язанням плюс капітал
    if all(k in series and focus in series[k] for k in ("assets", "liabilities", "equity")):
        for i in range(len(dates) - 1, -1, -1):
            a, l, e = (series["assets"][focus][i], series["liabilities"][focus][i],
                       series["equity"][focus][i])
            if a and l is not None and e is not None:
                diff = a - (l + e)
                ok = "ок" if abs(diff) < max(0.5, abs(a) * 0.001) else f"РОЗБІЖНІСТЬ {diff:+.2f}"
                print(f"  * перевірка балансу на {dates[i]}: чисті активи {a:,.2f} = "
                      f"зобов'язання {l:,.2f} + капітал {e:,.2f} — {ok}")
                break

    # --- проміжні суми для коефіцієнтів
    for spec in INTERMEDIATE:
        parts = [series[k] for k in spec["parts"] if k in series]
        if not parts:
            continue
        out = {}
        for b in {b for p in parts for b in p}:
            arr = []
            for i in range(len(dates)):
                vals = [p[b][i] for p in parts if b in p and p[b][i] is not None]
                arr.append(round(sum(abs(v) if spec.get("absolute") else v for v in vals), 3) if vals else None)
            if any(v is not None for v in arr):
                out[b] = arr
        if out:
            series[spec["key"]] = out

    def annualized(arr):
        """Показник з початку року -> річний вимір (на 01.06 минуло 5 місяців)."""
        out = []
        for i, d in enumerate(dates):
            m = int(d[5:7])
            months = 12 if m == 1 else m - 1
            out.append(None if arr[i] is None else arr[i] * 12.0 / months)
        return out

    def ltm(arr):
        """Ковзні 12 місяців із показника, що подається наростаючим підсумком з початку року."""
        inc = []
        for i, d in enumerate(dates):
            v = arr[i]
            if v is None:
                inc.append(None)
            elif int(d[5:7]) == 2:        # дані на 01.02 — це вже сам січень, лічильник обнулився
                inc.append(v)
            else:
                p = arr[i - 1] if i > 0 else None
                inc.append(None if p is None else v - p)
        out = []
        for i in range(len(dates)):
            w = inc[i - 11:i + 1] if i >= 11 else []
            out.append(round(sum(w), 3) if len(w) == 12 and all(x is not None for x in w) else None)
        return out

    # --- похідні показники (рахуються з уже наявних рядів)
    for spec in DERIVED:
        if spec.get("feature") and spec["feature"] not in feats:
            continue
        out, srcs = {}, ""
        if spec.get("op") == "ltm":
            src = series.get(spec["src"])
            if not src:
                continue
            srcs = f"{spec['src']} (ковзні 12 міс)"
            for b, arr in src.items():
                v = ltm(arr)
                if any(x is not None for x in v):
                    out[b] = v
        elif spec.get("op") == "diff":
            a, bb = series.get(spec["a"]), series.get(spec["b"])
            if not a or not bb:
                continue
            srcs = f"{spec['a']} − {spec['b']}"
            for b in a:
                if b not in bb:
                    continue
                arr = [round(a[b][i] - bb[b][i], 3) if (a[b][i] is not None and bb[b][i] is not None)
                       else None for i in range(len(dates))]
                if any(x is not None for x in arr):
                    out[b] = arr
        elif spec.get("op", "ratio") == "ratio":
            num, den = series.get(spec["num"]), series.get(spec["den"])
            if not num or not den:
                continue
            srcs = (f"{spec['num']} / {spec['den']}"
                    + (" (ковзні 12 міс)" if spec.get("ltm_num")
                       else " (річний вимір)" if spec.get("annualize") else ""))
            for b in num:
                if b not in den:
                    continue
                nb = [None if v is None else abs(v) for v in num[b]] if spec.get("abs_num") else num[b]
                # ковзні 12 місяців краще за річний перерахунок: не дає сплесків у січні–лютому
                nb = ltm(nb) if spec.get("ltm_num") else (annualized(nb) if spec.get("annualize") else nb)
                arr = [round(spec["factor"] * nb[i] / den[b][i], 3)
                       if (nb[i] is not None and den[b][i]) else None for i in range(len(dates))]
                if any(v is not None for v in arr):
                    out[b] = arr
        else:                                     # op == "net"
            base = series.get(spec["base"])
            parts = [series[k] for k in spec["minus"] if k in series]
            if not base or not parts:
                continue
            srcs = f"{spec['base']} − " + " − ".join(spec["minus"])
            for b in base:
                arr = []
                for i in range(len(dates)):
                    v = base[b][i]
                    if v is None:
                        arr.append(None)
                        continue
                    # резерви можуть приходити як з мінусом, так і без — беремо модуль
                    for p in parts:
                        pv = p.get(b, [None] * len(dates))[i]
                        if pv is not None:
                            v -= abs(pv)
                    arr.append(round(v, 3))
                if any(v is not None for v in arr):
                    out[b] = arr
        if out:
            series[spec["key"]] = out
            metrics.append({"key": spec["key"], "label": spec["label"], "code": "derived",
                            "name": srcs, "type": spec.get("type", "money"),
                            "group": spec.get("group", "Коефіцієнти"),
                            "lowerBetter": bool(spec.get("lower_better"))})
            print(f"  {spec['key']:14s} -> розраховано: {srcs}")

    # службові показники (резерви під кредити, проміжні суми) у перемикач не потрапляють
    hidden = {s["key"] for s in METRICS if s.get("hidden")} | {s["key"] for s in INTERMEDIATE}
    metrics = [m for m in metrics if m["key"] not in hidden]
    for k in hidden:
        series.pop(k, None)
    # порядок груп у перемикачі
    order = {g: i for i, g in enumerate(["Баланс", "Капітал", "Ресурси", "Кредити", "Активи",
                                         "Доходи і витрати", "Якість активів", "Коефіцієнти", "Інше"])}
    pos = {id(m): i for i, m in enumerate(metrics)}
    metrics.sort(key=lambda m: (order.get(m.get("group", "Інше"), 99), pos[id(m)]))

    # ---------------------------------------------------------------- нові джерела НБУ
    # 1) Непрацюючі кредити по кожному банку (набір klk).
    #    Значення розбиті за валютою і ще одним виміром, тому підсумовуємо все;
    #    частка NPL від цього не страждає — чисельник і знаменник агрегуються однаково.
    klk_rows = read_long(os.path.join(DATA, "klk_long.csv"),
                         ["dt", "bank_id", "ind_code", "dim_curr", "dim_s080", "value"])
    if klk_rows:
        # Чотири різні речі в одному наборі:
        #   Klk_UO, Klk_FO …           — сам кредитний портфель
        #   Klk_UO_NPL …               — непрацюючі кредити (НБУ публікує лише з 04.2026)
        #   Klk_Ryz_UO, Klk_Ryz_FO …   — кредитний ризик (очікувані втрати), є з 2021 року
        #   Klk_Ryz_*_NPL              — кредитний ризик саме за непрацюючими
        loans, npl, risk = {}, {}, {}
        for r in klk_rows:
            if r["dt"] not in didx:
                continue
            i, b, code = didx[r["dt"]], r["bank_id"], r["ind_code"]
            try:
                v = float(r["value"])
            except ValueError:
                continue
            if code.startswith("Klk_Ryz_"):
                if code.endswith("_NPL"):
                    continue              # ризик за непрацюючими — окрема річ, не рахуємо
                target = risk
            else:
                target = npl if code.endswith("_NPL") else loans
            arr = target.setdefault(b, [None] * len(dates))
            arr[i] = round((arr[i] or 0) + v, 3)

        def ratio(num):
            """Відношення до кредитного портфеля, у відсотках."""
            res = {}
            for b, la in loans.items():
                na = num.get(b)
                if not na:
                    continue
                arr = [round(100 * na[i] / la[i], 3) if (la[i] and na[i] is not None) else None
                       for i in range(len(dates))]
                if any(v is not None for v in arr):
                    res[b] = arr
            return res

        out, out_risk = ratio(npl), ratio(risk)
        if out:
            series["npl_share"] = out
            metrics.append({"key": "npl_share", "label": "Частка непрацюючих кредитів, %",
                            "code": "klk", "name": "Klk_*_NPL / Klk_*", "type": "pct",
                            "group": "Якість активів", "lowerBetter": True})
            first = next((d for j, d in enumerate(dates)
                          if any(a[j] is not None for a in out.values())), None)
            print(f"  npl_share      -> розраховано з набору klk ({len(out)} банків, "
                  f"НБУ публікує з {first})")
        if out_risk:
            series["risk_share"] = out_risk
            metrics.append({"key": "risk_share", "label": "Кредитний ризик до портфеля, %",
                            "code": "klk", "name": "Klk_Ryz_* / Klk_*", "type": "pct",
                            "group": "Якість активів", "lowerBetter": True})
            print(f"  risk_share     -> розраховано з набору klk ({len(out_risk)} банків, "
                  f"з {dates[0]})")
        if out or out_risk:
            series["loans_klk"] = loans
            metrics.append({"key": "loans_klk", "label": "Кредитний портфель (за klk)",
                            "code": "klk", "name": "сума Klk_*", "type": "money",
                            "group": "Якість активів"})

    # 1b) Позабаланс: портфель наданих гарантій (рахунок 9000, залишок).
    #     Це та сама методика, за якою рейтинг гарантій рахують НБУ та АУБ:
    #     залишок позабалансового рахунку 9000 «Надані гарантії». Авалі (9003) сюди не входять.
    GUAR_ACCOUNTS = {"9000": "Надані гарантії"}
    osb_rows = read_long(os.path.join(DATA, "osb_long.csv"),
                         ["dt", "bank_id", "acc", "t025", "dim_curr", "value"])
    if osb_rows:
        # НБУ у наборі osb віддає той самий залишок кілька разів — один і той самий
        # рядок (дата, банк, рахунок, t025, вид валюти, сума) повторюється два рази.
        # Якщо це не прибрати, портфель гарантій подвоюється. Тому спершу лишаємо
        # рівно один примірник кожного унікального рядка, і тільки потім
        # підсумовуємо гривню з валютою.
        seen, uniq, dups = set(), [], 0
        for r in osb_rows:
            if r["acc"] not in GUAR_ACCOUNTS or r["t025"] != "1" or r["dt"] not in didx:
                continue
            key = (r["dt"], r["bank_id"], r["acc"], r["t025"], r["dim_curr"], r["value"])
            if key in seen:
                dups += 1
                continue
            seen.add(key)
            uniq.append(r)
        guar = {}
        for r in uniq:
            try:
                v = float(r["value"])
            except ValueError:
                continue
            arr = guar.setdefault(r["bank_id"], [None] * len(dates))
            i = didx[r["dt"]]
            arr[i] = round((arr[i] or 0) + v, 3)     # підсумовуємо гривню й валюту
        if dups:
            print(f"  guarantees     -> прибрано {dups} дубльованих рядків osb "
                  f"(лишилось {len(uniq)})")
        if guar:
            last_i = len(dates) - 1
            tot = sum(a[last_i] for a in guar.values() if a[last_i])
            cnt = sum(1 for a in guar.values() if a[last_i])
            print(f"  guarantees     -> контроль на {dates[last_i]}: {cnt} банків, "
                  f"разом {tot:,.1f} млн грн")
            series["guarantees"] = guar
            metrics.append({"key": "guarantees", "label": "Надані гарантії",
                            "code": "osb 9000", "name": "залишок рахунку 9000",
                            "type": "money", "group": "Позабаланс"})
            print(f"  guarantees     -> рахунок 9000 з osb ({len(guar)} банків)")

    # 2) Ставки й курс: облікова ставка, ринкові ставки, курс долара
    rates_rows = read_long(os.path.join(DATA, "rates_long.csv"), ["dt", "ind_code", "ind_name", "value"])
    market_rows = read_long(os.path.join(DATA, "market_rates.csv"),
                            ["dt", "ind_code", "ind_name", "curr", "value"])
    fx_rows = read_long(os.path.join(DATA, "fx_long.csv"), ["dt", "cc", "rate"])
    context = {}

    def put(key, rows, code_field, code, val_field, extra=None):
        arr = [None] * len(dates)
        for r in rows:
            if r["dt"] in didx and r[code_field] == code and (extra is None or extra(r)):
                try:
                    arr[didx[r["dt"]]] = round(float(r[val_field]), 4)
                except ValueError:
                    pass
        if any(v is not None for v in arr):
            context[key] = arr

    put("policy_rate", rates_rows, "ind_code", "KEY_PolicyRate", "value")
    put("usd", fx_rows, "cc", "USD", "rate")
    put("usd_avg", rates_rows, "ind_code", "AvgKursPerM", "value")
    for key, code in [("mkt_dep_house", "New_Deposits_Households"),
                      ("mkt_dep_corp", "New_Deposits_NFinCorp"),
                      ("mkt_loan_house", "New_Loans_Households"),
                      ("mkt_loan_corp", "New_Loans_NFinCorp")]:
        put(key, market_rows, "ind_code", code, "value", extra=lambda r: r["curr"] in ("01", "total"))
    if context:
        print(f"  контекст ринку  -> {', '.join(sorted(context))}")

    # 3) Валютна структура і приріст, очищений від курсової переоцінки.
    #    Перевірено на даних НБУ: r034=1 — національна валюта, r034=2 — іноземна.
    fx_assets = {}
    for (k, b), dd in by_dim.items():
        if k == "assets" and "2" in dd:
            fx_assets[b] = [None if v is None else round(v * scale, 3) for v in dd["2"]]
    if fx_assets and "assets" in series:
        share = {}
        for b, arr in fx_assets.items():
            tot = series["assets"].get(b)
            if not tot:
                continue
            s = [round(100 * arr[i] / tot[i], 3) if (arr[i] is not None and tot[i]) else None
                 for i in range(len(dates))]
            if any(v is not None for v in s):
                share[b] = s
        if share:
            series["fx_share"] = share
            metrics.append({"key": "fx_share", "label": "Частка активів в іноземній валюті, %",
                            "code": "r034", "name": "активи r034=2 / усі активи", "type": "pct",
                            "group": "Коефіцієнти"})
            print(f"  fx_share       -> розраховано ({len(share)} банків)")
        # приріст у постійному курсі: валютну частину перераховуємо за курсом базового місяця
        if context.get("usd"):
            adj = {}
            usd = context["usd"]
            for b, tot in series["assets"].items():
                fxa = fx_assets.get(b)
                if not fxa:
                    continue
                arr = []
                for i in range(len(dates)):
                    if tot[i] is None or usd[i] is None:
                        arr.append(None)
                        continue
                    f = fxa[i] or 0
                    base = next((usd[j] for j in range(len(dates)) if usd[j]), usd[i])
                    arr.append(round((tot[i] - f) + f * base / usd[i], 3))
                if any(v is not None for v in arr):
                    adj[b] = arr
            if adj:
                series["assets_fxadj"] = adj
                metrics.append({"key": "assets_fxadj", "label": "Чисті активи у постійному курсі",
                                "code": "derived", "name": "валютна частина за курсом базового місяця",
                                "type": "money", "group": "Баланс"})
                print(f"  assets_fxadj   -> розраховано ({len(adj)} банків)")

    # --- групи банків за класифікацією НБУ.
    # У даних група позначена літерним кодом; розшифровку визначаємо за складом:
    # де опинилися Приватбанк/Ощадбанк — та група державна, де Райффайзен/ОТП — іноземна.
    STATE_RE = re.compile(r"приватбанк|ощадбанк|укрексімбанк|укргазбанк|сенс банк", re.I)
    FOREIGN_RE = re.compile(r"райффайзен|укрсиббанк|отп |креді агріколь|прокредит|сітібанк|інг банк|"
                            r"правекс|кредобанк|піреус|бнп|дойче|себ", re.I)
    grp_rows = read_long(os.path.join(DATA, "bank_groups.csv"),
                         ["bank_id", "bank_name", "group_code", "last_date"])
    last_date = dates[-1] if dates else ""
    if grp_rows:
        score = {}
        for r in grp_rows:
            c = r["group_code"] or "?"
            s = score.setdefault(c, [0, 0, 0])
            s[0] += bool(STATE_RE.search(r["bank_name"]))
            s[1] += bool(FOREIGN_RE.search(r["bank_name"]))
            s[2] += 1
        labels = {}
        st = max(score, key=lambda c: score[c][0], default=None)
        fr = max((c for c in score if c != st), key=lambda c: score[c][1], default=None)
        for c in score:
            if c == st and score[c][0] >= 2:
                labels[c] = "Банки з державною часткою"
            elif c == fr and score[c][1] >= 2:
                labels[c] = "Банки іноземних банківських груп"
            else:
                labels[c] = "Банки з приватним капіталом"
        for r in grp_rows:
            bid = r["bank_id"]
            if bid not in banks:
                continue
            banks[bid]["group"] = ("Не подають звітність (виведені з ринку)"
                                   if r["last_date"] < last_date else labels.get(r["group_code"] or "?", "Інші"))
        by_label = {}
        for b in banks.values():
            by_label[b.get("group", "Інші")] = by_label.get(b.get("group", "Інші"), 0) + 1
        print("  * групи банків: " + ", ".join(f"{k} — {v}" for k, v in sorted(by_label.items())))

    # --- логотип банку: вшиваємо у HTML, щоб файл лишався самодостатнім
    logo_uri = None
    logo_path = args.logo if (args.logo and not args.logo.startswith("http")) else None
    if not logo_path:
        for cand in ("logo.svg", "logo.png", "logo.jpg", "logo.jpeg", "logo.webp"):
            if os.path.exists(os.path.join(HERE, cand)):
                logo_path = os.path.join(HERE, cand)
                break
    if not logo_path:
        # разове завантаження логотипа з сайту банку; далі береться локальна копія
        url = args.logo if (args.logo and args.logo.startswith("http")) else LOGO_URL
        ext = os.path.splitext(url.split("?")[0])[1].lower() or ".svg"
        dest = os.path.join(HERE, "logo" + ext)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; nbu-dashboard/1.0)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                blob = r.read()
            if blob:
                with open(dest, "wb") as f:
                    f.write(blob)
                logo_path = dest
                print(f"  * логотип завантажено з {url} → {os.path.basename(dest)}")
        except Exception as e:
            print(f"  * логотип не завантажився ({e}); покладіть logo.png поруч зі скриптами")
    if logo_path and os.path.exists(logo_path):
        mime = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(os.path.splitext(logo_path)[1].lower())
        if mime:
            with open(logo_path, "rb") as f:
                logo_uri = f"data:{mime};base64," + base64.b64encode(f.read()).decode()
            print(f"  * логотип: {os.path.basename(logo_path)} ({os.path.getsize(logo_path)/1024:.0f} КБ)")
    elif not logo_path:
        print("  * логотип не знайдено — покладіть logo.png (або logo.svg) поруч зі скриптами")

    meta_path = os.path.join(DATA, "_meta.json")
    meta = json.load(open(meta_path, encoding="utf-8")) if os.path.exists(meta_path) else {}
    payload = {
        "meta": {"generated": datetime.now().isoformat(timespec="minutes"),
                 "mock": bool(meta.get("mock")), "source": "bank.gov.ua OpenData",
                 "features": feats, "logo": logo_uri},
        "dates": dates,
        "metrics": metrics,
        "banks": banks,
        "focus": focus,
        "series": series,
        "system": system,
        "context": context,
    }

    with open(os.path.join(HERE, "dashboard_template.html"), encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/*__DATA__*/", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    size = os.path.getsize(args.out) / 1024
    print(f"Готово: {os.path.relpath(args.out, HERE)} ({size:,.0f} КБ), "
          f"{len(banks)} банків, {len(dates)} звітних дат, {len(metrics)} показників")


if __name__ == "__main__":
    main()
