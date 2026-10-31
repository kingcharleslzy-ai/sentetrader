import { Star } from "lucide-react";

/**
 * 自选品种 (2026-10-02): the one star the board rows, the contract drawer's product rows and the
 * contract head share — it stars a product (not a month), so the board's 自选 filter follows the
 * main contract across roll-overs. Clicking it never triggers the row around it.
 */
export function WatchlistStar({ product, name, starred, onToggle }: {
  /** "<exchangeId>.<productId>", the watchlist entry's key. */
  product: string;
  /** The product's display name, so the label says which product the star marks. */
  name: string;
  starred: boolean;
  onToggle: (product: string) => void;
}) {
  const action = starred ? "移出自选" : "加入自选";
  return (
    <button type="button" className="fdw-watch-star" aria-pressed={starred} aria-label={`${action} ${name}`} title={action}
      onClick={event => { event.stopPropagation(); onToggle(product); }}>
      <Star aria-hidden="true" fill={starred ? "currentColor" : "none"} />
    </button>
  );
}
