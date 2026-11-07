import type { HttpServices, RetentionRequest, SessionsRequest } from './http.js';
import type { ReceiptedDelivery } from './delivery.js';

export interface UnattendedAgent {
  generateUnattended(input: { text: string; runId: string; answerOnly?: boolean; signal?: AbortSignal }): Promise<{ text: string; usage?: unknown }>;
}
export interface SessionLifecycle {
  /** Implementations serialize rotation with active conversations before touching memory. */
  execute(request: SessionsRequest): Promise<unknown>;
}
export interface ServiceDependencies {
  delivery: ReceiptedDelivery;
  agent: UnattendedAgent;
  sessions: SessionLifecycle;
  allowedSkills: ReadonlySet<string>;
  health: () => Promise<unknown>;
  retention?: (request: RetentionRequest) => Promise<unknown>;
  activationEvidence?: HttpServices['activationEvidence'];
  activationState?: HttpServices['activationState'];
  activationAck?: HttpServices['activationAck'];
}

/** Bind receiver operations without giving /run a channel-posting capability. */
export function createHttpServices(dependencies: ServiceDependencies): HttpServices {
  return {
    send: request => dependencies.delivery.sendText(request),
    sendGallery: request => dependencies.delivery.sendGallery(request),
    sendGalleryBytes: request => dependencies.delivery.sendGalleryBytes(request),
    receipt: async dispatchId => dependencies.delivery.getReceipt(dispatchId),
    run: async (request, signal) => {
      if (request.skill && !dependencies.allowedSkills.has(request.skill)) throw new Error('Unrecognized skill');
      const text = request.skill ? `Run the ${request.skill} skill for this task.\n\n${request.prompt}` : request.prompt;
      return dependencies.agent.generateUnattended({ text, runId: request.env.SOTTO_DELIVERY_RUN_ID, ...(request.answer_only ? {answerOnly: true} : {}), signal });
    },
    sessions: request => dependencies.sessions.execute(request),
    retention: dependencies.retention,
    activationEvidence: dependencies.activationEvidence,
    activationState: dependencies.activationState,
    activationAck: dependencies.activationAck,
    health: dependencies.health,
  };
}
