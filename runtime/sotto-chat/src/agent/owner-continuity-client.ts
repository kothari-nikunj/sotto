import {isGoogleReadFailureStage,isGoogleReadHttpStatus,isGoogleReadFailurePhase,googleReadPhaseMatches,type GoogleReadFailureStage,type GoogleReadFailureDetails} from './google-read-diagnostic.js';
/** Private receiver transport shared by bounded owner-scoped read projections. */
export interface OwnerContinuityClientOptions { bridgeUrl: string; readBearer: string; fetch?: typeof fetch }
const routes = ['owner-loops', 'owner-retune', 'brief-detail', 'google-read', 'knowledge-read'] as const;
export type OwnerContinuityRoute = typeof routes[number];

export class OwnerContinuityUnavailable extends Error {
  constructor(readonly googleReadStage?:GoogleReadFailureStage,readonly googleReadDetails:GoogleReadFailureDetails={}) { super('private owner continuity unavailable'); }
}

const MAX_BODY_BYTES = 64 * 1024;
const DEADLINE_MS = 5_000;

// One validated configuration only; no unbounded cache and no cached credentials.
let lastBridgeUrl: string | undefined;
let lastBridgeOrigin: string | undefined;
function ownerContinuityHref(options: OwnerContinuityClientOptions, route: OwnerContinuityRoute): string {
  if (!routes.includes(route) || !options.readBearer || /[\r\n]/.test(options.readBearer)) throw new Error('Owner continuity configuration is invalid');
  const bridgeUrl = options.bridgeUrl;
  if (bridgeUrl !== lastBridgeUrl || lastBridgeOrigin === undefined) {
    const url = new URL(bridgeUrl);
    if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) ||
        url.pathname !== '/mcp' || url.search || url.hash || url.username || url.password)
      throw new Error('Owner continuity configuration is invalid');
    lastBridgeUrl = bridgeUrl;
    lastBridgeOrigin = url.origin;
  }
  return `${lastBridgeOrigin}/runtime/${route}`;
}

export function ownerContinuityEndpoint(options: OwnerContinuityClientOptions, route: OwnerContinuityRoute): URL {
  return new URL(ownerContinuityHref(options, route));
}

function abortable<T>(promise: Promise<T>, signal: AbortSignal, onLateValue?: (value: T) => void): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let abandoned = false;
    const aborted = () => {
      abandoned = true;
      signal.removeEventListener('abort', aborted);
      reject(new Error('private owner continuity unavailable'));
    };
    signal.addEventListener('abort', aborted, { once: true });
    void promise.then(value => {
      if (abandoned) { try { onLateValue?.(value); } catch { /* late cleanup is best effort */ } return; }
      signal.removeEventListener('abort', aborted);
      resolve(value);
    }, () => {
      if (abandoned) return;
      signal.removeEventListener('abort', aborted);
      reject(new Error('private owner continuity unavailable'));
    });
    if (signal.aborted) aborted();
  });
}

/** Static routes only; credentials never follow a redirect or a model-selected URL. */
export async function readOwnerContinuityJson(options: OwnerContinuityClientOptions,
                                              route: OwnerContinuityRoute,
                                              body: { include_parked: boolean } | Record<string, never> |
                                                { artifact_id: string; offset: number; limit: number } |
                                                { latest: true; offset: number; limit: number } |
                                                { date?: string; query?: string; kind?: 'brief'|'prep'; offset: 0; limit: number } |
                                                { operation: string; arguments: Record<string, unknown>; source_revision: string } |
                                                { operation: 'master_file'; arguments: {argv: ['get']}; source_revision: null },
                                              callerSignal?: AbortSignal): Promise<unknown> {
  const url = ownerContinuityHref(options, route);
  const deadline = route === 'google-read' ? 30_000 : route === 'knowledge-read' ? ('operation' in body && ['granola_history', 'triage_queue'].includes(body.operation) ? 60_000 : 10_000) : DEADLINE_MS;
  const signal = callerSignal ? AbortSignal.any([callerSignal, AbortSignal.timeout(deadline)]) : AbortSignal.timeout(deadline);
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let response: Response | undefined;
  try {
    if (signal.aborted) throw new Error('private owner continuity unavailable');
    const pending = Promise.resolve().then(() => {
      if (signal.aborted) throw new Error('private owner continuity unavailable');
      return (options.fetch ?? fetch)(url, {
        method: 'POST', redirect: 'error', cache: 'no-store',
        headers: { authorization: `Bearer ${options.readBearer}`, 'content-type': 'application/json' },
        body: JSON.stringify(body), signal,
      });
    });
    response = await abortable(pending, signal, late => { void late.body?.cancel().catch(() => undefined); });
    if (route==='google-read' && response.status===503 &&
        (!response.url || response.url===url)) {
      // Only this authenticated Google error envelope has diagnostic metadata. Never inspect
      // provider responses, authentication failures, redirects or other private routes.
      let stage:GoogleReadFailureStage='unknown';let details:GoogleReadFailureDetails={};
      if(response.body&&Number(response.headers.get('content-length')??0)<=1024){
        reader=response.body.getReader();const chunks:Uint8Array[]=[];let bytes=0;
        try {
          while(true){
            const next=await abortable(reader.read(),signal);if(next.done)break;
            bytes+=next.value.byteLength;if(bytes>1024)throw new Error('diagnostic bound');
            chunks.push(next.value);
          }
          const value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(Buffer.concat(chunks)));
          if(value&&typeof value==='object'&&!Array.isArray(value)&&
              Object.keys(value).every(key=>['error','read_stage','read_http_status','read_phase'].includes(key))&&
              value.error==='Google read unavailable'&&isGoogleReadFailureStage(value.read_stage)&&
              (!Object.hasOwn(value,'read_http_status')||isGoogleReadHttpStatus(value.read_http_status))&&
              (!Object.hasOwn(value,'read_phase')||(isGoogleReadFailurePhase(value.read_phase)&&'operation' in body&&googleReadPhaseMatches(body.operation,value.read_phase)))){
            stage=value.read_stage;details={...(Object.hasOwn(value,'read_http_status')?{httpStatus:value.read_http_status}:{}),...(Object.hasOwn(value,'read_phase')?{phase:value.read_phase}:{})};
          }
        } catch {
          if(signal.aborted)throw new OwnerContinuityUnavailable();
          // Missing/invalid metadata on an observed503 means unknown, never source data.
        }
      }
      if(signal.aborted)throw new OwnerContinuityUnavailable();
      throw new OwnerContinuityUnavailable(stage,details);
    }
    if (response.status !== 200 || !response.body || (response.url && response.url !== url) ||
        Number(response.headers.get('content-length') ?? 0) > MAX_BODY_BYTES) throw new Error('private owner continuity unavailable');
    reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const next = await abortable(reader.read(), signal);
      if (signal.aborted) throw new Error('private owner continuity unavailable');
      if (next.done) break;
      size += next.value.byteLength;
      if (size > MAX_BODY_BYTES) throw new Error('private owner continuity unavailable');
      chunks.push(next.value);
    }
    if (signal.aborted) throw new Error('private owner continuity unavailable');
    return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
  } catch(error) {
    if(error instanceof OwnerContinuityUnavailable)throw error;
    throw new Error('private owner continuity unavailable');
  }
  finally {
    if (reader) {
      try { void reader.cancel().catch(() => undefined); } catch { /* already closed */ }
      try { reader.releaseLock(); } catch { /* an aborted read may still settle */ }
    } else if (response?.body) {
      try { void response.body.cancel().catch(() => undefined); } catch { /* already closed */ }
    }
  }
}
