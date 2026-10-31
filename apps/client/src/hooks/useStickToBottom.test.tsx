import { act, render } from "@testing-library/react";
import { expect, it } from "vitest";
import { useStickToBottom } from "./useStickToBottom";

/** jsdom has no layout: give the region a fixed box and a content height we control. */
function sized(element: HTMLElement, content: { height: number }) {
  Object.defineProperty(element, "clientHeight", { configurable: true, get: () => 300 });
  Object.defineProperty(element, "scrollHeight", { configurable: true, get: () => content.height });
}

function Region({ lines, followKey }: { lines: string[]; followKey?: string }) {
  const ref = useStickToBottom<HTMLDivElement>(followKey);
  return <div data-testid="region" ref={ref}>{lines.map((line, index) => <p key={index}>{line}</p>)}</div>;
}

const flush = () => act(async () => { await Promise.resolve(); });

it("follows new content while the reader is at the bottom", async () => {
  const content = { height: 1000 };
  const view = render(<Region lines={["a"]} />);
  const region = view.getByTestId("region");
  sized(region, content);
  region.scrollTop = 700;
  region.dispatchEvent(new Event("scroll"));
  content.height = 1400;
  view.rerender(<Region lines={["a", "b"]} />);
  await flush();
  expect(region.scrollTop).toBe(1400);
});

it("leaves the position alone after the reader scrolls up, until they send something", async () => {
  const content = { height: 1000 };
  const view = render(<Region lines={["a"]} followKey="u1" />);
  const region = view.getByTestId("region");
  sized(region, content);
  region.scrollTop = 200;
  region.dispatchEvent(new Event("scroll"));
  content.height = 1400;
  view.rerender(<Region lines={["a", "reply"]} followKey="u1" />);
  await flush();
  expect(region.scrollTop).toBe(200);
  content.height = 1600;
  view.rerender(<Region lines={["a", "reply", "my question"]} followKey="u2" />);
  await flush();
  expect(region.scrollTop).toBe(1600);
});
