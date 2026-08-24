import {
  CAPABILITY_COMPONENTS,
  type CapabilitiesResponse,
  type CapabilityComponentName,
  type CapabilityStatus,
  type ErrorCode,
  type SettingsResponse,
} from "../api/contracts";
import { ApiClientError } from "../api/errors";

export type CapabilityPresentation = {
  name: CapabilityComponentName;
  label: string;
  status: CapabilityStatus;
  statusLabel: string;
  advice: string;
};

const COMPONENT_LABELS: Record<CapabilityComponentName, string> = {
  ffmpeg: "FFmpeg",
  ollama: "Ollama",
  asr_package: "ASR Python 包",
  asr_model: "ASR 模型",
  diarization: "说话人识别",
  translation_primary: "主翻译模型",
  translation_fallback: "备用翻译模型",
};

const STATUS_LABELS: Record<CapabilityStatus, string> = {
  available: "可用",
  missing: "缺失",
  unavailable: "不可用",
  unchecked: "未检查",
};

export function capabilityPresentations(
  capabilities: CapabilitiesResponse,
): CapabilityPresentation[] {
  return CAPABILITY_COMPONENTS.map((name) => {
    const component = capabilities.components[name];
    return {
      name,
      label: COMPONENT_LABELS[name],
      status: component.status,
      statusLabel: STATUS_LABELS[component.status],
      advice: capabilityAdvice(name, component.status),
    };
  });
}

export function capabilityAdvice(name: CapabilityComponentName, status: CapabilityStatus): string {
  if (status === "available") {
    return "已通过本地探测";
  }
  if (status === "unchecked") {
    return name === "translation_primary" || name === "translation_fallback"
      ? "等待 Ollama 可用后重新检查"
      : "等待下一次本地探测";
  }
  if (name === "ollama") {
    return status === "unavailable"
      ? "Ollama 未运行，请启动 Ollama"
      : "未检测到 Ollama，请先安装并启动";
  }
  if (name === "asr_package" && status === "missing") {
    return "ASR Python 包缺失，请在本地环境安装";
  }
  if (name === "asr_model" && status === "missing") {
    return "ASR 模型缺失，请安装配置的模型";
  }
  if (name === "diarization" && status === "missing") {
    return "说话人模型缺失，请安装所需模型文件";
  }
  if (name === "translation_primary" && status === "missing") {
    return "主翻译模型缺失，请在 Ollama 中安装";
  }
  if (name === "translation_fallback" && status === "missing") {
    return "备用翻译模型缺失，请在 Ollama 中安装";
  }
  if (name === "ffmpeg" && status === "missing") {
    return "未检测到 FFmpeg，请安装后重启本地服务";
  }
  return "组件暂时不可用，请检查本地服务后重试";
}

export function jobErrorAdvice(errorCode: ErrorCode): string {
  if (errorCode === "INVALID_URL") {
    return "检查 HTTP/HTTPS 地址后重新提交";
  }
  if (errorCode === "DOWNLOAD_UNSUPPORTED" || errorCode === "DOWNLOAD_FAILED") {
    return "更换公开地址，或检查网络后重试";
  }
  if (errorCode === "FFMPEG_NOT_FOUND" || errorCode === "MEDIA_INVALID") {
    return "检查本地服务依赖或更换媒体";
  }
  if (errorCode === "ASR_MODEL_MISSING" || errorCode === "TRANSCRIPTION_FAILED") {
    return "检查 ASR 模型安装、磁盘和内存";
  }
  if (
    errorCode === "DIARIZATION_TOKEN_REQUIRED" ||
    errorCode === "DIARIZATION_MODEL_MISSING" ||
    errorCode === "DIARIZATION_FAILED"
  ) {
    return "检查说话人识别模型和本地授权配置";
  }
  if (
    errorCode === "OLLAMA_UNAVAILABLE" ||
    errorCode === "TRANSLATION_MODEL_MISSING" ||
    errorCode === "TRANSLATION_FAILED"
  ) {
    return "启动 Ollama 或安装配置的翻译模型";
  }
  if (
    errorCode === "TRANSLATION_INVALID_RESPONSE" ||
    errorCode === "TRANSLATION_ALL_MODELS_FAILED"
  ) {
    return "检查翻译模型后手工重试";
  }
  if (errorCode === "EXPORT_FAILED" || errorCode === "DISK_SPACE_LOW") {
    return "检查磁盘空间和本地目录权限";
  }
  if (errorCode === "CANCELLED_BY_USER") {
    return "可手工重新加入队列";
  }
  if (
    errorCode === "JOB_STATE_CONFLICT" ||
    errorCode === "RETRY_NOT_ALLOWED" ||
    errorCode === "DELETE_CONFIRMATION_REQUIRED"
  ) {
    return "刷新任务状态后重试";
  }
  if (
    errorCode === "UNSAFE_JOB_PATH" ||
    errorCode === "DELETE_FAILED" ||
    errorCode === "DELETE_CLEANUP_PENDING" ||
    errorCode === "ARTIFACT_NOT_FOUND"
  ) {
    return "检查本地服务日志和文件状态";
  }
  if (errorCode === "SETTINGS_APPLY_FAILED") {
    return "保持原并发数，检查本地 worker 后重试";
  }
  if (errorCode === "UNAUTHORIZED") {
    return "重新输入配对 Token";
  }
  if (errorCode === "CAPABILITIES_UNAVAILABLE") {
    return "稍后重新检查本地能力状态";
  }
  if (errorCode === "UNSUPPORTED_SOURCE_LANGUAGE") {
    return "更换支持的源语言或媒体";
  }
  return "查看本地日志后重试";
}

export class SettingsUpdateGate {
  private busy = false;

  isBusy(): boolean {
    return this.busy;
  }

  run(operation: () => Promise<SettingsResponse>): Promise<SettingsResponse> | undefined {
    if (this.busy) {
      return undefined;
    }
    this.busy = true;
    let result: Promise<SettingsResponse>;
    try {
      result = operation();
    } catch (error) {
      this.busy = false;
      throw error;
    }
    return result.finally(() => {
      this.busy = false;
    });
  }
}

export function settingsErrorMessage(error: unknown): string {
  if (!(error instanceof ApiClientError)) {
    return "运行设置更新失败，请稍后重试";
  }
  if (error.kind === "validation") {
    return "并发数只能为 1 或 2";
  }
  if (error.kind === "conflict") {
    return "后端设置已变化，正在刷新，请重试";
  }
  if (error.kind === "server" && error.status === 503) {
    return "无法应用并发设置，请检查本地 worker 后重试";
  }
  return error.message;
}
