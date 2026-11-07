/** Closed content-free failure vocabulary shared by transport and health projection. */
import contract from '../tools/google-read-contract.json' with {type: 'json'};
export type GoogleReadFailureStage = keyof typeof contract.stages;
export const GOOGLE_READ_FAILURE_STAGES = Object.keys(contract.stages) as readonly GoogleReadFailureStage[];
export const isGoogleReadFailureStage = (value:unknown):value is GoogleReadFailureStage =>
  typeof value==='string' && Object.hasOwn(contract.stages,value);
export type GoogleReadFailurePhase = keyof typeof contract.phases;
export const GOOGLE_READ_FAILURE_PHASES = Object.keys(contract.phases) as readonly GoogleReadFailurePhase[];
export const isGoogleReadHttpStatus=(value:unknown):value is number=>
  typeof value==='number'&&Number.isInteger(value)&&value>=contract.httpStatus.minimum&&value<=contract.httpStatus.maximum;
export const isGoogleReadFailurePhase=(value:unknown):value is GoogleReadFailurePhase=>
  typeof value==='string'&&Object.hasOwn(contract.phases,value);
export const googleReadPhaseMatches=(tool:unknown,phase:unknown)=>
  isGoogleReadFailurePhase(phase)&&contract.phases[phase]===tool;
export interface GoogleReadFailureDetails {httpStatus?:number;phase?:GoogleReadFailurePhase}
const MESSAGE='Google read is unavailable or source permission changed; no result can be used.';
export const googleReadFailureMessage=(stage:GoogleReadFailureStage,details:GoogleReadFailureDetails={})=>
  `${MESSAGE} [sotto-google-read-stage=${stage}${isGoogleReadHttpStatus(details.httpStatus)?`;http_status=${details.httpStatus}`:''}${isGoogleReadFailurePhase(details.phase)?`;phase=${details.phase}`:''}]`;
export function googleReadDiagnosticFromErrorText(value:unknown):({stage:GoogleReadFailureStage}&GoogleReadFailureDetails)|undefined {
  if(typeof value!=='string'||value.length>256)return undefined;
  const match=/^Google read is unavailable or source permission changed; no result can be used\. \[sotto-google-read-stage=([a-z_]+)(?:;http_status=([0-9]{3}))?(?:;phase=([a-z_]+))?\]$/.exec(value);
  if(!match||!isGoogleReadFailureStage(match[1]))return undefined;
  const httpStatus=match[2]===undefined?undefined:Number(match[2]),phase=match[3];
  if(httpStatus!==undefined&&!isGoogleReadHttpStatus(httpStatus)||phase!==undefined&&!isGoogleReadFailurePhase(phase))return undefined;
  const details={stage:match[1],...(httpStatus===undefined?{}:{httpStatus}),...(phase===undefined?{}:{phase:phase as GoogleReadFailurePhase})};
  return value===googleReadFailureMessage(details.stage,details)?details:undefined;
}
export const googleReadStageFromErrorText=(value:unknown)=>googleReadDiagnosticFromErrorText(value)?.stage;
