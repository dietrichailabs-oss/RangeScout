"""Independent high-confidence runtime and bundled-master semantic audit for R9."""
from __future__ import annotations
import argparse, csv, io, json, re, sqlite3, tempfile
from collections import Counter
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.company_data.instrument_intelligence import InstrumentReferenceSeeder, InstrumentResolver
from app.company_data.master import provision_company_master
from app.historical_store.repository import HistoricalStore
from app.market_data.discovery import classify_nasdaq_row, parse_nasdaq_directory


def contradictions(asset: str, name: str) -> list[str]:
    out=[]
    if asset=='warrant' and not re.search(r'\bWARRANTS?\b',name,re.I): out.append('warrant_without_warrant_word')
    if asset=='unit' and not re.search(r'\bUNITS?\b',name,re.I): out.append('unit_without_unit_word')
    if asset=='preferred' and re.search(r'\b(COMMON STOCK|COMMON SHARES?|ORDINARY SHARES?)\b',name,re.I) and not re.search(r'\b(PREFERRED (STOCK|SHARES?|SECURIT(?:Y|IES))|PFD)\b',name,re.I): out.append('preferred_but_explicit_common')
    if asset=='etf' and re.search(r'\b(ETN|EXCHANGE[- ]TRADED NOTE)\b',name,re.I): out.append('etn_as_etf')
    return out


def main() -> int:
    ap=argparse.ArgumentParser();ap.add_argument('output',type=Path);args=ap.parse_args()
    root=Path('docs/engineering/v1.6/company_master_sources')
    dynamic=[]; audited=0; parsed=0; test_rows=0
    for filename,symcol,venue in [('nasdaq_nasdaqlisted.txt','Symbol','Q'),('nasdaq_otherlisted.txt','ACT Symbol','N')]:
        text=(root/filename).read_text(encoding='utf-8-sig',errors='replace')
        parsed_rows,_=parse_nasdaq_directory(text,venue);parsed+=len(parsed_rows)
        for row in csv.DictReader(io.StringIO(text),delimiter='|'):
            symbol=str(row.get(symcol) or '').strip().upper();name=str(row.get('Security Name') or '').strip()
            if not symbol or symbol.startswith('FILE CREATION TIME') or not name: continue
            if str(row.get('Test Issue') or '').strip().upper()=='Y': test_rows+=1;continue
            audited+=1;asset,security_type=classify_nasdaq_row(row)
            for reason in contradictions(asset.value,name): dynamic.append({'reason':reason,'symbol':symbol,'name':name,'asset':asset.value,'security_type':security_type})
    database=Path(tempfile.mkdtemp(prefix='rangescout-r9-classifier-'))/'history.sqlite'
    with HistoricalStore(database): pass
    provision_company_master(database);InstrumentReferenceSeeder(database).apply();resolver=InstrumentResolver(database)
    resource=json.loads(Path('resources/RangeScout_Instrument_Classifications.json').read_text(encoding='utf-8'))
    cef_ciks={str(r['cik']).zfill(10) for r in resource['classifications']}
    master=[];false_cef=[]
    with sqlite3.connect(database) as con:
        con.row_factory=sqlite3.Row
        for row in con.execute('select instrument_id,canonical_symbol,security_name,asset_class,instrument_subtype,cik from rs_instruments where is_active=1 order by canonical_symbol'):
            instrument=resolver.by_id(int(row['instrument_id']));name=str(row['security_name'] or '')
            for reason in contradictions(instrument.asset_class,name): master.append({'reason':reason,'symbol':instrument.symbol,'name':name,'asset':instrument.asset_class})
            cik=str(row['cik'] or '').zfill(10) if row['cik'] else ''
            if instrument.asset_class=='closed_end_fund' and cik not in cef_ciks and re.search(r'\b(ETF|ETN|EXCHANGE[- ]TRADED (?:FUND|NOTE))\b',name,re.I):
                false_cef.append({'symbol':instrument.symbol,'name':name})
        active=con.execute('select count(*) from rs_instruments where is_active=1').fetchone()[0]
    payload={'schema':'rangescout.r9-shared-classifier-audit.v1','official_non_test_rows':audited,'runtime_parsed_rows':parsed,'official_test_issue_rows_excluded':test_rows,'dynamic_contradictions':len(dynamic),'dynamic_counts':dict(Counter(x['reason'] for x in dynamic)),'master_active':active,'master_contradictions':len(master),'master_counts':dict(Counter(x['reason'] for x in master)),'explicit_etf_etn_false_cef':len(false_cef),'dynamic_findings':dynamic,'master_findings':master,'false_cef_findings':false_cef,'verdict':'PASS' if not dynamic and not master and not false_cef and parsed==13136 and test_rows==33 else 'FAIL'}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    print(json.dumps({k:payload[k] for k in ('official_non_test_rows','runtime_parsed_rows','official_test_issue_rows_excluded','dynamic_contradictions','master_active','master_contradictions','explicit_etf_etn_false_cef','verdict')},indent=2))
    return 0 if payload['verdict']=='PASS' else 1
if __name__=='__main__': raise SystemExit(main())
