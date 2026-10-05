/**
 * Pure helpers of the JAIS Voice Pi extension, testable without Pi.
 *
 * Actions on the Mac run only once JAIS Voice approves them (its "Actions on this
 * Mac" setting: never, ask each time, or always). They then run with the current
 * user's privileges, in a fresh process group with a minimal environment. This
 * is not a sandbox; macOS privacy permissions still apply.
 */
import { spawn } from "node:child_process";
import { closeSync, constants, mkdirSync, openSync, readSync, realpathSync } from "node:fs";
import { constants as osConstants, homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const ROOT = process.env.JAIS_PROJECT_ROOT || resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
export const RUNTIME = process.env.JAIS_RUNTIME_DIR || join(ROOT, ".runtime");
export const NODE = process.env.JAIS_NODE || process.execPath;
export const PLAYWRIGHT_CLI = process.env.JAIS_PLAYWRIGHT_CLI || join(ROOT, "node_modules/@playwright/mcp/cli.js");
/** Where the agent's helpers run; Pi itself runs from the home folder, for the file tools. */
export const WORKSPACE = process.env.JAIS_AGENT_WORKSPACE || process.cwd();
export const BROWSER_OUTPUT = join(RUNTIME, "browser-output", String(process.pid));

export const MAX_ACTIONS = 50;
export const SHELL_SECONDS = 30;
export const MAX_COMMAND_BYTES = 8000;
export const MAX_REASON_BYTES = 1000;
export const MAX_OUTPUT_BYTES = 8192;
export const MAX_SNAPSHOT_BYTES = 60000;
export const BROWSER_PREFIX = "mcp__browser__";
/** Pi stops waiting slightly after the app closes its own approval alert (120 s). */
export const APPROVAL_SECONDS = 125;
export const REFUSED = "The user did not allow this action in JAIS Voice, so it was NOT executed. Do not retry it and do not "
  + "try to achieve it with another tool. Tell the user it was not done; actions on this Mac can be allowed in the JAIS Voice settings.";

/** The confirmation message JAIS Voice parses: why the action runs and exactly what runs. */
export function approvalMessage(reason: string, details: string): string {
  return JSON.stringify({ reason, details });
}
/** The Playwright tools the model sees; the others repeat these (fill_form, find) or are rarely needed. */
export const BROWSER_TOOLS = [
  "browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_press_key", "browser_wait_for", "browser_tabs",
];
export const MAC_TOOLS = ["shell_execute", "macos_create_note"];
export const SHELL_ENV = {
  PATH: "/usr/bin:/bin:/usr/sbin:/sbin",
  LANG: "en_US.UTF-8",
  LC_CTYPE: "en_US.UTF-8",
  HOME: homedir(),
};

export const bytes = (value: string) => Buffer.byteLength(value, "utf8");
export const shellQuote = (value: string) => "'" + value.replace(/'/g, "'\\''") + "'";

export function bounded(value: string, limit: number): string {
  const encoded = Buffer.from(value, "utf8");
  if (encoded.length <= limit) return value;
  // Drop a split trailing UTF-8 sequence rather than emitting a replacement character.
  return encoded.subarray(0, limit).toString("utf8").replace(/�$/, "") + "…";
}

export function commandProblem(command: unknown, reason: unknown): string | undefined {
  if (typeof command !== "string" || !command.trim() || command.includes("\0") || bytes(command) > MAX_COMMAND_BYTES) {
    return `Provide one non-empty command (maximum ${MAX_COMMAND_BYTES} UTF-8 bytes).`;
  }
  // Invisible controls and bidirectional overrides can hide what is executed.
  for (const char of command) {
    const code = char.codePointAt(0)!;
    if ((code < 32 && code !== 9 && code !== 10 && code !== 13) || code === 127
        || (code >= 0x202a && code <= 0x202e) || (code >= 0x2066 && code <= 0x2069)) {
      return "The command contains invisible control characters.";
    }
  }
  if (typeof reason !== "string" || !reason.trim() || reason.includes("\0") || bytes(reason) > MAX_REASON_BYTES) {
    return `Provide a short reason (maximum ${MAX_REASON_BYTES} UTF-8 bytes).`;
  }
  return undefined;
}

export interface ShellResult { exit_code: number | null; output: string; error: string | null; timed_out: boolean }

/** Run one command in its own process group; bounded time and output, cancellable. */
export function runShell(command: string, signal?: AbortSignal, seconds = SHELL_SECONDS): Promise<ShellResult> {
  const directory = join(homedir(), "Library/Caches/ae.ac.mbzuai.jaisvoice/Shell");
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  return new Promise((resolvePromise) => {
    if (signal?.aborted) return resolvePromise({ exit_code: null, output: "", error: "Command cancelled.", timed_out: false });
    const child = spawn("/bin/zsh", ["-f", "-c", command], {
      cwd: directory, env: SHELL_ENV, detached: true, stdio: ["ignore", "pipe", "pipe"],
    });
    let captured = Buffer.alloc(0);
    let truncated = false;
    let stopped: "timeout" | "cancel" | undefined;
    const capture = (chunk: Buffer) => {
      const remaining = MAX_OUTPUT_BYTES - captured.length;
      if (chunk.length > remaining) truncated = true;
      if (remaining > 0) captured = Buffer.concat([captured, chunk.subarray(0, remaining)]);
    };
    child.stdout!.on("data", capture);
    child.stderr!.on("data", capture);
    const killGroup = (name: NodeJS.Signals) => {
      try { if (child.pid) process.kill(-child.pid, name); } catch { /* group already gone */ }
    };
    const stop = (why: "timeout" | "cancel") => {
      if (stopped) return;
      stopped = why;
      killGroup("SIGTERM");
      setTimeout(() => killGroup("SIGKILL"), 250).unref();
    };
    const timer = setTimeout(() => stop("timeout"), seconds * 1000);
    const onAbort = () => stop("cancel");
    signal?.addEventListener("abort", onAbort, { once: true });
    const finish = (result: ShellResult) => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      // Background work left by the command belongs to the same owned group.
      killGroup("SIGKILL");
      resolvePromise(result);
    };
    child.on("error", (error) => finish({ exit_code: null, output: "", error: error.message, timed_out: false }));
    child.on("close", (code, killedBy) => {
      const output = captured.toString("utf8").replace(/�$/, "") + (truncated ? "\n[Output limited to 8 KB.]" : "");
      if (stopped) {
        return finish({ exit_code: null, output, error: stopped === "timeout" ? `Command stopped after ${seconds} seconds.` : "Command cancelled.", timed_out: stopped === "timeout" });
      }
      const exit = code ?? (killedBy ? 128 + (osConstants.signals as Record<string, number>)[killedBy] : null);
      finish({ exit_code: exit, output, error: exit === 0 ? null : `The command exited with code ${exit}.`, timed_out: false });
    });
  });
}

const NOTE_SCRIPT = `on run argv
    set noteTitle to item 1 of argv
    set noteHTML to item 2 of argv
    tell application "Notes"
        set destinationFolder to default folder of default account
        set createdNote to make new note at destinationFolder with properties {name:noteTitle, body:noteHTML}
        activate
        show createdNote
        return plaintext of createdNote
    end tell
end run`;

const escapeHTML = (value: string) => value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#x27;");

/** User strings stay in argv; the executed AppleScript is constant. */
export function noteCommand(title: string, body: string): string {
  if (!title.trim() || title.includes("\0") || bytes(title) > 300) throw new Error("Provide a non-empty note title of at most 300 UTF-8 bytes.");
  if (!body.trim() || body.includes("\0") || bytes(body) > 4000) {
    throw new Error("Provide the exact requested note text, non-empty and at most 4000 UTF-8 bytes. Do not silently shorten or translate it.");
  }
  // Notes prepends the title itself; repeating it in the body would duplicate it.
  const html = "<div>" + escapeHTML(body).replace(/\r\n?/g, "\n").replace(/\n/g, "<br>") + "</div>";
  const command = `/usr/bin/osascript - ${shellQuote(title)} ${shellQuote(html)} <<'NOTES_APPLESCRIPT'\n${NOTE_SCRIPT}\nNOTES_APPLESCRIPT`;
  if (bytes(command) > MAX_COMMAND_BYTES) throw new Error("This note is too long after safe escaping. Ask for shorter content; do not silently truncate it.");
  return command;
}

/** Replace Playwright's snapshot file link with the file content from this session's output directory. */
export function inlineSnapshots(text: string, outputDir: string, bases: string[]): string {
  return text.replace(/^### Snapshot\r?\n- \[Snapshot\]\(([^)\r\n]+)\)/m, (match, link: string) => {
    for (const base of bases) {
      try {
        const file = realpathSync(isAbsolute(link) ? link : resolve(base, link));
        if (!file.startsWith(realpathSync(outputDir) + "/") || !/^page-[A-Za-z0-9_.-]+\.yml$/.test(basename(file))) continue;
        const descriptor = openSync(file, constants.O_RDONLY | constants.O_NOFOLLOW);
        try {
          const buffer = Buffer.alloc(MAX_SNAPSHOT_BYTES + 1);
          const count = readSync(descriptor, buffer, 0, buffer.length, 0);
          const snapshot = buffer.subarray(0, Math.min(count, MAX_SNAPSHOT_BYTES)).toString("utf8").replace(/�$/, "");
          return "### Snapshot\n```yaml\n" + snapshot + (count > MAX_SNAPSHOT_BYTES ? "\n[Snapshot abbreviated]" : "") + "\n```";
        } finally { closeSync(descriptor); }
      } catch { /* try the next base */ }
    }
    return "### Snapshot\n[The page snapshot could not be read. Call browser_snapshot for the current page state.]";
  });
}

export type Message = { role: string; content?: any; [key: string]: any };

/** Characters kept in the model's context (32k tokens, with up to 16k for the answer). */
export const CONTEXT_LIMITS = { latestResult: 20000, earlierResults: 12000, earlierResult: 1200, assistantText: 3000, userTurns: 8 };

/**
 * Keep the current task and recent dialogue, and shorten stale page snapshots
 * and files in a copy of the context. The gateway remains the authority for the
 * exact token budget; this keeps browsing loops from filling the window.
 */
export function compactContext(messages: Message[]): Message[] {
  const users = messages.flatMap((message, index) => message.role === "user" ? [index] : []);
  const start = users.length >= CONTEXT_LIMITS.userTurns ? users[users.length - CONTEXT_LIMITS.userTurns] : 0;
  const kept = messages.slice(start).map((message) => structuredClone(message));
  const results = kept.filter((message) => message.role === "toolResult");
  const priorBudget = Math.min(CONTEXT_LIMITS.earlierResult, Math.floor(CONTEXT_LIMITS.earlierResults / Math.max(1, results.length - 1)));
  results.forEach((message, index) => {
    const limit = index === results.length - 1 ? CONTEXT_LIMITS.latestResult : priorBudget;
    for (const part of message.content ?? []) {
      if (part.type === "text" && part.text.length > limit) {
        part.text = part.text.slice(0, limit) + "\n[Earlier content abbreviated; call the tool again for the current state.]";
      }
    }
  });
  for (const message of kept) {
    if (message.role !== "assistant" || !Array.isArray(message.content)) continue;
    for (const part of message.content) {
      if (part.type === "text" && part.text.length > CONTEXT_LIMITS.assistantText) part.text = part.text.slice(0, CONTEXT_LIMITS.assistantText) + "…";
    }
  }
  // Remind after each Arabic request and each tool result answering it: the
  // English system prompt and English pages otherwise pull replies to English.
  let arabic = false;
  for (const message of kept) {
    if (message.role === "user") arabic = mostlyArabic(textOf(message.content));
    if (arabic && (message.role === "user" || message.role === "toolResult")) message.content = withReminder(message.content);
  }
  return kept;
}

/** Appended to Arabic requests and their tool results in the model's context. */
export const ARABIC_REMINDER = "\n\n(اكتب ردك كله بالعربية، بما في ذلك أي نص بين استدعاءات الأدوات.)";

function mostlyArabic(text: string): boolean {
  return (text.match(/[\u0600-\u06ff]/g) ?? []).length > (text.match(/[A-Za-z]/g) ?? []).length;
}

function textOf(content: any): string {
  if (typeof content === "string") return content;
  return Array.isArray(content) ? content.filter((part) => part.type === "text").map((part) => part.text).join(" ") : "";
}

function withReminder(content: any): any {
  if (typeof content === "string") return content + ARABIC_REMINDER;
  if (!Array.isArray(content)) return content;
  const last = content.findLastIndex((part) => part.type === "text");
  if (last < 0) return [...content, { type: "text", text: ARABIC_REMINDER.trim() }];
  return content.map((part, index) => index === last ? { ...part, text: part.text + ARABIC_REMINDER } : part);
}
