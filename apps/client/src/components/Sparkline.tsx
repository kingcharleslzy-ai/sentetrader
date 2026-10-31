interface SparklineProps {
  values: number[];
  className?: string;
  label: string;
}

function points(values: number[], width: number, height: number, padding: number) {
  if (values.length < 2) return "";
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const range = maximum - minimum || 1;
  return values
    .map((value, index) => {
      const x = padding + (index / (values.length - 1)) * (width - padding * 2);
      const y = height - padding - ((value - minimum) / range) * (height - padding * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

export function Sparkline({ values, className = "mini-chart", label }: SparklineProps) {
  const polyline = points(values, 126, 42, 2);
  const area = polyline ? `M ${polyline.replaceAll(" ", " L ")} L 124,42 L 2,42 Z` : "";
  return (
    <svg className={className} viewBox="0 0 126 42" role="img" aria-label={label}>
      <defs>
        <linearGradient id={`spark-${label}`} x1="0" y1="0" x2="0" y2="1">
          <stop stopColor="var(--accent)" stopOpacity=".18" />
          <stop offset="1" stopColor="var(--accent)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#spark-${label})`} />
      <polyline
        points={polyline}
        fill="none"
        stroke="var(--accent)"
        strokeWidth="2.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
