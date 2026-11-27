#!/usr/bin/env python3
"""Fresh publisher job only. Never runs candidate code. No approval creation."""
import argparse,hashlib,json,os,pathlib,re,urllib.request
PIN='5202c2359483a58b2916c58acac5f11ee2df20b8';REPO='arniesaha/drover'
p=argparse.ArgumentParser();p.add_argument('--profile',type=pathlib.Path,required=True);p.add_argument('--proof',type=pathlib.Path,required=True);p.add_argument('--dry-run',action='store_true');a=p.parse_args()
profile=json.loads(a.profile.read_text());proof=json.loads(a.proof.read_text())
assert profile['pull_number']==1029 and profile['app_id']==15368
assert profile['owner_id']!=3646287,'PR author cannot approve their own PR: choose a separately authorized human first'
assert proof['source']==PIN and proof['passed'] is True
assert len(proof['commands'])==3 and all(item['returncode']==0 for item in proof['commands'])
assert proof['counts']['tests']==36 and all(proof['counts'][k]==0 for k in ('skipped','errors','failures'))
revision=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
summary='Independently executed pinned quota/control proof: 36 tests, zero failures/errors/skips; strict boundary and held-out forbidden-import rejection passed. Human judgment and required exact-head CI remain separate.'
text=json.dumps(proof,sort_keys=True,indent=2);assert len(text.encode())<40000
payload=dict(name=profile['behavioral_check'],head_sha=PIN,status='completed',conclusion='success',external_id=revision,output=dict(title='Pinned independent quota/control outcome',summary=summary,text=text))
if a.dry_run:
    print(json.dumps({'profile_revision':revision,'payload':payload},indent=2));raise SystemExit
# Only this short-lived job receives the existing GitHub Actions token. Never persist it.
token=os.environ['GITHUB_TOKEN'];base='https://api.github.com/repos/'+REPO
headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
def request(method,path,body=None):
    req=urllib.request.Request(base+path,headers=headers,method=method,data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req,timeout=20) as response:return json.load(response)
pull=request('GET','/pulls/1029');assert pull['head']['sha']==PIN and pull['user']['id']!=profile['owner_id']
existing=request('GET','/commits/'+PIN+'/check-runs?per_page=100')['check_runs']
matching=[x for x in existing if x['name']==payload['name'] and x.get('external_id')==revision]
if matching:
    assert len(matching)==1,'ambiguous prior publication; inspect, never blindly retry'
    old=request('GET','/check-runs/'+str(matching[0]['id']))
    assert all(old.get(k)==payload[k] for k in ('head_sha','conclusion','external_id')) and all(old['output'][k]==payload['output'][k] for k in ('summary','text')),'prior result differs; inspect'
    print(json.dumps({'reused_check_id':old['id']}))
else:
    result=request('POST','/check-runs',payload);print(json.dumps({'created_check_id':result['id']}))
