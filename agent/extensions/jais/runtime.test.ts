import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { commandProblem, compactContext, inlineSnapshots, noteCommand, runShell, shellQuote, type Message } from "./runtime.ts";

const alive = (pid: number) => { try { process.kill(pid, 0); return true; } catch { return false; } };

test("commands are validated before execution", () => {
  assert.equal(commandProblem("echo ok", "Afficher ok"), undefined);
  assert.match(commandProblem("  ", "x")!, /non-empty/);
  assert.match(commandProblem("x".repeat(8001), "x")!, /8000/);
  assert.match(commandProblem("echo ‮evil", "x")!, /invisible/);
  assert.match(commandProblem("echo \u0007", "x")!, /invisible/);
  assert.match(commandProblem("echo ok", "")!, /reason/);
});

test("shell output, exit code and minimal environment", async () => {
  const ok = await runShell("echo \"$PATH\"; echo erreur >&2");
  assert.deepEqual([ok.exit_code, ok.error, ok.timed_out], [0, null, false]);
  assert.match(ok.output, /^\/usr\/bin:\/bin:\/usr\/sbin:\/sbin\nerreur\n$/);
  const failed = await runShell("exit 3");
  assert.equal(failed.exit_code, 3);
  assert.match(failed.error!, /code 3/);
});

test("output is bounded", async () => {
  const result = await runShell("yes | head -c 20000");
  assert.ok(Buffer.byteLength(result.output) < 8300);
  assert.match(result.output, /limited to 8 KB/);
});

test("timeout stops the whole process group", async () => {
  const started = Date.now();
  const result = await runShell("sleep 30 & echo $!; wait", undefined, 1);
  assert.equal(result.timed_out, true);
  assert.equal(result.exit_code, null);
  assert.ok(Date.now() - started < 4000);
  const child = Number(result.output.trim());
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.equal(alive(child), false);
});

test("cancellation stops the command and background work left behind", async () => {
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 300);
  const cancelled = await runShell("sleep 30", controller.signal);
  assert.match(cancelled.error!, /cancelled/);
  const leftover = await runShell("sleep 30 >/dev/null 2>&1 & echo $!");
  assert.equal(leftover.exit_code, 0);
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.equal(alive(Number(leftover.output.trim())), false);
});

test("note text stays in argv and is HTML-escaped", () => {
  const command = noteCommand("Courses", "pain & <lait>\n'œufs'");
  assert.ok(command.startsWith("/usr/bin/osascript - 'Courses' "));
  assert.ok(command.includes("<div>pain &amp; &lt;lait&gt;<br>&#x27;œufs&#x27;</div>"));
  assert.ok(command.endsWith("end run\nHORIZON_NOTES_APPLESCRIPT"));
  assert.throws(() => noteCommand("", "x"), /title/);
  assert.throws(() => noteCommand("t", "x".repeat(4001)), /4000/);
  assert.equal(execFileSync("/bin/sh", ["-c", "printf %s " + shellQuote("a'b $HOME")]).toString(), "a'b $HOME");
});

test("only snapshot files from this session's output directory are inlined", () => {
  const root = mkdtempSync(join(tmpdir(), "jais-snapshot-"));
  const output = join(root, "out");
  mkdirSync(output);
  writeFileSync(join(output, "page-1.yml"), "- button \"OK\" [ref=e1]");
  writeFileSync(join(root, "page-2.yml"), "secret");
  symlinkSync(join(root, "page-2.yml"), join(output, "page-3.yml"));
  const inlined = inlineSnapshots("### Page\n### Snapshot\n- [Snapshot](out/page-1.yml)", output, [root]);
  assert.match(inlined, /```yaml\n- button "OK" \[ref=e1\]\n```/);
  for (const link of ["page-2.yml", "out/page-3.yml", "../etc/passwd"]) {
    const refused = inlineSnapshots(`### Snapshot\n- [Snapshot](${link})`, output, [root]);
    assert.doesNotMatch(refused, /secret/);
    assert.match(refused, /could not be read/);
  }
});

test("context keeps recent dialogue and shortens stale tool output in a copy", () => {
  const messages: Message[] = [];
  for (let turn = 0; turn < 10; turn++) {
    messages.push({ role: "user", content: `question ${turn}` });
    messages.push({ role: "assistant", content: [{ type: "text", text: "a".repeat(2000) }] });
    messages.push({ role: "toolResult", content: [{ type: "text", text: "p".repeat(20000) }] });
  }
  const compacted = compactContext(messages);
  assert.equal(compacted.filter((message) => message.role === "user").length, 8);
  assert.equal(compacted[0].content, "question 2");
  const results = compacted.filter((message) => message.role === "toolResult").map((message) => message.content[0].text.length);
  assert.ok(results.slice(0, -1).every((length) => length < 800));
  assert.ok(results.at(-1)! > 10000 && results.at(-1)! < 10200);
  assert.ok(compacted.filter((message) => message.role === "assistant").every((message) => message.content[0].text.length <= 1601));
  assert.equal(messages[2].content[0].text.length, 20000);
});
