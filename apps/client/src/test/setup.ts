import "@testing-library/jest-dom/vitest";

// jsdom has no layout engine or native dialog methods. Keep the browser APIs
// used by the floating assistant and shared settings/help dialogs available.
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
if (typeof HTMLDialogElement !== "undefined") {
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event("close")); };
}
