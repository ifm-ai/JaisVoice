/**
 * Self-healing tool calls. A model sometimes writes its native tool-call markup as plain text:
 * a doubled `<ifm|tool_calls>`, a tool name the API does not know, a missing closing tag. The
 * API then returns that text as the answer and the request ends without acting. The calls are
 * recovered from the text so they run: names are matched to the declared tools, arguments
 * follow their schemas, and a name that matches nothing still runs, so the model reads the
 * "not found" error and corrects itself.
 */
import { randomBytes } from "node:crypto";

/** What the repair needs from a declared tool: its name and parameter schema. */
export interface ToolSpec { name: string; parameters?: unknown }
export interface TextCall { name: string; arguments: Record<string, unknown> }
type Block = { type: string; text?: string; [key: string]: unknown };
type Assistant = { role: string; content: Block[]; stopReason?: string; [key: string]: unknown };

const MARKUP_START = /<ifm\|tool_calls?>/;
const CALL = /<ifm\|tool_call>([\s\S]*?)(?=<\/ifm\|tool_call>|<ifm\|tool_call>|<\/ifm\|tool_calls>|$)/g;
const ARGUMENT = /<ifm\|arg_key>([\s\S]*?)<\/ifm\|arg_key>\s*(?:<ifm\|arg_type>[\s\S]*?<\/ifm\|arg_type>\s*)?<ifm\|arg_value>([\s\S]*?)(?:<\/ifm\|arg_value>|$)/g;

/** The text before the markup and the calls written in it; undefined without any call. */
export function textToolCalls(text: string): { before: string; calls: TextCall[] } | undefined {
  const start = text.search(MARKUP_START);
  if (start < 0) return undefined;
  const calls: TextCall[] = [];
  for (const [, body] of text.slice(start).matchAll(CALL)) {
    const call = parseCall(body);
    if (call) calls.push(call);
  }
  return calls.length ? { before: text.slice(0, start).trimEnd(), calls } : undefined;
}

function parseCall(body: string): TextCall | undefined {
  const trimmed = body.trim();
  if (trimmed.startsWith("{")) {
    try {
      const call = JSON.parse(trimmed);
      const args = typeof call.arguments === "string" ? JSON.parse(call.arguments) : call.arguments ?? {};
      return typeof call.name === "string" && args && typeof args === "object" ? { name: call.name, arguments: args } : undefined;
    } catch {
      return undefined;
    }
  }
  const name = trimmed.split("<ifm|arg_key>")[0].trim();
  if (!/^[\w.-]+$/.test(name)) return undefined;
  const args: Record<string, unknown> = {};
  for (const [, key, value] of trimmed.matchAll(ARGUMENT)) args[key.trim()] = value;
  return { name, arguments: args };
}

/** The declared tool a written name means: exact, then `mcp__server__name`, then ignoring case. */
export function resolveToolName(name: string, tools: ToolSpec[]): string {
  const names = tools.map((tool) => tool.name);
  if (names.includes(name)) return name;
  const suffixed = names.filter((candidate) => candidate.endsWith("__" + name));
  if (suffixed.length === 1) return suffixed[0];
  const folded = names.filter((candidate) => candidate.toLowerCase() === name.toLowerCase());
  return folded.length === 1 ? folded[0] : name;
}

/** Text arguments become numbers, booleans, arrays or objects where the schema says so. */
export function typedArguments(args: Record<string, unknown>, parameters: unknown): Record<string, unknown> {
  const properties = (parameters as { properties?: Record<string, Record<string, unknown>> })?.properties ?? {};
  return Object.fromEntries(Object.entries(args).map(([key, value]) => {
    if (typeof value !== "string" || isStringSchema(properties[key])) return [key, value];
    try {
      return [key, JSON.parse(value)];
    } catch {
      return [key, value];
    }
  }));
}

function isStringSchema(schema: Record<string, unknown> | undefined): boolean {
  if (!schema) return false;
  const branches = (schema.anyOf ?? schema.oneOf) as Record<string, unknown>[] | undefined;
  if (branches?.length) return branches.every((branch) => ["string", "null"].includes(String(branch.type)));
  return schema.type === "string" || (Array.isArray(schema.type) && schema.type.every((kind) => ["string", "null"].includes(kind)));
}

/**
 * The assistant message with the calls written as text turned into tool calls, or undefined
 * when there is nothing to repair. `alias` may redirect a call to another tool first.
 */
export function repairToolCalls(message: Assistant, tools: ToolSpec[], alias?: (call: TextCall) => TextCall | undefined): Assistant | undefined {
  if (message.role !== "assistant" || !Array.isArray(message.content)) return undefined;
  if (message.content.some((block) => block.type === "toolCall")) return undefined;
  const content: Block[] = [];
  const calls: TextCall[] = [];
  for (const block of message.content) {
    const found = block.type === "text" && typeof block.text === "string" ? textToolCalls(block.text) : undefined;
    if (!found) {
      content.push(block);
      continue;
    }
    if (found.before) content.push({ ...block, text: found.before });
    calls.push(...found.calls);
  }
  if (!calls.length) return undefined;
  for (const written of calls) {
    const call = alias?.(written) ?? written;
    const name = resolveToolName(call.name, tools);
    const spec = tools.find((tool) => tool.name === name);
    content.push({ type: "toolCall", id: "call_" + randomBytes(10).toString("hex"), name,
      arguments: typedArguments(call.arguments, spec?.parameters) });
  }
  return { ...message, content, stopReason: "toolUse" };
}
