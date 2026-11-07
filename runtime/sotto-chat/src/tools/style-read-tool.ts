import {createTool} from '@mastra/core/tools';
import {styleRequestSchema, type createKnowledgeCommand} from './knowledge-command.js';

/** Typed model interface to the existing closed read; no new backend or authority. */
export function createStyleReadTool(command: ReturnType<typeof createKnowledgeCommand>) {
  return createTool({
    id: 'read_writing_style',
    description: 'Read permitted writing-style guidance for this recipient and channel before drafting. Pass an object, not encoded JSON. Reuse guidance for edits to the same draft. Does not save or send anything.',
    inputSchema: styleRequestSchema,
    execute: async (input, {requestContext, abortSignal}) => {
      const request = styleRequestSchema.parse(input);
      return command({id: 'style-apply', args: [JSON.stringify(request)], mode: 'chat'}, {
        mode: requestContext?.get('mode'), runId: requestContext?.get('runId'),
        ownerRequestKey: requestContext?.get('ownerRequestKey'),
        sourceRevision: requestContext?.get('sourceRevision'), signal: abortSignal,
      });
    },
  });
}
