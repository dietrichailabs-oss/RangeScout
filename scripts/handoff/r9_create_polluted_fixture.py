"""Create a real R8-polluted database using an untouched R8 source tree."""
from __future__ import annotations
import argparse, hashlib, json, shutil, sqlite3, sys
from pathlib import Path

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest().upper()
def main():
 ap=argparse.ArgumentParser();ap.add_argument('r8',type=Path);ap.add_argument('output',type=Path);ap.add_argument('record',type=Path);a=ap.parse_args()
 sys.path.insert(0,str(a.r8.resolve()))
 from app.company_data.instrument_intelligence import InstrumentReferenceSeeder
 from app.company_data.master import provision_company_master
 from app.historical_store.repository import HistoricalStore
 from app.market_data.discovery import DiscoveryCoordinator,InstrumentDiscovery,parse_nasdaq_directory
 a.output.parent.mkdir(parents=True,exist_ok=True)
 with HistoricalStore(a.output): pass
 provision_company_master(a.output);InstrumentReferenceSeeder(a.output).apply()
 src=a.r8/'docs/engineering/v1.6/company_master_sources'
 na=(src/'nasdaq_nasdaqlisted.txt').read_text(encoding='utf-8-sig',errors='replace');ot=(src/'nasdaq_otherlisted.txt').read_text(encoding='utf-8-sig',errors='replace')
 nr,ne=parse_nasdaq_directory(na,'Q');orr,oe=parse_nasdaq_directory(ot,'N')
 con=sqlite3.connect(a.output);con.row_factory=sqlite3.Row
 report=InstrumentDiscovery(con).import_snapshot(DiscoveryCoordinator.SOURCE_ID,DiscoveryCoordinator.DISPLAY_NAME,DiscoveryCoordinator.OFFICIAL_URL,nr+orr,(na+ot).encode(),parse_errors=ne+oe)
 dup=con.execute("select count(*) from (select canonical_symbol from rs_instruments where is_active=1 group by canonical_symbol having count(*)>1)").fetchone()[0]
 active=con.execute('select count(*) from rs_instruments where is_active=1').fetchone()[0]
 clone=con.execute("select max(instrument_id) from rs_instruments where canonical_symbol='AAPL' and is_active=1").fetchone()[0]
 stamp='2026-08-24T20:00:00+00:00'
 con.execute("insert or replace into rs_instrument_capabilities values(?,?,?,?,?)",(clone,'quote','applicable','r8-user-cache-fixture',stamp))
 con.execute("insert or replace into rs_last_quotes(instrument_id,last_price,currency,provider_id,received_at_utc,delay_label) values(?,'123.45','USD','yahoo',?,'Delayed')",(clone,stamp))
 con.commit();integrity=con.execute('pragma integrity_check').fetchone()[0];fk=len(con.execute('pragma foreign_key_check').fetchall());con.close()
 user=a.output.parent/'user_state';user.mkdir(exist_ok=True)
 payloads={'watchlists.json':'{"watchlists":[{"id":"qa","symbols":["AAPL","BOE"]}]}\n','notes.json':'{"notes":[{"id":"n1","symbol":"AAPL","text":"preserve"}]}\n','alerts.json':'{"alerts":[{"symbol":"AAPL","threshold":100}]}\n','settings.json':'{"theme":"Dark","recent_symbols":["AAPL","BOE"]}\n'}
 for name,text in payloads.items():(user/name).write_text(text,encoding='utf-8')
 out={'schema':'rangescout.r9-r8-polluted-fixture.v1','r8_source':str(a.r8),'database':str(a.output),'import_report':report.__dict__,'active_before_repair':active,'duplicate_groups_before_repair':dup,'clone_cached_instrument_id':clone,'integrity_check':integrity,'foreign_key_violations':fk,'user_state_hashes':{p.name:sha(p) for p in sorted(user.iterdir())}}
 a.record.parent.mkdir(parents=True,exist_ok=True);a.record.write_text(json.dumps(out,indent=2,sort_keys=True)+'\n',encoding='utf-8');print(json.dumps({k:out[k] for k in ('active_before_repair','duplicate_groups_before_repair','clone_cached_instrument_id','integrity_check','foreign_key_violations')},indent=2));return 0 if active==29551 and dup==13136 else 1
if __name__=='__main__':raise SystemExit(main())
