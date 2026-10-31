import {
  Anvil, Apple, Atom, Banknote, Bean, Beef, Cable, CandlestickChart, Candy, ChartBar, CircleDot, Coins, Cylinder,
  Droplets, Egg, Factory, Flame, FlaskConical, Fuel, Gem, Landmark, Layers, Mountain, Package, Pickaxe, Shirt, Ship,
  Sprout, TrendingUp, Trees, Wheat, Zap, type LucideIcon
} from "lucide-react";

/**
 * One line icon per product family, drawn from lucide so it matches the rest of
 * the header and follows the tab's text colour. Keys are product ids as the
 * instrument catalog spells them (compared case-insensitively); foreign
 * products ("CBOT.ZC") also match on the part after the dot.
 */
const ICONS: Record<string, LucideIcon> = {
  // 黑色系
  rb: Anvil, hc: Layers, wr: Anvil, ss: Layers, i: Pickaxe, j: Mountain, jm: Mountain, zc: Mountain, sm: Factory, sf: Factory,
  // 贵金属
  au: Coins, ag: Gem, mgc: Coins, gc: Coins, si: Gem, sil: Gem, pl: Gem, pa: Gem, pt: Gem, pd: Gem,
  // 有色与新能源金属
  cu: Cable, al: CircleDot, zn: CircleDot, pb: CircleDot, ni: CircleDot, sn: CircleDot, bc: Cable, ao: CircleDot, ad: CircleDot,
  lc: Zap, ps: Atom, hg: Cable, ah: CircleDot, ca: Cable, zs: CircleDot,
  // 能源
  sc: Fuel, fu: Fuel, lu: Fuel, bu: Fuel, pg: Flame, ng: Flame, cl: Fuel, mcl: Fuel, qm: Fuel, ho: Fuel, qg: Flame, ec: Ship,
  // 化工
  eg: FlaskConical, eb: FlaskConical, pp: FlaskConical, l: FlaskConical, v: FlaskConical, ta: FlaskConical, ma: Droplets, pf: FlaskConical,
  px: FlaskConical, pr: FlaskConical, sa: FlaskConical, ur: FlaskConical, fg: Cylinder, sh: FlaskConical, br: Cylinder, ru: Trees, nr: Trees,
  sp: Package, bz: FlaskConical, lg: Trees, fb: Package, bb: Package, op: Droplets, fef: Pickaxe, tf: Layers,
  // 农产品
  a: Bean, b: Bean, m: Bean, y: Droplets, p: Droplets, c: Wheat, cs: Wheat, rr: Wheat, oi: Droplets, rm: Bean, rs: Sprout,
  cf: Shirt, cy: Shirt, sr: Candy, ap: Apple, cj: Apple, pk: Bean, wh: Wheat, pm: Wheat, ri: Wheat, jr: Wheat, ct: Shirt,
  zw: Wheat, ke: Wheat, zm: Bean, zl: Droplets, cn: Trees, uc: Trees, mca: Wheat, mcs: Bean, mch: Wheat,
  // 畜牧
  lh: Beef, jd: Egg,
  // 股指
  if: TrendingUp, ih: TrendingUp, ic: TrendingUp, im: TrendingUp, es: ChartBar, mes: ChartBar, nq: ChartBar, mnq: ChartBar,
  ym: ChartBar, mym: ChartBar, rty: ChartBar, m2k: ChartBar, niy: ChartBar, hsi: ChartBar, mhi: ChartBar, hhi: ChartBar,
  fdax: ChartBar, fdxm: ChartBar, fesx: ChartBar, vx: TrendingUp, cus: Banknote,
  // 国债与利率
  t: Landmark, ts: Landmark, tl: Landmark, zb: Landmark, zf: Landmark, zt: Landmark,
  fgbl: Landmark, fgbm: Landmark, fgbs: Landmark,
  // 外汇
  "6a": Banknote, "6b": Banknote, "6c": Banknote, "6e": Banknote, "6j": Banknote, "6n": Banknote, "6s": Banknote, dx: Banknote
};

/** 国债 CFFEX.TF / 美债 CBOT.ZN share their letters with SGX 铁矿 TF / 沪锌 zn; the exchange decides. */
const BY_EXCHANGE: Record<string, LucideIcon> = { "cffex.tf": Landmark, "cbot.zn": Landmark, "sgx.tf": Pickaxe, "shfe.zn": CircleDot };

export function productIcon(productId: string | undefined, exchangeId?: string): LucideIcon {
  if (!productId) return CandlestickChart;
  const raw = productId.toLowerCase();
  // "CBOT.ZN" carries its own exchange; otherwise the caller's exchange scopes the id.
  const scoped = raw.includes(".") ? raw : exchangeId ? `${exchangeId.toLowerCase()}.${raw}` : undefined;
  if (scoped && BY_EXCHANGE[scoped]) return BY_EXCHANGE[scoped];
  const short = raw.includes(".") ? raw.split(".").pop()! : raw;
  return ICONS[raw] ?? ICONS[short] ?? CandlestickChart;
}
