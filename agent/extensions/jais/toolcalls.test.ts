import assert from "node:assert/strict";
import { test } from "node:test";
import { repairToolCalls, resolveToolName, textToolCalls, typedArguments } from "./toolcalls.ts";

const tools = [
  { name: "shell_execute", parameters: { properties: { command: { type: "string" }, reason: { type: "string" } } } },
  { name: "mcp__browser__browser_navigate", parameters: { properties: { url: { type: "string" } } } },
  { name: "mcp__browser__browser_wait_for", parameters: { properties: { time: { type: "number" } } } },
  { name: "read", parameters: { properties: { path: { type: "string" }, offset: { type: "number" } } } },
];
const call = (name: string, args: Record<string, string>) => `<ifm|tool_call>${name}\n`
  + Object.entries(args).map(([key, value]) => `<ifm|arg_key>${key}</ifm|arg_key>\n<ifm|arg_value>${value}</ifm|arg_value>\n`).join("")
  + "</ifm|tool_call>\n";

test("calls written as text are read, even with a doubled or missing wrapper", () => {
  const script = "osascript <<'EOF'\ntell application \"Keynote\"\nend tell\nEOF";
  const doubled = "**Slide 6**<ifm|tool_calls>\n<ifm|tool_calls>\n" + call("shell_execute", { command: script, reason: "Add slide 6" }) + "</ifm|tool_calls>";
  assert.deepEqual(textToolCalls(doubled), { before: "**Slide 6**", calls: [{ name: "shell_execute", arguments: { command: script, reason: "Add slide 6" } }] });
  const unclosed = "Go.<ifm|tool_calls>\n<ifm|tool_call>read\n<ifm|arg_key>path</ifm|arg_key>\n<ifm|arg_value>/a/SKILL.md";
  assert.deepEqual(textToolCalls(unclosed)?.calls, [{ name: "read", arguments: { path: "/a/SKILL.md" } }]);
  const json = '<ifm|tool_calls><ifm|tool_call>{"name": "read", "arguments": {"path": "/x"}}</ifm|tool_call></ifm|tool_calls>';
  assert.deepEqual(textToolCalls(json)?.calls, [{ name: "read", arguments: { path: "/x" } }]);
  assert.equal(textToolCalls("A plain answer."), undefined);
  assert.equal(textToolCalls("Mentions <ifm|tool_calls> but writes no call."), undefined);
});

test("written names match the declared tools", () => {
  assert.equal(resolveToolName("shell_execute", tools), "shell_execute");
  assert.equal(resolveToolName("browser_navigate", tools), "mcp__browser__browser_navigate");
  assert.equal(resolveToolName("Read", tools), "read");
  assert.equal(resolveToolName("skill", tools), "skill");
});

test("arguments follow the tool's schema", () => {
  assert.deepEqual(typedArguments({ time: "2", path: "3" }, { properties: { time: { type: "number" }, path: { type: "string" } } }),
    { time: 2, path: "3" });
  assert.deepEqual(typedArguments({ time: "soon" }, { properties: { time: { type: "number" } } }), { time: "soon" });
});

test("a message whose calls are text becomes a tool-use message", () => {
  const message = {
    role: "assistant", stopReason: "stop",
    content: [{ type: "thinking", thinking: "plan" }, { type: "text", text: "Opening it.<ifm|tool_calls>\n" + call("browser_navigate", { url: "https://x.org" })
      + call("browser_wait_for", { time: "2" }) + "</ifm|tool_calls>" }],
  };
  const repaired = repairToolCalls(message, tools)!;
  assert.equal(repaired.stopReason, "toolUse");
  assert.deepEqual(repaired.content.slice(0, 2), [{ type: "thinking", thinking: "plan" }, { type: "text", text: "Opening it." }]);
  assert.deepEqual(repaired.content.slice(2).map(({ name, arguments: args, type }) => ({ type, name, args })), [
    { type: "toolCall", name: "mcp__browser__browser_navigate", args: { url: "https://x.org" } },
    { type: "toolCall", name: "mcp__browser__browser_wait_for", args: { time: 2 } },
  ]);
  assert.match(String(repaired.content[2].id), /^call_[0-9a-f]{20}$/);
  assert.notEqual(repaired.content[2].id, repaired.content[3].id);
});

test("an alias redirects a call; real tool calls and plain answers are left alone", () => {
  const skill = { role: "assistant", content: [{ type: "text", text: call("skill", { name: "presentations" }) }] };
  const alias = ({ name, arguments: args }: { name: string; arguments: Record<string, unknown> }) =>
    name === "skill" ? { name: "read", arguments: { path: `/skills/${args.name}/SKILL.md` } } : undefined;
  assert.deepEqual(repairToolCalls(skill, tools, alias)!.content.map(({ name, arguments: args }) => ({ name, args })),
    [{ name: "read", args: { path: "/skills/presentations/SKILL.md" } }]);
  const real = { role: "assistant", content: [{ type: "text", text: "<ifm|tool_calls>" }, { type: "toolCall", id: "1", name: "read", arguments: {} }] };
  assert.equal(repairToolCalls(real, tools), undefined);
  assert.equal(repairToolCalls({ role: "assistant", content: [{ type: "text", text: "Done." }] }, tools), undefined);
  assert.equal(repairToolCalls({ role: "user", content: [{ type: "text", text: call("read", { path: "/x" }) }] }, tools), undefined);
});
