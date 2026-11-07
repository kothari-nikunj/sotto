export type RunMode = 'chat' | 'unattended';

export interface ScriptInvocation {
  readonly id: string;
  readonly args: readonly string[];
  readonly meeting?: {readonly event_id: string; readonly start: string; readonly calendar_id?: string};
  readonly mode: RunMode;
  readonly runId?: string;
  readonly signal?: AbortSignal;
}

export interface ScriptRunnerResult {
  readonly stdout: string;
  readonly stderr: string;
  readonly exitCode: number;
}
