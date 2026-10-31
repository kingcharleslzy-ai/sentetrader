import { useLayoutEffect, useRef, useState } from "react";

export function RailIndicator({selection}: {selection: string}) {
  const marker = useRef<HTMLSpanElement>(null);
  const [position, setPosition] = useState({top: 0, height: 0, visible: false});
  useLayoutEffect(() => {
    const rail = marker.current?.parentElement;
    if (!rail) return;
    const measure = () => {
      const active = rail.querySelector<HTMLButtonElement>("button.active");
      setPosition(previous => active ? {top: active.offsetTop + 10, height: Math.max(0, active.offsetHeight - 20), visible: true} : {...previous, visible: false});
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(rail);
    rail.querySelectorAll("button").forEach(button => observer.observe(button));
    return () => observer.disconnect();
  }, [selection]);
  return <span ref={marker} className="fdw-rail-indicator" aria-hidden="true" style={{height:position.height, transform:`translateY(${position.top}px)`, opacity:position.visible?1:0}}/>;
}
