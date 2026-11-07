/** Fixed content-free labels only; never an effect/retry classification. */
export const cleanupStages = ['transport', 'response', 'receiver_perform', 'receiver_serialize', 'writer'] as const;
export const cleanupCategories = ['network', 'aborted', 'http', 'invalid_response', 'unavailable', 'io', 'type', 'import', 'edit', 'other'] as const;
export interface CleanupDiagnostic {cleanup_stage: typeof cleanupStages[number]; cleanup_category: typeof cleanupCategories[number]; cleanup_http_status?: number;}
export function cleanupDiagnostic(value: unknown): CleanupDiagnostic | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const v=value as Record<string,unknown>;
  if (!cleanupStages.includes(v.cleanup_stage as never) || !cleanupCategories.includes(v.cleanup_category as never) ||
      Object.keys(v).some(k=>!['cleanup_stage','cleanup_category','cleanup_http_status'].includes(k)) ||
      v.cleanup_http_status!==undefined && (!Number.isInteger(v.cleanup_http_status)||Number(v.cleanup_http_status)<100||Number(v.cleanup_http_status)>599)) return undefined;
  return v as unknown as CleanupDiagnostic;
}
export class CleanupDiagnosticError extends Error {
  constructor(readonly diagnostic: CleanupDiagnostic) {super('Cleanup diagnostic: '+JSON.stringify(diagnostic));}
}
export function cleanupDiagnosticFromText(value: unknown): CleanupDiagnostic | undefined {
  if(typeof value!=='string'||value.length>512)return undefined;
  const prefix='Cleanup diagnostic: ';const start=value.indexOf(prefix);
  if(start<0)return undefined;
  try{return cleanupDiagnostic(JSON.parse(value.slice(start+prefix.length)));}catch{return undefined;}
}
