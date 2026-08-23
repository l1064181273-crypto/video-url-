import { readFile } from "node:fs/promises";

import { describe, expect, it } from "vitest";

type Manifest = {
  manifest_version?: unknown;
  minimum_chrome_version?: unknown;
  permissions?: unknown;
  host_permissions?: unknown;
  background?: unknown;
  side_panel?: unknown;
  action?: unknown;
  content_security_policy?: unknown;
  content_scripts?: unknown;
};

async function readManifest(): Promise<Manifest> {
  const raw = await readFile(new URL("../../public/manifest.json", import.meta.url), "utf8");
  return JSON.parse(raw) as Manifest;
}

describe("Manifest V3 permissions", () => {
  it("uses the exact permission and loopback host allowlists", async () => {
    const manifest = await readManifest();

    expect(manifest.manifest_version).toBe(3);
    expect(manifest.permissions).toEqual(["storage", "downloads", "sidePanel"]);
    expect(manifest.host_permissions).toEqual(["http://127.0.0.1/*"]);
    expect(manifest.content_scripts).toBeUndefined();
    expect(manifest.permissions).not.toEqual(
      expect.arrayContaining(["tabs", "scripting", "nativeMessaging", "clipboardWrite"]),
    );
    expect(manifest.host_permissions).not.toContain("<all_urls>");
  });

  it("declares one module worker, one side panel, and no action popup", async () => {
    const manifest = await readManifest();

    expect(manifest.background).toEqual({
      service_worker: "background.js",
      type: "module",
    });
    expect(manifest.side_panel).toEqual({ default_path: "sidepanel.html" });
    expect(manifest.action).toEqual({
      default_title: "打开 Local Video Transcriber",
    });
  });

  it("forbids remote, inline, and evaluated extension scripts", async () => {
    const manifest = await readManifest();

    expect(manifest.content_security_policy).toEqual({
      extension_pages: "script-src 'self'; object-src 'self'",
    });
  });
});
