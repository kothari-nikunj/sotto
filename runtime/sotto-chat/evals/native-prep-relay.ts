/** Eval-only native relay: Node owns model credentials; Python stays localhost-only. */
import {createServer} from 'node:http';
import {randomBytes} from 'node:crypto';

export async function startNativePrepRelay(proxy:{origin:string;bearer:string;nativeMediaModelId:string}, fetcher:typeof fetch, synthetic=false, maxCalls=2) {
  if(!Number.isInteger(maxCalls)||maxCalls<1||maxCalls>24)throw new Error('Invalid native eval call limit');
  if(proxy.nativeMediaModelId!==(synthetic?'synthetic-scripted-provider':'gemini-3.8-flash'))throw new Error('Prep eval requires the approved Gemini 3.8 Flash model');
  const upstream=proxy.origin.replace(/\/$/,'')+'/native/v1beta/models/gemini-3.8-flash:generateContent';
  const bearer=randomBytes(32).toString('hex');
  const calls:Array<{status:number;elapsedMs:number;requestBytes:number;responseBytes:number}>=[];
  const refusals:string[]=[];
  const server=createServer(async(req,res)=>{
    try {
      if(req.url!=='/compose'||req.method!=='POST'||req.headers.authorization!==`Bearer ${bearer}`)throw new Error('invalid_native_relay_request');
      const chunks:Buffer[]=[];let bytes=0;
      for await(const chunk of req){bytes+=chunk.length;if(bytes>256_000)throw new Error('native_request_too_large');chunks.push(Buffer.from(chunk));}
      const raw=Buffer.concat(chunks),body=JSON.parse(raw.toString());
      if(!body||typeof body!=='object'||!Array.isArray(body.contents)||'tools' in body||'toolConfig' in body||
         Object.keys(body).some(key=>!['contents','generationConfig','systemInstruction','safetySettings'].includes(key)))throw new Error('native_tools_or_contract_refused');
      // Prep defaults to two attempts; chronological Golden stages share an explicit bounded cap.
      if(calls.length>=maxCalls)throw new Error('native_composition_attempt_limit');
      const call={status:0,elapsedMs:0,requestBytes:bytes,responseBytes:0};calls.push(call);
      const started=performance.now();
      try {
        const headers:Record<string,string>={'content-type':'application/json',authorization:`Bearer ${proxy.bearer}`};
        for(const name of ['x-sotto-workload','x-sotto-operation','x-sotto-parent-operation','x-sotto-run','x-sotto-job','x-sotto-attempt']){
          const value=req.headers[name];if(typeof value==='string')headers[name]=value;
        }
        const response=await fetcher(upstream,{method:'POST',body:raw,headers,redirect:'error',signal:AbortSignal.timeout(180_000)});
        call.status=response.status;
        const reader=response.body?.getReader(),output:Uint8Array[]=[];
        if(reader)while(true){const item=await reader.read();if(item.done)break;call.responseBytes+=item.value.length;
          if(call.responseBytes>1_000_000){await reader.cancel();throw new Error('native_response_too_large');}output.push(item.value);}
        res.writeHead(response.status,{'content-type':'application/json',
          ...(response.headers.get('x-sotto-outcome')?{'x-sotto-outcome':response.headers.get('x-sotto-outcome')!}:{})});
        res.end(Buffer.concat(output));
      } finally {call.elapsedMs=Math.round(performance.now()-started);}
    } catch(error) {
      const reason=error instanceof Error?error.message:'native_relay_failed';
      refusals.push(reason);if(!res.headersSent)res.writeHead(502,{'content-type':'application/json'});res.end(JSON.stringify({error:'Eval native relay refused or failed'}));
    }
  });
  await new Promise<void>((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  const address=server.address();if(!address||typeof address==='string')throw new Error('Native relay address unavailable');
  return {url:`http://127.0.0.1:${address.port}/compose`,bearer,calls,refusals,
    close:()=>new Promise<void>((resolve,reject)=>server.close(error=>error?reject(error):resolve()))};
}
