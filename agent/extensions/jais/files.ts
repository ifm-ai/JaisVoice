/**
 * What Pi's file tools (read, ls, find, grep, write, edit) may touch, testable without Pi.
 *
 * Reading asks Jais Voice once per request, except for the agent's own skills.
 * Writing asks for every file, and only reaches the user's own files: the home
 * folder, outside ~/Library, hidden files and Jais Voice itself, so the agent
 * can neither change its settings, prompt or skills nor plant startup scripts.
 * Credential stores are never read nor written.
 */
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

export const READ_TOOLS = ["read", "ls", "find", "grep"];
export const WRITE_TOOLS = ["write", "edit"];
export const FILE_TOOLS = [...READ_TOOLS, ...WRITE_TOOLS];

/** Where secrets live: SSH and cloud keys, shell startup files (often API keys), keychains, browser profiles and cookies. */
const SECRET_PATHS = [
  ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".netrc", ".git-credentials", ".npmrc", ".pypirc",
  ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".bashrc", ".bash_profile", ".profile", ".config/fish",
  ".config/gh", ".config/gcloud", "Library/Keychains", "Library/Cookies", "Library/Safari",
  "Library/Application Support/Google/Chrome", "Library/Application Support/Firefox", "Library/Application Support/BraveSoftware",
  "Library/Application Support/Microsoft Edge", "Library/Application Support/1Password", "Library/Group Containers/2BUA8C4S2C.com.1password",
];

export const isInside = (path: string, folder: string) => path === folder || path.startsWith(folder.endsWith(sep) ? folder : folder + sep);

/** The absolute path a tool argument names, with "~" expanded and symlinks of existing parents resolved. */
export function resolveToolPath(path: string | undefined, cwd: string, home = homedir()): string {
  let value = (path ?? "").trim().replace(/^@/, "");
  if (value === "~" || value.startsWith("~/")) value = home + value.slice(1);
  const absolute = resolve(isAbsolute(value) ? value : join(cwd, value || "."));
  // A symlink inside an allowed folder must not lead outside it.
  let existing = absolute, rest = "";
  while (!existsSync(existing) && dirname(existing) !== existing) {
    rest = rest ? join(basename(existing), rest) : basename(existing);
    existing = dirname(existing);
  }
  try { return rest ? join(realpathSync(existing), rest) : realpathSync(existing); } catch { return absolute; }
}

/** Why this path may not be read, if it may not. */
export function readProblem(path: string, home = homedir()): string | undefined {
  const secret = SECRET_PATHS.find((entry) => isInside(path, join(home, entry)));
  if (secret) return `${path} holds credentials (~/${secret}): Jais Voice never reads it. Do not try another way.`;
  if (basename(path) === "auth.json" && path.includes("pi-agent")) return "The agent's credentials are never read.";
  if (basename(path) === "remote-api-key" && path.includes("Jais Voice")) return "The API key of Jais Voice is never read.";
  if (/^\.env(\..*)?$/.test(basename(path))) return `${path} is an environment file, which usually holds credentials: Jais Voice never reads it.`;
  return undefined;
}

/** Why this path may not be written, if it may not; `protectedPaths` are Jais Voice's own folders. */
export function writeProblem(path: string, protectedPaths: string[], home = homedir()): string | undefined {
  const secret = readProblem(path, home);
  if (secret) return secret;
  if (!isInside(path, home) || path === home) {
    return `${path} is outside your home folder: Jais Voice only writes your own files (for example in ~/Documents or ~/Desktop).`;
  }
  const inside = relative(home, path).split(sep);
  if (inside[0] === "Library") return `${path} is in ~/Library (app data and settings): Jais Voice does not write there.`;
  if (inside.some((part) => part.startsWith("."))) return `${path} is a hidden file or folder (settings, startup scripts): Jais Voice does not write there.`;
  const own = protectedPaths.filter(Boolean).map((entry) => resolveToolPath(entry, "/", home)).find((entry) => isInside(path, entry));
  if (own) return `${path} belongs to Jais Voice itself: the agent never changes its own app, settings, prompt or skills.`;
  return undefined;
}

/** The folder or file a file tool call reads or writes; the working directory when none is named. */
export function toolPath(input: Record<string, unknown>, cwd: string, home = homedir()): string {
  return resolveToolPath(typeof input.path === "string" ? input.path : undefined, cwd, home);
}

/** The approval for reading files, once per request. */
export const FILES_READ_MESSAGE = JSON.stringify({
  reason: "Let Jais Voice read your files until this request ends?", details: "", kind: "files_read",
});

/** The approval for one write: the file and what changes in it. */
export function fileWriteMessage(toolName: string, path: string, input: Record<string, unknown>): string {
  let preview = "";
  if (toolName === "write") {
    preview = String(input.content ?? "");
  } else if (Array.isArray(input.edits)) {
    preview = input.edits.map((edit: any) => `- ${String(edit?.oldText ?? "")}\n+ ${String(edit?.newText ?? "")}`).join("\n\n");
  }
  if (preview.length > 1500) preview = preview.slice(0, 1500) + "…";
  const reason = toolName === "write" ? (existsSync(path) ? "Replace this file?" : "Create this file?") : "Change this file?";
  return JSON.stringify({ reason, details: path + (preview ? "\n\n" + preview : ""), kind: "file_write", path });
}

export const FILES_REFUSED = "The user did not allow Jais Voice to read their files, so nothing was read. Do not retry and do not try another way. "
  + "Tell the user; file access can be allowed in the Jais Voice settings.";
export const WRITE_REFUSED = "The user did not allow this change, so the file was NOT written. Do not retry it and do not try another way.";

/** The skill that explains a tool's area: the web, the apps on this Mac or the user's files. */
export function skillFor(toolName: string): string | undefined {
  if (toolName.startsWith("browser_")) return "web-research";
  if (toolName.startsWith("desktop_")) return "mac-apps";
  if (FILE_TOOLS.includes(toolName)) return "files";
  return undefined;
}

/** A skill's instructions without their frontmatter, as the model would read them. */
export function skillInstructions(skillsDir: string, name: string): string {
  try {
    return readFileSync(join(skillsDir, name, "SKILL.md"), "utf8").replace(/^---\n[\s\S]*?\n---\n/, "").trim();
  } catch {
    return "";
  }
}
