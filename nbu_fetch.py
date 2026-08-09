#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nbu_fetch.py — завантаження відкритих даних НБУ для дашборду банківської системи.

Джерела (OpenData НБУ REST API, безкоштовно, без ключа):
  1) banksfinrep — дані фінансової звітності ПО КОЖНОМУ БАНКУ (з 01.02.2018)
     https://bank.gov.ua/NBUStatService/v1/statdirectory/banksfinrep?period=m&date=YYYYMMDD&json
  2) basindbank  — основні показники діяльності банків України, АГРЕГАТ по системі (з 01.01.2016)
     https://bank.gov.ua/NBUStatService/v1/statdirectory/basindbank?period=m&date=YYYYMMDD&json

Скрипт:
  * тягне дані помісячно (кожна звітна дата = 1-ше число місяця),
  * кешує сирий JSON у raw/ (повторний запуск не перекачує вже наявне),
  * автоматично розпізнає назви полів у відповіді API (схема НБУ подекуди змінюється),
  * складає нормалізовані CSV у data/.

Запуск:
    python3 nbu_fetch.py                      # з 2021-01-01 по поточну дату
    python3 nbu_fetch.py --start 20180201     # уся доступна історія
    python3 nbu_fetch.py --refresh-last 3     # перекачати останні 3 звітні дати
    python3 nbu_fetch.py --mock               # згенерувати ДЕМО-дані без мережі
"""

import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://bank.gov.ua/NBUStatService/v1/statdirectory"
UA = "Mozilla/5.0 (compatible; nbu-dashboard/1.0; +local analytics script)"

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
DATA = os.path.join(HERE, "data")

# ----------------------------------------------------------------------------- utils


def log(msg):
    print(msg, flush=True)


def month_starts(start: str, end: str):
    """Перелік звітних дат (1-ше число кожного місяця) у форматі YYYYMMDD."""
    y, m = int(start[:4]), int(start[4:6])
    ey, em = int(end[:4]), int(end[4:6])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}{m:02d}01")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def http_get_json(url, retries=4, timeout=60):
    """Повертає [] якщо даних немає, None — якщо була справжня помилка зв'язку.

    Важливо: 400 і 404 від НБУ означають «на цю дату набору немає» — це остаточна
    відповідь, повторювати запит немає сенсу. Раніше скрипт робив 4 спроби з паузами
    на кожну таку дату, і завантаження перетворювалось на 15 хвилин очікування.
    """
    ctx = ssl.create_default_context()
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                raw = r.read().decode("utf-8", errors="replace")
            raw = raw.strip()
            if not raw or raw[0] not in "[{":
                return []
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return []                      # даних на цю дату немає — йдемо далі
            last = e
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    log(f"    ! не вдалося завантажити {url}: {last}")
    return None


def fetch_dataset(apikod, date, period="m", force=False):
    """Завантажує один датасет на одну звітну дату (з кешуванням у raw/)."""
    path = os.path.join(RAW, f"{apikod}_{date}.json")
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as f:
            try:
                cached = json.load(f)
            except json.JSONDecodeError:
                cached = None
        # Порожній кеш не довіряємо: він міг лишитись від обірваного завантаження.
        # Перепитати дешево — якщо даних справді немає, НБУ відповідає 400 миттєво.
        if cached:
            return cached
    url = (f"{BASE}/{apikod}?period={period}&date={date}&json" if period
           else f"{BASE}/{apikod}?date={date}&json")
    recs = http_get_json(url)
    if recs is None:
        return None
    with open(path, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False)
    time.sleep(0.35)  # ввічлива пауза до API НБУ
    return recs


# ----------------------------------------------------- автоматичне розпізнавання схеми

# НБУ подекуди віддає дату як 01.02.2018, 2018-02-01, 20180201 або 2018-02-01T00:00:00
DATE_RE = re.compile(r"^(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}([T ].*)?|\d{8})$")
# «Сильна» ознака назви банку: організаційна форма або лапки (АТ "ОКСІ БАНК").
# «Слабка» — просто слово «банк», яке трапляється і в назвах показників
# («Кількість діючих банків»), тому саме по собі ознакою не є.
BANK_STRONG_RE = re.compile(r"(^|\W)(АТ|ПАТ|ПРАТ|ТОВ|АБ|JSC|PJSC)(\W|$)|АКЦІОНЕРН|[\"«»]", re.IGNORECASE)
BANKISH_RE = re.compile(r"банк|bank", re.IGNORECASE)


def _is_num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return True
    if isinstance(v, str):
        try:
            float(v.replace(",", ".").replace(" ", ""))
            return True
        except ValueError:
            return False
    return False


def detect_schema(records, per_bank=True):
    """Визначає, які ключі відповідають даті / банку / показнику / значенню."""
    keys = sorted({k for r in records[:2000] for k in r.keys()})
    stats = {}
    for k in keys:
        vals = [r.get(k) for r in records[:4000] if r.get(k) not in (None, "")]
        if not vals:
            continue
        distinct = {str(v) for v in vals}
        stats[k] = {
            "n_distinct": len(distinct),
            "num_share": sum(_is_num(v) for v in vals) / len(vals),
            "date_share": sum(bool(DATE_RE.match(str(v))) for v in vals) / len(vals),
            "bankish": sum(bool(BANKISH_RE.search(str(v))) for v in vals) / len(vals),
            "strong": sum(bool(BANK_STRONG_RE.search(str(v))) for v in vals) / len(vals),
            "avg_len": sum(len(str(v)) for v in vals) / len(vals),
            "sample": list(distinct)[:3],
        }

    def pick(pred, prefer=None, exclude=()):
        cands = [k for k, s in stats.items() if k not in exclude and pred(k, s)]
        if not cands:
            return None
        if prefer:
            for p in prefer:
                if p in cands:
                    return p
        return sorted(cands, key=lambda k: -stats[k]["n_distinct"])[0]

    schema = {}
    schema["date"] = pick(lambda k, s: s["date_share"] > 0.9, prefer=["dt", "date"])
    idish = re.compile(r"^(id|kod|code|nkb|mfo|edrpou|glms|leveli|freq|period|year|month)", re.I)
    schema["value"] = pick(lambda k, s: s["num_share"] > 0.9 and not idish.match(k),
                           prefer=["value", "val"], exclude={schema["date"]})
    used = {schema["date"], schema["value"]}

    schema["ind_code"] = pick(lambda k, s: s["num_share"] < 0.6 and 3 <= s["n_distinct"] <= 400,
                              prefer=["id_api", "indicator", "code"], exclude=used)
    used.add(schema["ind_code"])
    schema["ind_name"] = pick(lambda k, s: s["strong"] < 0.3 and s["avg_len"] > 8 and 3 <= s["n_distinct"] <= 400,
                              prefer=["txt", "name", "indicator_name"], exclude=used)
    used.add(schema["ind_name"])

    # Вимір, за яким показник розбитий на кілька рядків (r034 — ознака виду валюти).
    # Такі рядки треба підсумовувати, інакше показник виходить неповним.
    for cand in ("r034", "r030", "curr", "ccy", "valuta"):
        if cand in stats and stats[cand]["n_distinct"] <= 6:
            schema["dim"] = cand
            break
    else:
        schema["dim"] = None

    # Група банку за класифікацією НБУ (державні / іноземні / з приватним капіталом)
    schema["group"] = next((c for c in ("gr_bank", "group", "grp") if c in stats), None)

    if per_bank:
        schema["bank_name"] = pick(lambda k, s: s["strong"] > 0.5 or s["bankish"] > 0.85,
                                   prefer=["fullname", "bank", "bank_name", "name_bank", "nameb"], exclude=used)
        used.add(schema["bank_name"])
        bn = schema.get("bank_name")
        n_banks = stats[bn]["n_distinct"] if bn and bn in stats else None
        def id_pred(k, s):
            if n_banks:
                return abs(s["n_distinct"] - n_banks) <= max(3, n_banks * 0.15) and s["avg_len"] <= 14
            return 10 <= s["n_distinct"] <= 300 and s["avg_len"] <= 14
        schema["bank_id"] = pick(id_pred, prefer=["nkb", "id_bank", "bank_id", "edrpou", "kod_bank", "mfo", "glms"],
                                 exclude=used)
    return schema, stats


def schema_report(name, schema, stats):
    lines = [f"=== {name} ===", "Розпізнана схема:"]
    for role, key in schema.items():
        lines.append(f"  {role:10s} -> {key}")
    lines.append("Усі поля у відповіді API:")
    for k, s in sorted(stats.items()):
        lines.append(f"  {k:22s} distinct={s['n_distinct']:5d} num={s['num_share']:.2f} "
                     f"bankish={s['bankish']:.2f} sample={s['sample']}")
    return "\n".join(lines) + "\n\n"


def norm_num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace(",", ".").replace("\xa0", "").replace(" ", ""))
    except ValueError:
        return None


def to_iso(d):
    """Будь-який формат дати НБУ -> '2018-02-01'"""
    s = str(d).strip()
    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", s):
        dd, mm, yy = s.split(".")
        return f"{yy}-{mm}-{dd}"
    if re.match(r"^\d{8}$", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    return s


# ----------------------------------------------------------------------------- ДЕМО-дані

DEMO_BANKS = [
    ("305299", "АТ КБ \"ПРИВАТБАНК\"", 720000), ("300465", "АТ \"ОЩАДБАНК\"", 400000),
    ("322313", "АТ \"УКРЕКСІМБАНК\"", 280000), ("320478", "АТ \"РАЙФФАЙЗЕН БАНК\"", 200000),
    ("380269", "АТ \"УНІВЕРСАЛ БАНК\"", 145000), ("300335", "АТ \"УКРСИББАНК\"", 140000),
    ("300614", "АТ \"ПУМБ\"", 130000), ("300346", "АТ \"АЛЬФА-БАНК\"", 95000),
    ("300023", "АТ \"ОТП БАНК\"", 90000), ("339500", "АТ \"КРЕДІ АГРІКОЛЬ БАНК\"", 80000),
    ("300528", "АТ \"СЕНС БАНК\"", 78000), ("320984", "АТ \"КРЕДОБАНК\"", 55000),
    ("328845", "АТ \"А-БАНК\"", 40000), ("300711", "АТ \"ТАСКОМБАНК\"", 38000),
    ("380805", "АТ \"ПРАВЕКС БАНК\"", 22000), ("300658", "АТ \"ПІРЕУС БАНК МКБ\"", 12000),
    ("325365", "АТ \"БАНК КРЕДИТ ДНІПРО\"", 30000), ("380775", "АТ \"КОМІНВЕСТБАНК\"", 9000),
    ("325990", "АТ \"МЕГАБАНК\"", 8000), ("380934", "АТ \"МТБ БАНК\"", 15000),
    ("300506", "АТ \"БАНК ВОСТОК\"", 26000), ("380862", "АТ \"АГРОПРОСПЕРІС БАНК\"", 11000),
    ("300120", "АТ \"БАНК АЛЬЯНС\"", 14000), ("380995", "АТ \"АЙБОКС БАНК\"", 4000),
    ("325248", "АТ \"АСВІО БАНК\"", 3500), ("380538", "АТ \"БАНК ФОРВАРД\"", 3200),
    ("380637", "АТ \"КРИСТАЛБАНК\"", 3000), ("325990b", "АТ \"ПОЛІКОМБАНК\"", 2200),
    ("325213", "АТ \"ОКСІ БАНК\"", 2600), ("325990c", "АТ \"БАНК \"ПОРТАЛ\"", 1800),
    ("380720", "АТ \"СКАЙ БАНК\"", 5200), ("300731", "АТ \"ЮНЕКС БАНК\"", 4800),
    ("380418", "АТ \"РВС БАНК\"", 6100), ("300614b", "АТ \"БТА БАНК\"", 2100),
    ("322302", "АТ \"УКРГАЗБАНК\"", 180000), ("380266", "АТ \"АККОРДБАНК\"", 4500),
    ("351005", "АТ \"ПІВДЕННИЙ\"", 45000), ("300614c", "АТ \"ГЛОБУС\"", 9500),
    ("380096", "АТ \"КБ \"ЗЕМЕЛЬНИЙ КАПІТАЛ\"", 1500), ("325895", "АТ \"ІНДУСТРІАЛБАНК\"", 7200),
    ("380281", "АТ \"ПРОКРЕДИТ БАНК\"", 42000), ("380749", "АТ \"КЛІРИНГОВИЙ ДІМ\"", 5600),
    ("300465b", "АТ \"ІНГ БАНК УКРАЇНА\"", 34000), ("300213", "АТ \"СІТІБАНК\"", 48000),
    ("380771", "АТ \"ДІВІ БАНК\"", 2900), ("325575", "АТ \"АГРІКОЛЬ\"", 2400),
    ("380805b", "АТ \"КОМЕРЦІЙНИЙ ІНДУСТРІАЛЬНИЙ БАНК\"", 3800),
    ("380637b", "АТ \"ТРАСТ КАПІТАЛ\"", 1200), ("300175", "АТ \"МІБ\"", 2000),
    ("325898", "АТ \"ЄПБ\"", 1700), ("320999", "АТ \"ЛЬВІВ\"", 6800),
]

# Коди й назви як у справжньому наборі banksfinrep (довідник BS1), значення — у тис. грн
DEMO_INDICATORS = [
    ("BS1_AssetsTotal", "Активи - Загальні активи, усього", 1.00),
    ("BS1_AssetsNet", "Активи - Чисті активи, усього", 0.94),
    ("BS1_LiabTotal", "Зобов'язання - Усього зобов'язань", 0.80),
    ("BS1_CapitalTotal", "Капітал - Усього власного капіталу", 0.14),
    ("BS1_AssetsLoansClients", "Активи - Кредити та заборгованість клієнтів", 0.42),
    ("BS1_AssetsLoansLE", "Активи - Кредити та заборгованість клієнтів-юридичних осіб", 0.30),
    ("BS1_AssetsLoansIndiv", "Активи - Кредити та заборгованість клієнтів-фізичних осіб", 0.12),
    ("BS1_LiabCust", "Зобов'язання - Кошти клієнтів", 0.77),
    ("BS1_LiabIndiv", "Зобов'язання - Кошти клієнтів-фізичних осіб", 0.44),
    ("BS1_LiabLE", "Зобов'язання - Кошти суб'єктів господарювання та небанківських фінансових установ", 0.33),
    ("BS1_AssetsProvTotal", "Активи - Резерви, усього", 0.07),
    ("BS1_AssetsProvLoansLE", "Активи - Резерви під знецінення кредитів та заборгованості клієнтів-юридичних осіб", 0.04),
    ("BS1_AssetsProvLoansIndiv", "Активи - Резерви під знецінення кредитів та заборгованості клієнтів-фізичних осіб", 0.02),
    ("BS1_NetInterIncomeCosts", "Доходи і витрати - Чистий процентний дохід/(Чисті процентні витрати)", 0.05),
    ("BS1_NetCommIncomeCosts", "Доходи і витрати - Чистий комісійний дохід/(Чисті комісійні витрати)", 0.02),
    ("BS1_AdminOperCosts", "Доходи і витрати - Адміністративні та інші операційні витрати", 0.03),
    ("BS1_ProfitLossAfterTax", "Доходи і витрати - Прибуток/(збиток) після оподаткування", 0.022),
    ("BS1_SelIndAssets", "Окремі показники - Активні банківські операції банку, за якими визначається "
                         "розмір кредитного ризику", 0.50),
    ("BS1_SelIndAssetsNonperf", "Окремі показники - Активні банківські операції банку, за якими визначається "
                                "розмір кредитного ризику: із них непрацюючі активи", 0.06),
    ("BS1_ShareCapital", "Капітал - Статутний капітал", 0.10),
    ("BS1_LiabIndivDemand", "Зобов'язання - Кошти клієнтів-фізичних осіб на вимогу", 0.15),
    ("BS1_LiabLEDemand", "Зобов'язання - Кошти клієнтів-юридичних осіб на вимогу", 0.20),
    ("BS1_LiabBanks", "Зобов'язання - Кошти банків", 0.02),
    ("BS1_LiabSubDebt", "Зобов'язання - Субординований борг", 0.01),
    ("BS1_AssetsCash", "Активи - Грошові кошти та їх еквіваленти", 0.08),
    ("BS1_AssetsOtherBanks", "Активи - Кошти в інших банках, усього", 0.05),
    ("BS1_AssetsIGLBRefinNBU", "Активи - ОВДП, що рефінансуються НБУ", 0.10),
    ("BS1_InterestIncome", "Доходи і витрати - Процентні доходи", 0.09),
    ("BS1_InterestCosts", "Доходи і витрати - Процентні витрати", 0.05),
    ("BS1_CommIncome", "Доходи і витрати - Комісійні доходи", 0.03),
    ("BS1_IncomeTotal", "Доходи і витрати - Всього доходів", 0.12),
    ("BS1_ExpenTotal", "Доходи і витрати - Всього витрат", 0.10),
    ("BS1_AllocProv", "Доходи і витрати - Відрахування до резервів", 0.01),
    ("BS1_Payroll", "Доходи і витрати - Заробітна плата персоналу", 0.015),
    ("BS1_NetIncome", "Доходи і витрати - Торговий результат", 0.005),
    ("BS1_IndReturnAssets", "Окремі показники - Рентабельність активів (%)", -1.0),
]


def make_mock(dates):
    """Генерує ДЕМО-дані такої ж структури, як відповідь API, для перевірки пайплайну."""
    random.seed(42)
    per_bank, system, pb_dts, sy_dts = [], [], [], []
    trend = {b[0]: random.uniform(-0.004, 0.020) for b in DEMO_BANKS}
    for i, d in enumerate(dates):
        dts = f"{d[6:]}.{d[4:6]}.{d[:4]}"
        month = int(d[4:6])
        sys_assets = 0.0
        for nkb, name, base in DEMO_BANKS:
            g = (1 + trend[nkb]) ** i * (1 + random.uniform(-0.012, 0.012))
            assets = base * g
            sys_assets += assets
            for code, label, ratio in DEMO_INDICATORS:
                if ratio < 0:                      # рентабельність активів, %
                    v = round(random.uniform(-1.5, 6.0), 2)
                elif code in ("BS1_AssetsNet", "BS1_LiabTotal", "BS1_CapitalTotal"):
                    # без шуму: у демо-даних баланс має сходитися так само, як у реальних
                    v = assets * 1000 * ratio
                elif code == "BS1_ProfitLossAfterTax":
                    v = assets * 1000 * 0.022 * (month / 12.0) * random.uniform(0.4, 1.6)
                else:
                    v = assets * 1000 * ratio * (1 + random.uniform(-0.05, 0.05))
                # як у справжньому API: показник розбитий за виміром r034 (вид валюти)
                fx_part = round(v * random.uniform(0.05, 0.35), 3)
                grp = ("A" if re.search(r"ПРИВАТБАНК|ОЩАДБАНК|УКРЕКСІМБАНК|УКРГАЗБАНК|СЕНС", name)
                       else "F" if re.search(r"РАЙФФАЙЗЕН|УКРСИББАНК|ОТП|АГРІКОЛЬ|ПРОКРЕДИТ|СІТІБАНК|ІНГ |ПРАВЕКС|КРЕДОБАНК|ПІРЕУС", name)
                       else "E")
                for dim, part in (("1", fx_part), ("2", round(v - fx_part, 3))):
                    per_bank.append({"nkb": nkb, "fullname": name, "id_api": code, "r034": dim,
                                     "gr_bank": grp, "txt": label, "value": part, "freq": "M"})
                    pb_dts.append(to_iso(d))
        for code, label, ratio in [("BS3_Assets", "Активи", 1.0),
                                   ("BS3_AssetsFC", "Активи в іноземній валюті", 0.20),
                                   ("BS3_Equity", "Капітал", 0.14),
                                   ("BS3_BanksLiab", "Зобов'язання банків", 0.86),
                                   ("BS3_NumberBanks", "Кількість діючих банків", 0.0),
                                   ("BS3_ReturnAssets", "Рентабельність активів, %", 0.0),
                                   ("BS3_ReturnEquity", "Рентабельність капіталу, %", 0.0)]:
            if code == "BS3_NumberBanks":
                v = len(DEMO_BANKS)
            elif code == "BS3_ReturnAssets":
                v = round(random.uniform(2.0, 5.5), 2)
            elif code == "BS3_ReturnEquity":
                v = round(random.uniform(20.0, 45.0), 2)
            else:
                v = round(sys_assets * ratio, 1)
            system.append({"dt": dts, "id_api": code, "txt": label, "value": v, "freq": "M"})
            sy_dts.append(to_iso(d))
    return per_bank, system, pb_dts, sy_dts


# ----------------------------------------------------------------------------- main


def write_csv(path, rows, header):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    log(f"    -> {os.path.relpath(path, HERE)}: {len(rows):,} рядків")


# ------------------------------------------------- додаткові набори НБУ
# key      — облікова ставка та ставки овернайт (щоденно)
# exchange — офіційний курс валют на дату
# kursf    — середньомісячний курс і обсяги валютного ринку
# mir      — ринкові ставки за новими кредитами й депозитами (агрегат по системі)
# klk      — кредити та НЕПРАЦЮЮЧІ кредити за категоріями боржників (по банках)
# Позабалансові рахунки, які забираємо з osb.
# 9000 «Надані гарантії» — той самий рахунок, за яким портфель гарантій рахує НБУ й АУБ.
# Решту додано «на виріст»: щоб увімкнути, достатньо дописати номер і назву в GUAR_ACCOUNTS
# у build_dashboard.py — дані вже качатимуться.
OSB_ACCOUNTS = {"9000", "9003", "9020", "9023", "9122", "9129", "9031"}


def first_available(apikod, dates, period="m"):
    """З якої дати набір реально віддає дані.

    НБУ на «порожні» дати відповідає 400, і питати кожну з них по черзі — це
    десятки зайвих запитів. Тому шукаємо межу двійковим пошуком: 5–7 запитів
    замість шести десятків.
    """
    def has(d):
        url = (f"{BASE}/{apikod}?period={period}&date={d}&json" if period
               else f"{BASE}/{apikod}?date={d}&json")
        recs = http_get_json(url, retries=2, timeout=180)
        if recs is None:                         # обрив зв'язку або таймаут
            recs = http_get_json(url, retries=3, timeout=420)
        if recs is None:
            # Не знаємо, є дані чи ні. Вважаємо, що є: зайвий запит коштує секунду,
            # а помилкове «немає» мовчки обрізало б історію показника на роки.
            log(f"    ! {apikod} {d}: перевірку не вдалось зробити, вважаємо що дані є")
            return True
        return bool(recs)

    hi = len(dates) - 1
    while hi >= 0 and not has(dates[hi]):        # хвіст може бути порожній: дані ще не вийшли
        hi -= 1
    if hi < 0:
        return None
    lo = 0
    while lo < hi:
        mid = (lo + hi) // 2
        if has(dates[mid]):
            hi = mid
        else:
            lo = mid + 1
    # Двійковий пошук вірить, що «є дані» — це суцільний хвіст. Якщо в середині
    # трапився збій мережі, межа могла з'їхати вгору. Перевіряємо сусідню дату
    # знизу і, якщо там дані все-таки є, спускаємось далі.
    while lo > 0 and has(dates[lo - 1]):
        lo -= 1
    return dates[lo]


def fetch_extras(dates, refresh_last=1):
    log("\nДодаткові набори НБУ (ставки, курс, непрацюючі кредити):")
    rates, fx, market, klk, osb = [], [], [], [], []
    s080_seen, dims_seen = set(), set()

    # одноразово визначаємо, з якої дати кожен набір взагалі має дані
    starts = {}
    for kod, per in [("osb", "m"), ("klk", "m"), ("mir", "m"), ("key", "d"),
                     ("kursf", "m"), ("exchange", None)]:
        starts[kod] = first_available(kod, dates, per)
        log(f"  {kod}: дані з {starts[kod] or 'немає взагалі'}")
    osb_from = starts.get("osb") or "99999999"
    skip = {k for k, v in starts.items() if v is None}

    for i, d in enumerate(dates, 1):
        force = i > len(dates) - refresh_last
        iso = to_iso(d)
        if i % 12 == 0 or i == len(dates):
            log(f"  [{i}/{len(dates)}] {d}")

        if "key" not in skip and d >= (starts.get("key") or d):
          for r in (fetch_dataset("key", d, "d", force=force) or []):
            v = norm_num(r.get("value"))
            if v is not None:
                rates.append([iso, str(r.get("id_api", "")).strip(), str(r.get("txt", "")).strip(), v])

        if "kursf" not in skip and d >= (starts.get("kursf") or d):
          for r in (fetch_dataset("kursf", d, "m", force=force) or []):
            v = norm_num(r.get("value"))
            if v is not None:
                rates.append([iso, str(r.get("id_api", "")).strip(), str(r.get("txt", "")).strip(), v])

        if "exchange" not in skip and d >= (starts.get("exchange") or d):
          for r in (fetch_dataset("exchange", d, None, force=force) or []):
            if str(r.get("cc")) in ("USD", "EUR"):
                v = norm_num(r.get("rate"))
                if v is not None:
                    fx.append([iso, str(r.get("cc")), v])

        # mir: беремо лише ставки (tzep=*_ir) і лише підсумкові розрізи, крім валюти
        if "mir" not in skip and d >= (starts.get("mir") or d):
          for r in (fetch_dataset("mir", d, "m", force=force) or []):
            if not str(r.get("tzep", "")).lower().endswith("ir"):
                continue
            if any(str(r.get(k, "total")) != "total" for k in ("ods180", "odk111", "odkodter", "odf074")):
                continue
            v = norm_num(r.get("value"))
            if v is not None:
                market.append([iso, str(r.get("id_api", "")).strip(), str(r.get("txt", "")).strip(),
                               str(r.get("odr030", "")).strip(), v])

        # osb: набір великий (80 тис. записів на дату), тож у кеш кладемо вже відфільтроване —
        # лише потрібні позабалансові рахунки і лише залишки (t025=1 актив, 2 пасив).
        # Порожній кеш теж зберігаємо: якщо НБУ не має даних на цю дату, повторно не питаємо.
        osb_path = os.path.join(RAW, f"osb9_{d}.json")
        if os.path.exists(osb_path) and not force:
            with open(osb_path, encoding="utf-8") as f:
                try:
                    keep = json.load(f)
                except json.JSONDecodeError:
                    keep = []
        elif d < osb_from:
            keep = []                          # раніше цієї дати набору просто немає
        else:
            keep = []
            for r in (fetch_dataset("osb", d, "m", force=force) or []):
                if str(r.get("r020", "")) in OSB_ACCOUNTS and str(r.get("t025")) in ("1", "2"):
                    keep.append({k: r.get(k) for k in ("nkb", "r020", "t025", "r034", "value")})
            with open(osb_path, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False)
            big = os.path.join(RAW, f"osb_{d}.json")
            if os.path.exists(big):
                os.remove(big)          # сирий файл на 10 МБ більше не потрібен
        # НБУ віддає кожен залишок двічі — той самий рядок повторюється в відповіді.
        # Прибираємо дублікати одразу, інакше портфель гарантій подвоїться.
        osb_seen = set()
        for r in keep:
            v = norm_num(r.get("value"))
            if v is None:
                continue
            row = [iso, str(r.get("nkb", "")).strip().lstrip("0"), str(r.get("r020")),
                   str(r.get("t025")), str(r.get("r034")), v]
            key = tuple(row)
            if key in osb_seen:
                continue
            osb_seen.add(key)
            osb.append(row)

        if "klk" not in skip and d >= (starts.get("klk") or d):
          for r in (fetch_dataset("klk", d, "m", force=force) or []):
            v = norm_num(r.get("value"))
            if v is None:
                continue
            s080_seen.add(str(r.get("s080", "")))
            dims_seen.add(str(r.get("r034", "")))
            klk.append([iso, str(r.get("nkb", "")).strip().lstrip("0"),
                        str(r.get("id_api", "")).strip(), str(r.get("r034", "")).strip(),
                        str(r.get("s080", "")).strip(), v])

    write_csv(os.path.join(DATA, "rates_long.csv"), rates, ["dt", "ind_code", "ind_name", "value"])
    write_csv(os.path.join(DATA, "fx_long.csv"), fx, ["dt", "cc", "rate"])
    write_csv(os.path.join(DATA, "market_rates.csv"), market,
              ["dt", "ind_code", "ind_name", "curr", "value"])
    write_csv(os.path.join(DATA, "klk_long.csv"), klk,
              ["dt", "bank_id", "ind_code", "dim_curr", "dim_s080", "value"])
    write_csv(os.path.join(DATA, "osb_long.csv"), osb,
              ["dt", "bank_id", "acc", "t025", "dim_curr", "value"])
    if klk:
        log(f"    (у klk розрізи: r034={sorted(dims_seen)}, s080={sorted(s080_seen)})")


def mock_extras(dates):
    """Демо-версія додаткових наборів: та сама структура файлів, вигадані цифри."""
    random.seed(7)
    rates, fx, market, klk, osb = [], [], [], [], []
    usd, policy = 38.0, 13.5
    for i, d in enumerate(dates):
        iso = to_iso(d)
        usd *= 1 + random.uniform(-0.004, 0.012)
        policy = max(9.0, min(25.0, policy + random.choice([0, 0, 0, -0.5, 0.5])))
        rates.append([iso, "KEY_PolicyRate", "Облікова ставка Національного банку України", round(policy, 2)])
        rates.append([iso, "KEY_RateOvernightCD", "Ставка за депозитними сертифікатами овернайт",
                      round(policy - 1.0, 2)])
        rates.append([iso, "AvgKursPerM", "Курс гривні до долара США (середньозважений за місяць)",
                      round(usd, 4)])
        fx.append([iso, "USD", round(usd, 4)])
        fx.append([iso, "EUR", round(usd * 1.08, 4)])
        for code, name, base in [("New_Deposits_Households", "Нові депозити домашніх господарств", 12.5),
                                 ("New_Deposits_NFinCorp", "Нові депозити нефінансових корпорацій", 9.5),
                                 ("New_Loans_Households", "Нові кредити домашнім господарствам", 29.0),
                                 ("New_Loans_NFinCorp", "Нові кредити нефінансовим корпораціям", 17.5)]:
            market.append([iso, code, name, "01", round(base + policy - 13.5 + random.uniform(-0.8, 0.8), 2)])
        for nkb, bank, _ in DEMO_BANKS:
            key = abs(hash(nkb)) % 100 / 100
            loans_fo = 300 * (0.5 + key) * (1 + i * 0.004)
            loans_uo = 700 * (0.5 + key) * (1 + i * 0.004)
            npl = 0.02 + key * 0.18 + random.uniform(-0.01, 0.01)
            for code, val in [("Klk_FO", loans_fo), ("Klk_UO", loans_uo),
                              ("Klk_FO_NPL", loans_fo * npl), ("Klk_UO_NPL", loans_uo * npl * 1.2),
                              ("Klk_Ryz_FO", loans_fo * npl * 0.7), ("Klk_Ryz_UO", loans_uo * npl * 0.8)]:
                klk.append([iso, nkb, code, "1", "M", round(val, 4)])
    write_csv(os.path.join(DATA, "rates_long.csv"), rates, ["dt", "ind_code", "ind_name", "value"])
    write_csv(os.path.join(DATA, "fx_long.csv"), fx, ["dt", "cc", "rate"])
    write_csv(os.path.join(DATA, "market_rates.csv"), market,
              ["dt", "ind_code", "ind_name", "curr", "value"])
    write_csv(os.path.join(DATA, "klk_long.csv"), klk,
              ["dt", "bank_id", "ind_code", "dim_curr", "dim_s080", "value"])

    # гарантії: рахунок 9000, залишок (t025=1)
    osb = []
    base = {}
    for j, (nkb, name, assets) in enumerate(DEMO_BANKS):
        base[nkb] = max(0.0, assets * random.uniform(0.002, 0.06)) if j % 7 else 0.0
    base["325213"] = 220.93 / 1.0                      # Оксі Банк — як у реальних даних
    for i, d in enumerate(dates):
        iso = to_iso(d)
        for nkb, _, _ in DEMO_BANKS:
            v = base[nkb] * (1 + i * random.uniform(-0.004, 0.006))
            osb.append([iso, nkb, "9000", "1", "1", round(max(0.0, v), 4)])
    write_csv(os.path.join(DATA, "osb_long.csv"), osb,
              ["dt", "bank_id", "acc", "t025", "dim_curr", "value"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20210101", help="перша звітна дата, YYYYMMDD (мін. 20180201)")
    ap.add_argument("--end", default=None, help="остання звітна дата, YYYYMMDD (типово — поточний місяць)")
    ap.add_argument("--refresh-last", type=int, default=1, help="перекачати N останніх дат навіть якщо є в кеші")
    ap.add_argument("--mock", action="store_true", help="згенерувати ДЕМО-дані без звернення до API")
    args = ap.parse_args()

    os.makedirs(RAW, exist_ok=True)
    os.makedirs(DATA, exist_ok=True)

    today = dt.date.today()
    end = args.end or f"{today.year:04d}{today.month:02d}01"
    dates = month_starts(args.start, end)
    log(f"Звітних дат до обробки: {len(dates)} ({dates[0]} … {dates[-1]})")

    # bank_dts / sys_dts — звітна дата, з якою було зроблено запит. Використовується
    # як надійний запасний варіант, якщо поле дати у відповіді API не розпізналося.
    if args.mock:
        log("РЕЖИМ ДЕМО-ДАНИХ: мережа не використовується, цифри вигадані.")
        bank_recs_all, sys_recs_all, bank_dts, sys_dts = make_mock(dates)
    else:
        bank_recs_all, sys_recs_all, bank_dts, sys_dts = [], [], [], []
        for i, d in enumerate(dates, 1):
            force = i > len(dates) - args.refresh_last
            log(f"  [{i}/{len(dates)}] {d}")
            b = fetch_dataset("banksfinrep", d, "m", force=force)
            s = fetch_dataset("basindbank", d, "m", force=force)
            if b:
                bank_recs_all.extend(b)
                bank_dts.extend([to_iso(d)] * len(b))
            else:
                log(f"    (по банках даних на {d} немає)")
            if s:
                sys_recs_all.extend(s)
                sys_dts.extend([to_iso(d)] * len(s))

    if not bank_recs_all:
        log("ПОМИЛКА: жодного запису по банках не отримано. Перевірте доступ до bank.gov.ua.")
        sys.exit(1)

    report = ""
    bs, bstats = detect_schema(bank_recs_all, per_bank=True)
    report += schema_report("banksfinrep (по банках)", bs, bstats)
    log("Схема banksfinrep: " + json.dumps(bs, ensure_ascii=False))

    if not bs.get("date"):
        log("    (поле дати у відповіді API не розпізналося — беру звітну дату із самого запиту)")
    missing = [r for r in ("value", "ind_code", "bank_name") if not bs.get(r)]
    if missing:
        log(f"ПОМИЛКА: не вдалося розпізнати поля {missing}. Дивіться data/_schema_report.txt")
        with open(os.path.join(DATA, "_schema_report.txt"), "w", encoding="utf-8") as f:
            f.write(report)
        sys.exit(2)

    rows = []
    for i, r in enumerate(bank_recs_all):
        v = norm_num(r.get(bs["value"]))
        if v is None:
            continue
        rows.append([
            to_iso(r.get(bs["date"])) if bs.get("date") else bank_dts[i],
            str(r.get(bs["bank_id"]) or "").strip() if bs.get("bank_id") else "",
            str(r.get(bs["bank_name"]) or "").strip(),
            str(r.get(bs["ind_code"]) or "").strip(),
            str(r.get(bs["ind_name"]) or "").strip() if bs.get("ind_name") else "",
            v,
            str(r.get(bs["dim"]) or "").strip() if bs.get("dim") else "",
        ])
    if bs.get("dim"):
        log(f"    (показники розбиті за виміром '{bs['dim']}' — під час збирання дашборду вони підсумовуються)")
    write_csv(os.path.join(DATA, "banks_long.csv"), rows,
              ["dt", "bank_id", "bank_name", "ind_code", "ind_name", "value", "dim"])

    srows = []
    if sys_recs_all:
        ss, sstats = detect_schema(sys_recs_all, per_bank=False)
        report += schema_report("basindbank (система)", ss, sstats)
        for i, r in enumerate(sys_recs_all):
            v = norm_num(r.get(ss["value"])) if ss.get("value") else None
            if v is None:
                continue
            srows.append([to_iso(r.get(ss["date"])) if ss.get("date") else sys_dts[i],
                          str(r.get(ss["ind_code"]) or "").strip(),
                          str(r.get(ss["ind_name"]) or "").strip() if ss.get("ind_name") else "", v])
        write_csv(os.path.join(DATA, "system_long.csv"), srows, ["dt", "ind_code", "ind_name", "value"])

    # довідник банків: група за класифікацією НБУ та остання дата, на яку є звітність
    if bs.get("group"):
        binfo = {}
        for i, r in enumerate(bank_recs_all):
            bid = str(r.get(bs["bank_id"]) or "").strip() if bs.get("bank_id") else ""
            bid = bid or str(r.get(bs["bank_name"]) or "").strip()
            d = to_iso(r.get(bs["date"])) if bs.get("date") else bank_dts[i]
            cur = binfo.get(bid)
            if cur is None or d >= cur[2]:
                binfo[bid] = (str(r.get(bs["bank_name"]) or "").strip(),
                              str(r.get(bs["group"]) or "").strip(), d)
        write_csv(os.path.join(DATA, "bank_groups.csv"),
                  [[b, v[0], v[1], v[2]] for b, v in sorted(binfo.items())],
                  ["bank_id", "bank_name", "group_code", "last_date"])

    inds = {}
    for _, _, _, code, name, *_ in rows:
        inds.setdefault(code, name)
    write_csv(os.path.join(DATA, "indicators.csv"), sorted(inds.items()), ["ind_code", "ind_name"])

    if args.mock:
        mock_extras(dates)
    else:
        fetch_extras(dates, args.refresh_last)

    with open(os.path.join(DATA, "_schema_report.txt"), "w", encoding="utf-8") as f:
        f.write(report)
    with open(os.path.join(DATA, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": dt.datetime.now().isoformat(timespec="seconds"),
                   "mock": bool(args.mock), "dates": len(dates),
                   "start": dates[0], "end": dates[-1]}, f, ensure_ascii=False, indent=2)
    log("Готово. Далі: python3 build_dashboard.py")


if __name__ == "__main__":
    main()
