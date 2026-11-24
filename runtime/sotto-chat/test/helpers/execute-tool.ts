import { RequestContext } from '@mastra/core/request-context';

type FixtureContext = { requestContext: RequestContext; abortSignal?: AbortSignal };
type FixtureTool = { execute?: (input: never, context: FixtureContext) => Promise<unknown> };

// Fixture callers select tools by name and may intentionally pass invalid input.
export function executeTool(tool: FixtureTool, input: unknown, signal: AbortSignal | undefined,
  values: Record<string, unknown> | RequestContext) {
  const requestContext = values instanceof RequestContext ? values : new RequestContext();
  if (!(values instanceof RequestContext)) {
    for (const [key, value] of Object.entries(values)) requestContext.set(key, value);
  }
  if (!tool.execute) throw new Error('Fixture requires an executable tool');
  return tool.execute(input as never, { requestContext, abortSignal: signal });
}
