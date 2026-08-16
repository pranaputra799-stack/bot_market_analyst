"""Unit tests untuk parser & matcher COT (data/cot.py) — tanpa network.

Menggunakan CSV sintetis berformat legacy futures-only (annual.txt CFTC)
agar parsing, pencocokan instrumen, dan perhitungan net/change teruji.
"""

import unittest

from data.cot import (
    cot_data_from_json,
    cot_data_to_json,
    extract_market,
    format_cot_message,
    interpret_cot,
    parse_cot_csv,
    parse_legacy_csv,
    resolve_instrument,
)

SAMPLE_CSV = (
    "Market and Exchange Names,CFTC Contract Market Code,CFTC Market Code,"
    "As of Date in Form YYYY-MM-DD,Open Interest (All),"
    "Noncommercial Positions-Long (All),Noncommercial Positions-Short (All),"
    "Noncommercial Positions-Spread (All),Commercial Positions-Long (All),"
    "Commercial Positions-Short (All),Total Reportable Positions-Long (All),"
    "Total Reportable Positions-Short (All),Nonreportable Positions-Long (All),"
    "Nonreportable Positions-Short (All),Change in Committments-Long (All),"
    "Change in Committments-Short (All),% of OI-Noncommercial-Long (All),"
    "% of OI-Noncommercial-Short (All),% of OI-Commercial-Long (All),"
    "% of OI-Commercial-Short (All),% of OI-Nonreportable-Long (All),"
    "% of OI-Nonreportable-Short (All),Traders-Noncommercial-Long (All),"
    "Traders-Noncommercial-Short (All),Traders-Noncommercial-Spread (All),"
    "Traders-Commercial-Long (All),Traders-Commercial-Short (All),"
    "Traders-Total-Long (All),Traders-Total-Short (All),Traders-Total-Spread (All),"
    "Pct of OI Held by the 4 Largest Traders-Long (All),"
    "Pct of OI Held by the 4 Largest Traders-Short (All),"
    "Pct of OI Held by the 8 Largest Traders-Long (All),"
    "Pct of OI Held by the 8 Largest Traders-Short (All)\n"
    "GOLD - COMMODITY EXCHANGE INC.,88691,GC,2026-08-11,500000,"
    "300000,200000,50000,150000,250000,500000,500000,0,0,10000,-20000,"
    "60,40,30,50,0,0,200,150,50,100,150,300,300,50,10,20,15,25\n"
    "GOLD - COMMODITY EXCHANGE INC.,88691,GC,2026-08-04,490000,"
    "290000,210000,50000,160000,240000,500000,500000,0,0,8000,-15000,"
    "59,43,33,49,0,0,195,145,50,105,145,300,290,50,10,20,15,25\n"
    "MICRO GOLD - COMMODITY EXCHANGE INC.,99731,MGC,2026-08-11,10000,"
    "6000,4000,1000,2000,3000,9000,8000,1000,2000,500,-500,"
    "60,40,20,30,10,20,50,40,10,20,30,70,70,20,10,20,15,25\n"
    "EURO FX - CHICAGO MERCANTILE EXCHANGE,99741,EC,2026-08-11,700000,"
    "400000,250000,50000,200000,350000,650000,650000,50000,50000,20000,-10000,"
    "57,36,29,50,7,7,180,120,40,100,150,280,270,60,10,20,15,25\n"
    "EURO FX - CHICAGO MERCANTILE EXCHANGE,99741,EC,2026-08-04,680000,"
    "380000,260000,50000,210000,340000,640000,650000,40000,30000,15000,-5000,"
    "56,38,31,50,6,4,170,125,40,105,145,275,270,55,10,20,15,25\n"
    '"CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE",67651,CL,2026-08-11,900000,'
    "500000,400000,100000,300000,400000,900000,900000,0,0,30000,-20000,"
    "56,44,33,44,0,0,300,250,100,200,250,500,500,100,10,20,15,25\n"
    "RUSSELL E-MINI - CHICAGO MERCANTILE EXCHANGE,95426,RM,2026-08-11,419222,"
    "220000,170000,20000,120000,260000,410000,450000,9000,-30000,15000,-8000,"
    "52,41,29,62,2,-7,170,130,20,80,170,250,300,60,10,20,15,25\n"
    "RUSSELL 2000 ANNUAL DIVIDEND - CHICAGO MERCANTILE EXCHANGE,95500,R2D,2026-08-11,44200,"
    "20000,18000,3000,15000,20000,38000,41000,6200,3200,1000,-500,"
    "45,41,34,45,14,7,80,70,10,40,60,120,130,30,10,20,15,25\n"
    "DJIA Consolidated - CHICAGO BOARD OF TRADE,13560,DJ,2026-08-11,89844,"
    "45000,30000,8000,20000,45000,73000,83000,16844,6844,5000,-2000,"
    "50,33,22,50,19,8,90,70,20,40,90,130,160,30,10,20,15,25\n"
    "DJIA x $5 - CHICAGO BOARD OF TRADE,13560,DJX,2026-08-11,60000,"
    "30000,20000,5000,15000,30000,50000,55000,10000,5000,3000,-1000,"
    "50,33,25,50,17,8,70,50,15,30,60,100,110,25,10,20,15,25\n"
    "MICRO E-MINI DJIA (x$0.5) - CHICAGO BOARD OF TRADE,13890,MYM,2026-08-11,20000,"
    "10000,8000,2000,5000,10000,17000,18000,3000,2000,500,-200,"
    "50,40,25,50,15,10,40,30,10,20,40,60,70,15,10,20,15,25\n"
)


class TestParseLegacyCsv(unittest.TestCase):
    def test_parses_rows_with_expected_keys(self):
        rows = parse_legacy_csv(SAMPLE_CSV)
        self.assertEqual(len(rows), 11)
        gold = [r for r in rows if r["name"].startswith("GOLD")][0]
        self.assertEqual(gold["oi"], 500000)
        self.assertEqual(gold["nc_long"], 300000)
        self.assertEqual(gold["nc_short"], 200000)
        self.assertEqual(gold["c_long"], 150000)
        self.assertEqual(str(gold["date"]), "2026-08-11")

    def test_skips_rows_without_valid_date(self):
        broken = "header,row\nGOLD,88691,GC,not-a-date,100\n"
        # header tidak dikenali → []
        self.assertEqual(parse_legacy_csv(broken), [])

    def test_empty_text_returns_empty(self):
        self.assertEqual(parse_legacy_csv(""), [])
        self.assertEqual(parse_legacy_csv(None), [])


class TestResolveInstrument(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(resolve_instrument("gold")["keywords"], ["gold"])
        self.assertEqual(resolve_instrument("XAU/USD")["keywords"], ["gold"])
        self.assertEqual(resolve_instrument("emas")["keywords"], ["gold"])
        self.assertEqual(resolve_instrument("eur")["keywords"], ["euro fx"])
        self.assertEqual(resolve_instrument("EURUSD")["keywords"], ["euro fx"])
        self.assertEqual(resolve_instrument("eurusd")["keywords"], ["euro fx"])
        self.assertEqual(resolve_instrument("usd/jpy")["keywords"], ["japanese yen"])

    def test_new_financial_instruments(self):
        # Instrumen finansial yang ditambahkan (2Y/5Y note, Fed Funds, SOFR, dll)
        self.assertEqual(resolve_instrument("us2y")["keywords"], ["ust 2y"])
        self.assertEqual(resolve_instrument("us5y")["keywords"], ["ust 5y"])
        self.assertEqual(resolve_instrument("fed funds")["keywords"], ["fed funds"])
        self.assertEqual(resolve_instrument("sofr")["keywords"], ["sofr 3m"])
        self.assertEqual(resolve_instrument("sofr1m")["keywords"], ["sofr 1m"])
        self.assertEqual(resolve_instrument("sp400")["keywords"], ["s&p 400"])
        self.assertEqual(resolve_instrument("russell")["keywords"], ["russell e-mini"])
        self.assertEqual(resolve_instrument("vix")["keywords"], ["vix"])
        # E-mini Dow (DJIA)
        self.assertEqual(resolve_instrument("dow")["keywords"], ["djia"])
        self.assertEqual(resolve_instrument("DJIA")["keywords"], ["djia"])
        self.assertEqual(resolve_instrument("e-mini dow")["keywords"], ["djia"])
        # Alias lama tetap bekerja
        self.assertEqual(resolve_instrument("10y")["keywords"], ["ust 10y"])
        self.assertEqual(resolve_instrument("30y")["keywords"], ["ust bond"])

    def test_short_alias_not_matched_as_substring(self):
        # Alias pendek (mis. '1m') TIDAK boleh cocok di tengah kata lain —
        # '1000' bukan instrumen COT.
        self.assertIsNone(resolve_instrument("1000"))
        self.assertIsNone(resolve_instrument("1000usd"))

    def test_unknown_returns_none(self):
        self.assertIsNone(resolve_instrument("usd/idr"))
        self.assertIsNone(resolve_instrument("xyz"))
        self.assertIsNone(resolve_instrument(""))


class TestExtractMarket(unittest.TestCase):
    def setUp(self):
        self.rows = parse_legacy_csv(SAMPLE_CSV)

    def test_gold_picks_standard_contract_and_latest_week(self):
        cfg = resolve_instrument("gold")
        data = extract_market(self.rows, cfg)
        self.assertIsNotNone(data)
        # Kontrak standar (OI terbesar) — bukan MICRO GOLD
        self.assertIn("GOLD", data["market_name"])
        self.assertNotIn("MICRO", data["market_name"])
        self.assertEqual(str(data["report_date"]), "2026-08-11")
        self.assertEqual(data["open_interest"], 500000)
        # Net non-commercial = long - short = 300000 - 200000
        self.assertEqual(data["noncommercial"]["net"], 100000)
        # Change vs minggu lalu = 100000 - (290000-210000) = 20000
        self.assertEqual(data["noncommercial"]["change"], 20000)
        # Commercial net = 150000 - 250000
        self.assertEqual(data["commercial"]["net"], -100000)

    def test_eur_fx(self):
        cfg = resolve_instrument("eur")
        data = extract_market(self.rows, cfg)
        self.assertIsNotNone(data)
        self.assertIn("EURO FX", data["market_name"])
        self.assertEqual(data["noncommercial"]["net"], 150000)

    def test_commodity_with_comma_in_name(self):
        cfg = resolve_instrument("oil")
        data = extract_market(self.rows, cfg)
        self.assertIsNotNone(data)
        self.assertIn("CRUDE OIL", data["market_name"])
        self.assertEqual(data["open_interest"], 900000)

    def test_russell_picks_eminir_contract_not_dividend_index(self):
        """Regresi: keyword lama 'russell 2000' cocok dengan 'RUSSELL 2000
        ANNUAL DIVIDEND' (indeks dividen) — harusnya kontrak 'RUSSELL E-MINI'."""
        cfg = resolve_instrument("russell")
        data = extract_market(self.rows, cfg)
        self.assertIsNotNone(data)
        self.assertEqual(data["market_name"], "RUSSELL E-MINI - CHICAGO MERCANTILE EXCHANGE")
        self.assertNotIn("DIVIDEND", data["market_name"])

    def test_dow_picks_djia_contract(self):
        cfg = resolve_instrument("dow")
        data = extract_market(self.rows, cfg)
        self.assertIsNotNone(data)
        self.assertIn("DJIA", data["market_name"])

    def test_missing_market_returns_none(self):
        cfg = {"keywords": ["soybeans"], "display": "Soybean Futures"}
        self.assertIsNone(extract_market(self.rows, cfg))


TFF_CSV = (
    '"Market_and_Exchange_Names","As_of_Date_In_Form_YYMMDD","Report_Date_as_YYYY-MM-DD",'
    '"CFTC_Contract_Market_Code","CFTC_Market_Code","CFTC_Region_Code","CFTC_Commodity_Code",'
    '"Open_Interest_All","Dealer_Positions_Long_All","Dealer_Positions_Short_All",'
    '"Dealer_Positions_Spread_All","Asset_Mgr_Positions_Long_All","Asset_Mgr_Positions_Short_All",'
    '"Asset_Mgr_Positions_Spread_All","Lev_Money_Positions_Long_All","Lev_Money_Positions_Short_All",'
    '"Lev_Money_Positions_Spread_All","Other_Rept_Positions_Long_All","Other_Rept_Positions_Short_All",'
    '"Other_Rept_Positions_Spread_All","Tot_Rept_Positions_Long_All","Tot_Rept_Positions_Short_All",'
    '"NonRept_Positions_Long_All","NonRept_Positions_Short_All","Change_in_Open_Interest_All",'
    '"Contract_Units","FutOnly_or_Combined"\n'
    '"USD INDEX - ICE FUTURES U.S.",260811,2026-08-11,098662,USDX,00,100 ,  49541,  30000,   25000,    2000,   10000,   15000,     500,   20000,   12000,    1000,    5000,    6000,     300,  66541,  58500,   2000,   1500,   -1000,USDX,F\n'
    '"USD INDEX - ICE FUTURES U.S.",260804,2026-08-04,098662,USDX,00,100 ,  50500,  31000,   24000,    2000,   10000,   14000,     500,   18000,   13000,    1000,    5000,    5500,     300,  66000,  57500,   1500,   1200,     500,USDX,F\n'
)


class TestParseTff(unittest.TestCase):
    def test_parses_tff_rows(self):
        rows = parse_cot_csv(TFF_CSV, schema="tff")
        self.assertEqual(len(rows), 2)
        row = rows[0]
        self.assertEqual(row["name"], "USD INDEX - ICE FUTURES U.S.")
        self.assertEqual(row["oi"], 49541)
        # Lev Money = speculative (non-commercial)
        self.assertEqual(row["nc_long"], 20000)
        self.assertEqual(row["nc_short"], 12000)
        # Dealer = hedger (commercial)
        self.assertEqual(row["c_long"], 30000)
        self.assertEqual(row["c_short"], 25000)

    def test_tff_extract_market(self):
        cfg = {
            "keywords": ["usd index"],
            "display": "US Dollar Index Futures (ICE)",
            "prefer": ["ice futures u.s"],
            "report": "tff",
        }
        data = extract_market(parse_cot_csv(TFF_CSV, schema="tff"), cfg)
        self.assertIsNotNone(data)
        self.assertEqual(data["noncommercial"]["net"], 8000)  # 20000 - 12000
        self.assertEqual(data["noncommercial"]["change"], 3000)  # 8000 - 5000
        self.assertEqual(data["commercial"]["net"], 5000)  # 30000 - 25000


class TestInterpretAndFormat(unittest.TestCase):
    def setUp(self):
        cfg = resolve_instrument("gold")
        self.data = extract_market(parse_legacy_csv(SAMPLE_CSV), cfg)

    def test_interpret_mentions_net_long(self):
        text = interpret_cot(self.data)
        self.assertIn("LONG", text)
        self.assertIn("100,000", text)

    def test_format_message_contains_numbers(self):
        msg = format_cot_message(self.data)
        self.assertIn("COT REPORT", msg)
        self.assertIn("500,000", msg)
        self.assertIn("Open Interest", msg)


class TestJsonRoundTrip(unittest.TestCase):
    def test_date_conversion(self):
        cfg = resolve_instrument("gold")
        data = extract_market(parse_legacy_csv(SAMPLE_CSV), cfg)
        j = cot_data_to_json(data)
        self.assertIsInstance(j["report_date"], str)
        back = cot_data_from_json(j)
        self.assertEqual(str(back["report_date"]), "2026-08-11")
        self.assertEqual(back["noncommercial"]["net"], 100000)


if __name__ == "__main__":
    unittest.main()
