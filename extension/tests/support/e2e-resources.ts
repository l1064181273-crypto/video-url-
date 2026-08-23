import { rm } from "node:fs/promises";

export type StoppableProcess = {
  exitCode: number | null;
  signalCode: NodeJS.Signals | null;
  kill(signal: NodeJS.Signals): boolean;
  off(
    event: "exit",
    listener: (code: number | null, signal: NodeJS.Signals | null) => void,
  ): StoppableProcess;
  once(
    event: "exit",
    listener: (code: number | null, signal: NodeJS.Signals | null) => void,
  ): StoppableProcess;
};

export type E2eResources = {
  backend?: StoppableProcess | undefined;
  dataRoot?: string | undefined;
};

type CleanupOptions = {
  processTimeoutMs?: number;
  removeDirectory?: (path: string) => Promise<void>;
};

export async function cleanupE2eResources(
  resources: E2eResources,
  options: CleanupOptions = {},
): Promise<void> {
  const errors: unknown[] = [];
  try {
    await stopProcess(resources.backend, options.processTimeoutMs);
  } catch (error) {
    errors.push(error);
  }
  if (resources.dataRoot !== undefined) {
    try {
      const removeDirectory =
        options.removeDirectory ??
        (async (path: string) => {
          await rm(path, { force: true, recursive: true });
        });
      await removeDirectory(resources.dataRoot);
    } catch (error) {
      errors.push(error);
    }
  }
  if (errors.length > 0) {
    throw new AggregateError(errors, "E2E resource cleanup failed");
  }
}

export async function stopProcess(
  child: StoppableProcess | undefined,
  timeoutMs = 10_000,
): Promise<void> {
  if (child === undefined) {
    return;
  }
  if (hasExited(child)) {
    return;
  }
  const terminated = waitForExit(child, timeoutMs);
  child.kill("SIGTERM");
  if (await terminated) {
    return;
  }
  if (hasExited(child)) {
    return;
  }
  const killed = waitForExit(child, timeoutMs);
  child.kill("SIGKILL");
  if (!(await killed) && !hasExited(child)) {
    throw new Error("E2E backend did not exit after SIGKILL");
  }
}

function hasExited(child: StoppableProcess): boolean {
  return child.exitCode !== null || child.signalCode !== null;
}

function waitForExit(child: StoppableProcess, timeoutMs: number): Promise<boolean> {
  return new Promise<boolean>((resolvePromise) => {
    let settled = false;
    const finish = (exited: boolean) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(deadline);
      child.off("exit", onExit);
      resolvePromise(exited);
    };
    const onExit = () => finish(true);
    const deadline = setTimeout(() => finish(false), timeoutMs);
    child.once("exit", onExit);
    if (child.exitCode !== null || child.signalCode !== null) {
      finish(true);
    }
  });
}
