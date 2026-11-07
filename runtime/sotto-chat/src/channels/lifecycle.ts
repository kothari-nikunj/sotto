/** Process-local single poller ownership; durable inbox admission is the service journal's responsibility. */
export class PollerOwnership {
  #owner: string | undefined;
  claim(owner: string): () => void {
    if (this.#owner) throw new Error(`poller already owned by ${this.#owner}`);
    this.#owner = owner;
    let released = false;
    return () => { if (!released && this.#owner === owner) this.#owner = undefined; released = true; };
  }
  get owner(): string | undefined { return this.#owner; }
}
