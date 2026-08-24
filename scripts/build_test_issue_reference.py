"""Build the frozen official Nasdaq Test Issue reference used by R9 repair."""
from __future__ import annotations
import csv, hashlib, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'docs/engineering/v1.6/company_master_sources'
OUT=ROOT/'resources/RangeScout_Nasdaq_Test_Issues.json'
rows=[];sources=[]
for filename,symbol_field in [('nasdaq_nasdaqlisted.txt','Symbol'),('nasdaq_otherlisted.txt','ACT Symbol')]:
 path=SRC/filename;raw=path.read_bytes();sources.append({'filename':filename,'sha256':hashlib.sha256(raw).hexdigest().upper()})
 with path.open('r',encoding='utf-8-sig',errors='replace',newline='') as stream:
  for row in csv.DictReader(stream,delimiter='|'):
   if str(row.get('Test Issue') or '').strip().upper()=='Y':rows.append({'symbol':str(row.get(symbol_field) or '').strip().upper(),'security_name':str(row.get('Security Name') or '').strip(),'official_test_issue':'Y','source_file':filename})
payload={'schema':'rangescout.nasdaq-test-issue-reference.v1','derivation':'Rows whose official Nasdaq Trader Test Issue field equals Y','sources':sources,'count':len(rows),'rows':sorted(rows,key=lambda r:(r['symbol'],r['source_file']))}
OUT.write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n',encoding='utf-8');print(json.dumps({'output':str(OUT),'count':len(rows)},indent=2));raise SystemExit(0 if len(rows)==33 else 1)
