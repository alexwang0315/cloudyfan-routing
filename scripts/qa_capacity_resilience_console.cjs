'use strict';
// Private bounded evidence and a strict verdict for directly verified planned-outage reads.
const fs = require('node:fs/promises');
const path = require('node:path');

function consoleVerdict(events, observation, rawConsole) {
  const httpStatus=/^Failed to load resource: the server responded with a status of (401|403|404|409|410|429|503) \(/;
  const codes=['net::ERR_EMPTY_RESPONSE','net::ERR_CONNECTION_REFUSED'];
  const safe=row=>row?.same_fixture_origin===true&&
    ['/monitor/snapshot','/monitor/queue','/monitor/capacity'].includes(row.path)&&
    row.query_present===false&&row.fragment_present===false&&row.userinfo_present===false;
  const rejected=[],accepted=[],originalHttp=[];
  const reject=(source,reason,fields={})=>rejected.push({source,reason,...fields});
  const rows=type=>events.filter(row=>row.type===type);
  const ack=rows('planned_crash_ack'),exit=rows('old_process_exit'),ready=rows('new_ready');
  const window=ack.length===1&&exit.length===1&&ready.length===1?{
    ack_sequence:ack[0].sequence,exit_sequence:exit[0].sequence,ready_sequence:ready[0].sequence,
    start_ms:ack[0].at_ms,end_ms:ready[0].at_ms,maximum_ms:20000,
    old_pid:ack[0].pid,new_pid:ready[0].pid}:null;
  const windowValid=window!==null&&Number.isInteger(ack[0].pid)&&ack[0].pid>0&&
    Number.isInteger(ready[0].pid)&&ready[0].pid>0&&ack[0].ack_valid&&ack[0].pid===ack[0].expected_old_pid&&
    ack[0].intentional_exit===86&&exit[0].pid===ack[0].pid&&exit[0].valid&&exit[0].code===86&&
    !exit[0].signal_present&&!exit[0].timed_out&&!exit[0].cleanup_failed&&
    ready[0].same_origin&&ready[0].same_port&&ready[0].pid!==ack[0].pid&&
    window.ack_sequence<window.exit_sequence&&window.exit_sequence<window.ready_sequence&&
    window.end_ms>window.start_ms&&window.end_ms-window.start_ms<=window.maximum_ms;
  const inside=row=>windowValid&&row.at_ms>=window.start_ms&&row.at_ms<window.end_ms&&
    row.sequence>window.ack_sequence&&row.sequence<window.ready_sequence&&row.step===ack[0].step;
  if(!observation.complete)reject('observation','incomplete_telemetry');
  const consoles=rows('playwright_console');
  if(consoles.length!==rawConsole.length)reject('observation','raw_console_count_mismatch');
  const networkLogs=rows('cdp_log_error').filter(row=>row.message_kind==='network'||
    (row.source==='network'&&row.message_kind!=='http'));
  const candidates=[];
  for(const log of networkLogs){
    const id=log.request_id,starts=rows('cdp_request_start').filter(row=>row.request_id===id),
      failed=rows('cdp_loading_failed').filter(row=>row.request_id===id),
      logs=networkLogs.filter(row=>row.request_id===id);
    let reason;
    if(!windowValid)reason='unverified_planned_outage';
    else if(id===null||log.request_id_missing)reason='missing_direct_request_id';
    else if(starts.length!==1||failed.length!==1||logs.length!==1)reason='missing_or_ambiguous_direct_chain';
    else if(log.source!=='network'||!codes.includes(log.error_code)||failed[0].error_code!==log.error_code)
      reason='unapproved_network_error';
    else if(!safe(starts[0])||!safe(log)||starts[0].path!==log.path)reason='unsafe_observation_url';
    else if(starts[0].method!=='GET')reason='observation_method_not_GET';
    else if(starts[0].request_id_reused||starts[0].redirect_present)reason='reused_or_redirected_request_id';
    else if(failed[0].canceled||failed[0].blocked)reason='canceled_or_blocked_request';
    else if(starts[0].sequence>=failed[0].sequence||starts[0].sequence>=log.sequence)
      reason='invalid_request_event_order';
    else if(!inside(failed[0])||!inside(log))reason='failure_outside_verified_window';
    if(reason)reject('cdp_network_log',reason,{request_id:id,sequence:log.sequence});
    else candidates.push({id,path:log.path,error_code:log.error_code,start:starts[0],failure:failed[0],log});
  }
  for(const failed of rows('cdp_loading_failed').filter(row=>codes.includes(row.error_code))){
    if(!networkLogs.some(log=>log.request_id!==null&&log.request_id===failed.request_id))
      reject('cdp_loading_failed','missing_corresponding_network_log',{request_id:failed.request_id,sequence:failed.sequence});
  }
  const eligibleConsole=[];
  for(let i=0;i<rawConsole.length;i++){
    const value=rawConsole[i],row=consoles[i];
    if(httpStatus.test(value)){
      if(row?.message_kind==='http'&&row.status===Number(/status of ([0-9]{3})/.exec(value)[1]))originalHttp.push(i);
      else reject('playwright_console','raw_and_safe_console_mismatch',{console_index:i});
      continue;
    }
    let reason;
    if(!row||row.message_kind!=='network'||!codes.includes(row.error_code)||
      value!=='Failed to load resource: '+row.error_code)reason='unapproved_console_error';
    else if(row.argument_count!==0)reason='console_API_arguments_not_network_log';
    else if(!safe(row))reason='unsafe_console_location';
    else if(!inside(row))reason='console_outside_verified_window';
    if(reason)reject('playwright_console',reason,{console_index:i,sequence:row?.sequence});
    else eligibleConsole.push({index:i,row});
  }
  const signature=row=>row.error_code+'|'+row.path;
  for(const candidate of candidates){
    const peers=candidates.filter(row=>signature(row)===signature(candidate));
    const matches=eligibleConsole.filter(row=>signature(row.row)===signature(candidate));
    if(peers.length!==1||matches.length!==1){
      reject('correspondence','missing_or_ambiguous_console_log_accounting',{request_id:candidate.id});
      continue;
    }
    accepted.push({console_index:matches[0].index,request_id:candidate.id,path:candidate.path,
      error_code:candidate.error_code,start_sequence:candidate.start.sequence,
      failure_sequence:candidate.failure.sequence,network_log_sequence:candidate.log.sequence,
      console_sequence:matches[0].row.sequence});
  }
  for(const value of eligibleConsole){
    if(!accepted.some(row=>row.console_index===value.index))
      reject('playwright_console','no_unique_verified_network_log',{console_index:value.index,sequence:value.row.sequence});
  }
  return {schema_version:1,passed:rejected.length===0,original_http_status_indexes:originalHttp,
    accepted_planned_observations:accepted,unaccepted:rejected,
    verified_window:windowValid?window:null,telemetry_complete:observation.complete,
    scope:'Only directly evidenced observation failures within this planned outage; no retrospective cause or SLO'};
}

async function observeBrowserConsole({page, output, origin, step, boot}) {
  const filename='actual-browser-console-observations.jsonl';
  const file=await fs.open(path.join(output,filename),'wx',0o600);
  const started=performance.now(),limits={maximum_events:4096,maximum_bytes:1048576,
    maximum_pending_requests:128,ack_to_ready_maximum_ms:20000};
  const pending=new Map(),seen=new Set();
  let count=0,bytes=0,writes=Promise.resolve(),problem=null,stopping=false,window=null,finished;
  const now=()=>performance.now()-started;
  const fail=kind=>{problem ||= kind;};
  const requestId=value=>typeof value==='string'&&/^[A-Za-z0-9._:-]{1,80}$/.test(value)?value:null;
  const errorCode=value=>['net::ERR_EMPTY_RESPONSE','net::ERR_CONNECTION_REFUSED'].includes(value)?value:'other';
  const message=value=>{
    const network=/^Failed to load resource: (net::ERR_[A-Z_]+)$/.exec(value);
    if(network)return {message_kind:'network',error_code:errorCode(network[1])};
    const http=/^Failed to load resource: the server responded with a status of ([0-9]{3}) \(/.exec(value);
    return http?{message_kind:'http',status:Number(http[1])}:{message_kind:'unrecognized'};
  };
  const url=raw=>{
    try {
      const value=new URL(raw);
      const safe=['/monitor/snapshot','/monitor/queue','/monitor/capacity'];
      return {same_fixture_origin:value.origin===origin,
        path:safe.includes(value.pathname)?value.pathname:'other',
        query_present:value.search!=='',fragment_present:value.hash!=='',
        userinfo_present:value.username!==''||value.password!==''};
    } catch {return {same_fixture_origin:false,path:'unknown',url_invalid:true};}
  };
  const method=value=>['GET','POST'].includes(value)?value:'OTHER';
  const record=(type,fields={})=>{
    if(stopping||problem)return;
    const row={sequence:count+1,at_ms:now(),step:step(),boot:boot(),type,...fields};
    const line=JSON.stringify(row)+'\n',size=Buffer.byteLength(line);
    if(count>=limits.maximum_events||bytes+size>limits.maximum_bytes){fail('observation_bound_reached');return;}
    count++;bytes+=size;
    writes=writes.then(()=>file.writeFile(line)).catch(()=>fail('observation_write_failed'));
  };
  let session;
  const consoleEvent=item=>{
    if(item.type()==='error')record('playwright_console',{...message(item.text()),...url(item.location().url),
      argument_count:item.args().length});
  };
  const failedEvent=request=>record('playwright_requestfailed',{...url(request.url()),method:method(request.method()),
    error_code:errorCode(request.failure()?.errorText),direct_cdp_id_available:false});
  try {
    session=await page.context().newCDPSession(page);
    session.on('Network.requestWillBeSent',event=>{
      if(stopping||problem)return;
      const id=requestId(event.requestId),projection={...url(event.request.url),method:method(event.request.method)};
      const reused=id!==null&&seen.has(id);
      if(id!==null){
        seen.add(id);pending.set(id,projection);
        if(pending.size>limits.maximum_pending_requests){fail('pending_request_bound_reached');return;}
      }
      record('cdp_request_start',{request_id:id,request_id_missing:id===null,
        request_id_reused:reused,redirect_present:event.redirectResponse!==undefined,...projection});
    });
    session.on('Network.loadingFailed',event=>{
      const id=requestId(event.requestId);
      record('cdp_loading_failed',{request_id:id,request_id_missing:id===null,
        error_code:errorCode(event.errorText),canceled:event.canceled===true,
        blocked:event.blockedReason!==undefined||event.corsErrorStatus!==undefined});
      if(id!==null)pending.delete(id);
    });
    session.on('Network.loadingFinished',event=>{
      const id=requestId(event.requestId);
      record('cdp_loading_finished',{request_id:id,request_id_missing:id===null});
      if(id!==null)pending.delete(id);
    });
    session.on('Network.responseReceived',event=>{
      const id=requestId(event.requestId),projection=pending.get(id);
      const status=Number.isInteger(event.response.status)?event.response.status:null;
      record('cdp_response',{request_id:id,request_id_missing:id===null,status});
      if(window?.old_exit_valid&&window.first_response_after_exit_at_ms===null&&projection?.same_fixture_origin){
        window.first_response_after_exit_at_ms=now();
        record('first_same_origin_response_after_old_exit',{request_id:id,status});
      }
    });
    session.on('Log.entryAdded',({entry})=>{
      if(entry.level!=='error')return;
      const id=requestId(entry.networkRequestId);
      record('cdp_log_error',{source:['network','javascript'].includes(entry.source)?entry.source:'other',
        request_id:id,request_id_missing:id===null,...message(entry.text),...url(entry.url)});
    });
    session.on('close',()=>{if(!stopping)fail('cdp_closed_before_observation_end');});
    page.on('console',consoleEvent);page.on('requestfailed',failedEvent);
    await session.send('Network.enable');await session.send('Log.enable');
    record('observation_started',{limits,classification:'bounded_capture_for_strict_planned_outage_verdict'});
  } catch(error) {
    stopping=true;page.off('console',consoleEvent);page.off('requestfailed',failedEvent);
    if(session)await session.detach().catch(()=>{});
    await writes;await file.close();throw error;
  }
  return {
    ready({pid,initial=false,sameOrigin=true,samePort=true}) {
      record(initial?'initial_ready':'new_ready',{pid,same_origin:sameOrigin,same_port:samePort});
      if(window&&!initial){
        if(window.new_ready_at_ms!==undefined){window.duplicate_ready=true;return;}
        window.new_ready_at_ms=now();window.new_pid=pid;window.same_origin=sameOrigin;
        window.new_pid_distinct=pid!==window.old_pid;
        window.ack_to_ready_ms=window.new_ready_at_ms-window.ack_at_ms;
        window.bound_reached=window.ack_to_ready_ms>limits.ack_to_ready_maximum_ms;
      }
    },
    plannedAck({expectedOldPid,pid,intentionalExit}) {
      if(window){fail('multiple_planned_acknowledgements');return;}
      window={old_pid:expectedOldPid,ack_pid:pid,intentional_exit:intentionalExit,ack_at_ms:now(),
        ack_valid:pid===expectedOldPid&&intentionalExit===86,old_exit_valid:false,
        first_response_after_exit_at_ms:null};
      record('planned_crash_ack',{expected_old_pid:expectedOldPid,pid,intentional_exit:intentionalExit,
        ack_valid:window.ack_valid});
    },
    oldExit(value) {
      const valid=value.code===86&&value.signal===null&&!value.timed_out&&!value.cleanup_failed;
      if(window){window.old_exit_at_ms=now();window.old_exit_valid=valid;}
      record('old_process_exit',{pid:window?.old_pid??null,code:value.code,signal_present:value.signal!==null,
        timed_out:value.timed_out,cleanup_failed:value.cleanup_failed,valid});
    },
    normalStop(value,pid) {record('diagnostic_normal_stop',{pid,code:value.code,
      signal_present:value.signal!==null,timed_out:value.timed_out,cleanup_failed:value.cleanup_failed});},
    diagnosticRead(value) {record('diagnostic_stopped_service_observation',{path:'/monitor/queue',method:method(value.method),
      kind:['response','transport_failed'].includes(value.kind)?value.kind:'other',
      ...(Number.isInteger(value.status)?{status:value.status}:{})});},
    finish() {
      if(finished)return finished;
      record('observation_ended');stopping=true;
      page.off('console',consoleEvent);page.off('requestfailed',failedEvent);
      finished=(async()=>{
        await session.detach().catch(()=>fail('cdp_detach_failed'));
        await writes;await file.close().catch(()=>fail('observation_close_failed'));
        if(window)window.control_complete=window.ack_valid&&window.old_exit_valid&&
          window.new_ready_at_ms!==undefined&&window.same_origin&&window.new_pid_distinct&&!window.bound_reached&&!window.duplicate_ready;
        return {schema_version:1,path:filename,complete:problem===null,problem,
          event_count:count,bytes,limits,planned_outage:window,
          clock:'Node performance.now relative to this recorder; event receipt observations only',
          scope:'Safe CDP IDs and lifecycle markers; final verdict requires a complete direct chain'};
      })();
      return finished;
    },
    async verdict(rawConsole) {
      const observation=await this.finish();
      let events=[];
      try {
        const buffer=await fs.readFile(path.join(output,filename));
        events=buffer.toString('utf8').trimEnd().split('\n').map(line=>JSON.parse(line));
        if(buffer.length!==observation.bytes||events.length!==observation.event_count||
          events.some((row,i)=>row.sequence!==i+1||!Number.isFinite(row.at_ms)||
            (i>0&&row.at_ms<events[i-1].at_ms))||events.at(-1)?.type!=='observation_ended')
          observation.complete=false;
      } catch {observation.complete=false;}
      return {observation,verdict:consoleVerdict(events,observation,rawConsole)};
    }
  };
}

module.exports={observeBrowserConsole};
