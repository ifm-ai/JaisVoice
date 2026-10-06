import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, realpathSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileWriteMessage, readProblem, resolveToolPath, skillFor, skillInstructions, toolPath, writeProblem } from "./files.ts";

const home = realpathSync(mkdtempSync(join(tmpdir(), "jais-home-")));
const app = join(home, "Workspace/JAISVoice");
mkdirSync(join(home, "Documents"), { recursive: true });
mkdirSync(join(home, ".ssh"), { recursive: true });
mkdirSync(app, { recursive: true });

test("paths expand ~, resolve against the working directory and follow symlinks", () => {
  assert.equal(resolveToolPath("~/Documents/list.txt", "/", home), join(home, "Documents/list.txt"));
  assert.equal(resolveToolPath("Documents", home, home), join(home, "Documents"));
  assert.equal(toolPath({}, home, home), home);
  symlinkSync(join(home, ".ssh"), join(home, "Documents/keys"));
  assert.equal(resolveToolPath("~/Documents/keys/id_ed25519", "/", home), join(home, ".ssh/id_ed25519"));
});

test("credentials are never read; other files can be, once approved", () => {
  assert.match(readProblem(join(home, ".ssh/id_ed25519"), home)!, /credentials/);
  assert.match(readProblem(join(home, "Library/Keychains/login.keychain-db"), home)!, /credentials/);
  assert.match(readProblem(join(home, ".zshrc"), home)!, /credentials/);
  assert.match(readProblem(join(home, "Projects/app/.env.local"), home)!, /environment file/);
  assert.equal(readProblem(join(home, "Documents/list.txt"), home), undefined);
  assert.equal(readProblem("/etc/hosts", home), undefined);
});

test("writes reach only the user's own files", () => {
  assert.equal(writeProblem(join(home, "Documents/list.txt"), [app], home), undefined);
  assert.equal(writeProblem(join(home, "Desktop/new folder/a.md"), [app], home), undefined);
  assert.match(writeProblem("/etc/hosts", [app], home)!, /outside your home/);
  assert.match(writeProblem(home, [app], home)!, /outside your home/);
  assert.match(writeProblem(join(home, "Library/Preferences/ae.ac.mbzuai.jaisvoice.plist"), [app], home)!, /Library/);
  assert.match(writeProblem(join(home, ".zshrc"), [app], home)!, /credentials/);
  assert.match(writeProblem(join(home, ".config/app.json"), [app], home)!, /hidden/);
  assert.match(writeProblem(join(home, "Documents/.hidden/a"), [app], home)!, /hidden/);
  assert.match(writeProblem(join(app, "agent/SYSTEM.md"), [app], home)!, /Jais Voice itself/);
  assert.match(writeProblem(resolveToolPath("~/Documents/keys/config", "/", home), [app], home)!, /credentials/);
});

test("write approvals show the file and the change", () => {
  writeFileSync(join(home, "Documents/existing.txt"), "old");
  const created = JSON.parse(fileWriteMessage("write", join(home, "Documents/new.txt"), { content: "pain, lait" }));
  assert.deepEqual([created.kind, created.reason, created.details], ["file_write", "Create this file?", join(home, "Documents/new.txt") + "\n\npain, lait"]);
  assert.equal(JSON.parse(fileWriteMessage("write", join(home, "Documents/existing.txt"), { content: "x" })).reason, "Replace this file?");
  const edit = JSON.parse(fileWriteMessage("edit", join(home, "Documents/existing.txt"), { edits: [{ oldText: "old", newText: "new" }] }));
  assert.equal(edit.details, join(home, "Documents/existing.txt") + "\n\n- old\n+ new");
  assert.ok(JSON.parse(fileWriteMessage("write", "/x", { content: "a".repeat(5000) })).details.length < 1600);
});

test("each tool area has its skill, read without its frontmatter", () => {
  assert.deepEqual(["browser_navigate", "desktop_click", "grep", "shell_execute"].map(skillFor), ["web-research", "mac-apps", "files", undefined]);
  const skills = join(new URL(".", import.meta.url).pathname, "../../skills");
  for (const name of ["web-research", "mac-apps", "files"]) {
    const text = skillInstructions(skills, name);
    assert.ok(text.startsWith("# ") && !text.includes("description:"), name);
  }
  assert.equal(skillInstructions(skills, "missing"), "");
});
