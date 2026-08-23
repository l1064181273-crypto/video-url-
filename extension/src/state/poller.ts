const VISIBLE_INTERVAL_MS = 1_000;
const HIDDEN_INTERVAL_MS = 5_000;
const FAILURE_BACKOFF_MS = [1_000, 2_000, 5_000, 10_000] as const;

type PollerOptions<T> = {
  load(signal: AbortSignal): Promise<T>;
  onData(value: T, generation: number): void;
  onError(error: unknown, generation: number): void;
  visible?: boolean;
};

export class VisibilityPoller<T> {
  private generation = 0;
  private running = false;
  private visible: boolean;
  private failureCount = 0;
  private inFlight = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private controller: AbortController | undefined;

  constructor(private readonly options: PollerOptions<T>) {
    this.visible = options.visible ?? true;
  }

  start(): number {
    return this.restart();
  }

  restart(): number {
    this.deactivate();
    this.generation += 1;
    this.running = true;
    this.failureCount = 0;
    void this.poll(this.generation);
    return this.generation;
  }

  stop(): number {
    this.deactivate();
    this.generation += 1;
    return this.generation;
  }

  setVisible(visible: boolean): void {
    if (this.visible === visible) {
      return;
    }
    this.visible = visible;
    if (this.running && !this.inFlight && this.timer !== undefined) {
      clearTimeout(this.timer);
      this.timer = undefined;
      this.schedule(this.generation);
    }
  }

  private deactivate(): void {
    this.running = false;
    if (this.timer !== undefined) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
    this.controller?.abort();
    this.controller = undefined;
    this.inFlight = false;
  }

  private async poll(generation: number): Promise<void> {
    if (!this.running || generation !== this.generation || this.inFlight) {
      return;
    }
    const controller = new AbortController();
    this.controller = controller;
    this.inFlight = true;
    try {
      const value = await this.options.load(controller.signal);
      if (!this.isCurrent(generation, controller)) {
        return;
      }
      this.failureCount = 0;
      this.options.onData(value, generation);
    } catch (error) {
      if (!this.isCurrent(generation, controller) || controller.signal.aborted) {
        return;
      }
      this.failureCount += 1;
      this.options.onError(error, generation);
    } finally {
      if (this.isCurrent(generation, controller)) {
        this.controller = undefined;
        this.inFlight = false;
        this.schedule(generation);
      }
    }
  }

  private schedule(generation: number): void {
    if (!this.running || generation !== this.generation || this.timer !== undefined) {
      return;
    }
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.poll(generation);
    }, this.nextDelay());
  }

  private nextDelay(): number {
    const visibilityDelay = this.visible ? VISIBLE_INTERVAL_MS : HIDDEN_INTERVAL_MS;
    if (this.failureCount === 0) {
      return visibilityDelay;
    }
    const backoffIndex = Math.min(this.failureCount - 1, FAILURE_BACKOFF_MS.length - 1);
    return Math.max(visibilityDelay, FAILURE_BACKOFF_MS[backoffIndex] as number);
  }

  private isCurrent(generation: number, controller: AbortController): boolean {
    return this.running && generation === this.generation && this.controller === controller;
  }
}
