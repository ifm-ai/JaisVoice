/**
 * App control for the JAIS Voice Pi extension, testable without Pi.
 *
 * The native helper (`JAISVoice --desktop-helper`, a child of the app's process
 * tree so macOS applies JAIS Voice's Accessibility permission) reads and drives
 * other apps' windows. This side asks JAIS Voice before each app is read or
 * driven, once per request, and keeps the web from receiving what was read.
 */
import { spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";

export const DESKTOP_HELPER = process.env.JAIS_DESKTOP_HELPER || "";
export const DESKTOP_TOOLS = [
  "desktop_list_windows", "desktop_open_app", "desktop_snapshot", "desktop_find", "desktop_click",
  "desktop_type", "desktop_press_key", "desktop_menu", "desktop_scroll",
];

export type Access = "read" | "control";

/** An app as the helper resolved it; `tier` is what no approval can exceed. */
export interface DesktopApp {
  name: string;
  bundle_id?: string;
  pid?: number;
  running: boolean;
  tier: "control" | "read" | "blocked";
  note?: string;
}

/** One JSON line per request and per answer; the helper exits when stdin closes. */
export class DesktopHelper {
  private child?: ChildProcess;
  private pending = new Map<number, { resolve: (value: any) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }>();
  private next = 0;
  private command: string;
  private args: string[];

  constructor(command: string, args: string[] = ["--desktop-helper"]) {
    this.command = command; this.args = args;
  }

  get running(): boolean { return this.child !== undefined; }

  private start(): ChildProcess {
    if (this.child) return this.child;
    if (!this.command) throw new Error("App control is unavailable: run the agent from JAIS Voice, which provides its desktop helper.");
    const child = spawn(this.command, this.args, { stdio: ["pipe", "pipe", "inherit"] });
    const failAll = (message: string) => {
      if (this.child === child) this.child = undefined;
      for (const request of this.pending.values()) { clearTimeout(request.timer); request.reject(new Error(message)); }
      this.pending.clear();
    };
    createInterface({ input: child.stdout! }).on("line", (line) => this.receive(line));
    child.on("exit", () => failAll("The desktop helper stopped. Try again."));
    child.on("error", (error) => failAll("The desktop helper could not start: " + error.message));
    child.stdin!.on("error", () => { /* reported by exit */ });
    // The helper must not keep Pi alive; pending requests hold their own timers.
    child.unref();
    (child.stdout as any)?.unref?.();
    (child.stdin as any)?.unref?.();
    this.child = child;
    return child;
  }

  private receive(line: string) {
    let message: any;
    try { message = JSON.parse(line); } catch { return; }
    const request = this.pending.get(message?.id);
    if (!request) return;
    this.pending.delete(message.id);
    clearTimeout(request.timer);
    if (typeof message.error === "string") request.reject(new Error(message.error));
    else request.resolve(message.result ?? {});
  }

  request(method: string, params: Record<string, unknown> = {}, signal?: AbortSignal, seconds = 45): Promise<any> {
    if (signal?.aborted) return Promise.reject(new Error("Cancelled."));
    let child: ChildProcess;
    try { child = this.start(); } catch (error) { return Promise.reject(error); }
    const id = ++this.next;
    return new Promise((resolve, reject) => {
      const done = () => signal?.removeEventListener("abort", onAbort);
      const timer = setTimeout(() => {
        this.pending.delete(id); done();
        reject(new Error(`The desktop helper did not answer within ${seconds} seconds. Do not claim success.`));
      }, seconds * 1000);
      // A cancelled request may still complete in the app; its answer is ignored.
      const onAbort = () => { clearTimeout(timer); this.pending.delete(id); reject(new Error("Cancelled.")); };
      signal?.addEventListener("abort", onAbort, { once: true });
      this.pending.set(id, { resolve: (value) => { done(); resolve(value); }, reject: (error) => { done(); reject(error); }, timer });
      child.stdin!.write(JSON.stringify({ id, method, params }) + "\n");
    });
  }

  stop() { this.child?.stdin?.end(); }
}

/** Approvals granted during the current request, by app; control includes reading. */
export class DesktopGrants {
  private granted = new Map<string, Access>();
  private static key(app: DesktopApp) { return app.bundle_id || app.name; }
  has(app: DesktopApp, need: Access): boolean {
    const level = this.granted.get(DesktopGrants.key(app));
    return level === "control" || level === need;
  }
  grant(app: DesktopApp, access: Access) {
    if (!this.has(app, access)) this.granted.set(DesktopGrants.key(app), access);
  }
  reset() { this.granted.clear(); }
}

/** Why an app cannot be used at the requested level, if it cannot. */
export function tierProblem(app: DesktopApp, need: Access): string | undefined {
  if (app.tier === "blocked") return `${app.name} is not accessible to JAIS Voice: ${app.note ?? "protected app"}. Do not try another way.`;
  if (need === "control" && app.tier !== "control") {
    return `${app.name} is read-only for JAIS Voice: ${app.note ?? "protected app"}. Do not try another way; tell the user what you found.`;
  }
  return undefined;
}

/** The approval JAIS Voice shows; `kind` and `app` let it word the question in the user's language. */
export function appAccessMessage(app: DesktopApp, access: Access): string {
  const reason = access === "control"
    ? `Let JAIS Voice control ${app.name} until this request ends: click, type, use its menus and keys.`
    : `Let JAIS Voice read the windows of ${app.name} until this request ends.`;
  return JSON.stringify({ reason, details: app.bundle_id ? `${app.name} (${app.bundle_id})` : app.name, kind: "app_" + access, app: app.name });
}

export function appRefused(app: string, access: Access): string {
  return `The user did not allow JAIS Voice to ${access === "control" ? "control" : "read"} ${app}, so nothing was done there. `
    + "Do not retry and do not try another way. Tell the user; access to apps can be allowed in the JAIS Voice settings.";
}

/** The overview of open windows is refused only when actions on the Mac are never allowed. */
export const OVERVIEW_MESSAGE = JSON.stringify({ reason: "List the open windows", details: "", kind: "overview" });

// MARK: The web after reading the Mac

/** Domain names the user wrote in the request, without "www.". */
export function hostsIn(text: string): string[] {
  const hosts = new Set<string>();
  for (const match of text.toLowerCase().matchAll(/(?:https?:\/\/)?((?:[a-z0-9-]+\.)+[a-z]{2,})/g)) {
    hosts.add(match[1].replace(/^www\./, ""));
  }
  return [...hosts];
}

/** Once windows were read, the browser may only go to DuckDuckGo or to sites the user named without asking. */
export function navigationAllowed(url: string, hosts: string[]): boolean {
  let host: string;
  try { host = new URL(url).hostname.toLowerCase().replace(/^www\./, ""); } catch { return false; }
  return ["duckduckgo.com", ...hosts].some((allowed) => host === allowed || host.endsWith("." + allowed));
}

export function navigationMessage(url: string): string {
  return JSON.stringify({ reason: "JAIS Voice read your apps during this request. Open this page?", details: url, kind: "web_after_desktop" });
}

export const NAVIGATION_REFUSED = "The user did not allow opening this page after the assistant read their apps, so it was NOT opened. "
  + "Do not retry it and do not open it another way.";
