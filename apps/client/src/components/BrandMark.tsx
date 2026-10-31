/** The Sente mark at small sizes (≤64 px): the stone on the board and the blue "next move" dot —
 *  the simplified form of the app icon. Brand artwork, so its colours are fixed in both themes. */
export function BrandMark({ size = 24 }: { size?: number }) {
  return (
    <svg className="brand-mark" width={size} height={size} viewBox="0 0 64 64" aria-hidden="true">
      <rect x="1" y="1" width="62" height="62" rx="13" fill="var(--brand-mark-bg)" stroke="var(--brand-mark-line)" strokeWidth="2" />
      <circle cx="27" cy="38" r="13" fill="var(--brand-mark-ink)" />
      <circle cx="46" cy="19" r="7" fill="var(--brand-mark-dot)" />
    </svg>
  );
}
