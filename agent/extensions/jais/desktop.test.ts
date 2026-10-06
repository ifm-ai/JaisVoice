import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import {
  appAccessMessage, appRefused, DesktopGrants, DesktopHelper, hostsIn, navigationAllowed, navigationMessage, tierProblem, type DesktopApp,
} from "./desktop.ts";

const mail: DesktopApp = { name: "Mail", bundle_id: "com.apple.mail", pid: 42, running: true, tier: "control" };
const safari: DesktopApp = { name: "Safari", bundle_id: "com.apple.Safari", pid: 43, running: true, tier: "read", note: "it uses your web accounts" };
const passwords: DesktopApp = { name: "Passwords", bundle_id: "com.apple.Passwords", running: true, tier: "blocked", note: "it manages passwords" };

/** A stand-in for `JAISVoice --desktop-helper`: echoes requests, fails on "boom", exits on "quit". */
function fakeHelper(): string {
  const path = join(mkdtempSync(join(tmpdir(), "jais-desktop-")), "helper.mjs");
  writeFileSync(path, `#!/usr/bin/env node
import { createInterface } from "node:readline";
createInterface({ input: process.stdin }).on("line", (line) => {
  const { id, method, params } = JSON.parse(line);
  if (method === "quit") process.exit(3);
  if (method === "slow") return;
  const answer = method === "boom" ? { id, error: "No such window." } : { id, result: { text: method + " " + JSON.stringify(params), argv: process.argv.slice(2) } };
  process.stdout.write(JSON.stringify(answer) + "\\n");
});
`);
  chmodSync(path, 0o755);
  return path;
}

test("the helper client correlates answers, reports errors and restarts after a crash", async () => {
  const helper = new DesktopHelper(fakeHelper());
  const [first, second] = await Promise.all([helper.request("snapshot", { pid: 42 }), helper.request("find", { text: "Inbox" })]);
  assert.equal(first.text, 'snapshot {"pid":42}');
  assert.equal(second.text, 'find {"text":"Inbox"}');
  assert.deepEqual(first.argv, ["--desktop-helper"]);
  await assert.rejects(helper.request("boom"), /No such window/);
  await assert.rejects(helper.request("quit"), /stopped/);
  assert.equal((await helper.request("status")).text, "status {}");
  helper.stop();
});

test("a slow or cancelled request fails without waiting forever", async () => {
  const helper = new DesktopHelper(fakeHelper());
  await assert.rejects(helper.request("slow", {}, undefined, 0.2), /did not answer/);
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(helper.request("slow", {}, controller.signal), /Cancelled/);
  helper.stop();
  await assert.rejects(new DesktopHelper("").request("status"), /unavailable/);
});

test("grants last for the request, per app and level; control includes reading", () => {
  const grants = new DesktopGrants();
  assert.equal(grants.has(mail, "read"), false);
  grants.grant(mail, "read");
  assert.equal(grants.has(mail, "read"), true);
  assert.equal(grants.has(mail, "control"), false);
  grants.grant(mail, "control");
  assert.equal(grants.has(mail, "read") && grants.has(mail, "control"), true);
  assert.equal(grants.has(safari, "read"), false);
  grants.reset();
  assert.equal(grants.has(mail, "read"), false);
});

test("protected apps are refused before any approval", () => {
  assert.equal(tierProblem(mail, "control"), undefined);
  assert.equal(tierProblem(safari, "read"), undefined);
  assert.match(tierProblem(safari, "control")!, /read-only.*web accounts.*Do not try another way/);
  assert.match(tierProblem(passwords, "read")!, /not accessible.*passwords/);
});

test("app approvals name the app and level for Jais Voice to word them", () => {
  assert.deepEqual(JSON.parse(appAccessMessage(mail, "control")), {
    reason: "Let Jais Voice control Mail until this request ends: click, type, use its menus and keys.",
    details: "Mail (com.apple.mail)", kind: "app_control", app: "Mail",
  });
  assert.equal(JSON.parse(appAccessMessage(mail, "read")).kind, "app_read");
  assert.match(appRefused("Mail", "control"), /did not allow Jais Voice to control Mail.*Do not retry/);
});

test("after reading windows, the browser goes without asking only to DuckDuckGo and sites the user named", () => {
  const hosts = hostsIn("Lis mon mail puis ouvre https://www.Louvre.fr/visite et cherche sur wikipedia.org");
  assert.deepEqual(hosts, ["louvre.fr", "wikipedia.org"]);
  assert.equal(navigationAllowed("https://duckduckgo.com/?q=horaires+louvre", hosts), true);
  assert.equal(navigationAllowed("https://html.duckduckgo.com/html?q=x", hosts), true);
  assert.equal(navigationAllowed("https://www.louvre.fr/en", hosts), true);
  assert.equal(navigationAllowed("https://fr.wikipedia.org/wiki/Louvre", hosts), true);
  assert.equal(navigationAllowed("https://evil.example/?mail=secret", hosts), false);
  assert.equal(navigationAllowed("https://louvre.fr.evil.example/", hosts), false);
  assert.equal(navigationAllowed("not a url", hosts), false);
  assert.deepEqual(JSON.parse(navigationMessage("https://evil.example/")).kind, "web_after_desktop");
});
