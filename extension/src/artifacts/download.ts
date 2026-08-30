import { LocalApiClient, type ConnectionSource } from "../api/client";
import type { JobArtifact } from "../api/contracts";
import { ApiClientError } from "../api/errors";

type DownloadsApi = {
  download(options: chrome.downloads.DownloadOptions): Promise<number>;
};

type ObjectUrlApi = {
  createObjectURL(blob: Blob): string;
  revokeObjectURL(url: string): void;
};

export type ArtifactDownloadOptions = {
  saveAs?: boolean;
};

export class ArtifactDownloadService {
  constructor(
    private readonly client: LocalApiClient,
    private readonly connectionSource: ConnectionSource,
    private readonly downloads: DownloadsApi = chrome.downloads,
    private readonly objectUrls: ObjectUrlApi = URL,
  ) {}

  async download(
    jobId: string,
    jobTitle: string,
    artifact: JobArtifact,
    options: ArtifactDownloadOptions = {},
    signal?: AbortSignal,
  ): Promise<number> {
    const blob = await this.client.getArtifactBlob(jobId, artifact, signal);
    signal?.throwIfAborted();
    let connection;
    try {
      connection = await this.connectionSource.getConnection();
    } catch {
      throw new ApiClientError("notConfigured", "无法读取本地连接设置，请重新配置");
    }
    const directory = safeDownloadDirectory(jobTitle, jobId, connection.token);
    let objectUrl: string;
    try {
      objectUrl = this.objectUrls.createObjectURL(blob);
    } catch {
      throw new ApiClientError("server", "浏览器未能准备下载，请稍后重试");
    }
    try {
      const downloadId = await this.downloads.download({
        url: objectUrl,
        filename: `${directory}/${artifact.kind}`,
        conflictAction: "uniquify",
        saveAs: options.saveAs === true,
      });
      if (!Number.isInteger(downloadId) || downloadId < 0) {
        throw new Error("download did not return an ID");
      }
      return downloadId;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw error;
      }
      throw new ApiClientError("server", "浏览器未能开始下载，请检查下载权限后重试");
    } finally {
      this.objectUrls.revokeObjectURL(objectUrl);
    }
  }
}

export function safeDownloadDirectory(title: string, jobId: string, token: string | null): string {
  const normalizedTitle = title.normalize("NFKC");
  const sanitized = normalizedTitle
    .split("")
    .map((character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint < 32 || codePoint === 127 ? "_" : character;
    })
    .join("")
    .replace(/[<>:"/\\|?*]/gu, "_")
    .replace(/_+/gu, "_")
    .replace(/\s+/gu, " ")
    .replace(/^[.\s]+|[.\s]+$/gu, "")
    .slice(0, 64);
  const exposesToken =
    token !== null &&
    token.length > 0 &&
    (normalizedTitle.includes(token) || sanitized.includes(token));
  const safeTitle = sanitized.length === 0 || exposesToken ? "local-video" : sanitized;
  const suffix = /^[0-9a-f]{8}/u.exec(jobId)?.[0] ?? "job";
  return `${safeTitle}--${suffix}`;
}
