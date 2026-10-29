import json,sys
src=sys.argv[1]; out=open(sys.argv[2],'w')
results={}
calls=[]
for ln,line in enumerate(open(src),1):
    try: d=json.loads(line)
    except: continue
    ts=d.get('timestamp','')
    m=d.get('message') or {}
    c=m.get('content')
    if not isinstance(c,list): continue
    for b in c:
        if b.get('type')=='tool_use':
            calls.append((ln,ts,b['id'],b['name'],b.get('input')))
        elif b.get('type')=='tool_result':
            r=b.get('content')
            if isinstance(r,list): r='\n'.join(x.get('text','') for x in r if isinstance(x,dict))
            results[b['tool_use_id']]=(ln,str(r))
for ln,ts,i,n,inp in calls:
    out.write(json.dumps(dict(ln=ln,ts=ts,id=i,name=n,input=inp,result=results.get(i,(None,None))[1],rln=results.get(i,(None,None))[0]))+'\n')
