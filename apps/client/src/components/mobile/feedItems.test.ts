import type { ConversationTurn } from "../../state/conversation";
import type { DeskReport } from "../ResearchDesk";
import { approvalMinutesLeft, buildMobileFeed, deskJudgmentCard, isPending } from "./feedItems";
import { judgmentChips } from "./notifyItems";

const report = (judgment: DeskReport["judgment"]): DeskReport => ({
  symbol: "SHFE.rb2701", productName: "螺纹钢", mainSymbol: "SHFE.rb2701", generatedAt: "2026-10-04T01:30:00Z",
  sections: [], facts: [], notice: "", judgment,
});

test("今日判断取自调研台：立场、置信、关键事实带出处；生成中与不能生成各有说明", () => {
  const card = deskJudgmentCard(report({
    status: "ready", today: true, stance: "偏多", confidence: "中", headline: "库存回落", generatedAt: "2026-10-04T01:30:00Z", researchDay: "2026-10-04",
    points: [{ text: "社会库存周降 4.2%", basis: ["f1"], kind: "推断", grounds: "data", quote: "周降4.2%" }],
    risks: [{ text: "跌破 3,080", basis: [], kind: "推测", grounds: "data" }],
    basis: { f1: { section: "库存", kind: "data", label: "社会库存", value: "512", source: "钢联", asOf: "2026-10-03" } },
  }));
  expect(card).toMatchObject({ status: "ready", stance: "偏多", confidence: "中", headline: "库存回落", symbol: "SHFE.rb2701", note: null,
    facts: [{ text: "社会库存周降 4.2%", quote: "周降4.2%", source: "钢联", asOf: "2026-10-03" }], invalidation: [{ text: "跌破 3,080" }] });
  expect(card.actions).toEqual([{ kind: "ask", label: "问 AI 怎么看", prompt: "螺纹钢今天怎么看？" }]);
  expect(deskJudgmentCard(report({ status: "pending" }))).toMatchObject({ status: "pending", note: "今日判断生成中，完成后会在这里更新。" });
  expect(deskJudgmentCard(report({ status: "budget_exhausted" }))).toMatchObject({ status: "unavailable" });
});

test("AI 动态按时间倒序合并对话卡片、调研判断与简报；同一张卡只留最新一版，对话里已有的当日判断不重复", () => {
  const judgment = { turnId: "t1", cardId: "j1", symbol: "SHFE.rb2701", productName: "螺纹钢", status: "ready" as const, researchDay: "2026-10-04", generatedAt: "2026-10-04T01:30:00Z" };
  const turns = [
    { id: "t1", role: "assistant", text: "", timestamp: "2026-10-04T01:30:00Z", cards: [{ id: "j1", type: "research-judgment", payload: judgment }] },
    { id: "t2", role: "assistant", text: "", timestamp: "2026-10-04T02:00:00Z", cards: [
      { id: "e1", type: "execution", payload: { cardId: "e1", status: "submitted", updatedAt: "2026-10-04T02:00:00Z" } }] },
    // The same execution card republished once filled: the feed keeps the latest form only.
    { id: "t3", role: "assistant", text: "", timestamp: "2026-10-04T02:01:00Z", cards: [
      { id: "e1", type: "execution", payload: { cardId: "e1", status: "filled", updatedAt: "2026-10-04T02:01:00Z" } }] },
    { id: "t4", role: "user", text: "hidden", timestamp: "2026-10-04T03:00:00Z", hidden: true, cards: [
      { id: "r9", type: "reminder", payload: { cardId: "r9", at: "2026-10-04T03:00:00Z" } }] },
  ] as unknown as ConversationTurn[];
  const briefs = [{ id: "brief:1", kind: "news" as const, time: Date.parse("2026-10-04T02:30:00Z"), title: "重要资讯", detail: "铁矿港口库存回落", source: "财联社" }];
  const items = buildMobileFeed({ turns, briefs, desks: [{ ...judgment, cardId: "desk:SHFE.rb2701" }] });
  expect(items.map(item => item.id)).toEqual(["brief:1", "e1", "j1"]);
  expect(items[1]).toMatchObject({ kind: "execution", card: { status: "filled" } });
  // Without the conversation's judgment the desk's shows.
  expect(buildMobileFeed({ turns: [], briefs: [], desks: [{ ...judgment, cardId: "desk:SHFE.rb2701" }] }).map(item => item.id.split("@")[0])).toEqual(["desk:SHFE.rb2701"]);
});

test("同品种同研究日的判断在时间线只留一条：对话里两张卡、合约写法不同的调研台读数都并成最新那张（线上重复回归）", () => {
  const judgment = (cardId: string, symbol: string, generatedAt: string) => ({ turnId: cardId, cardId, symbol, productName: "螺纹钢", status: "ready" as const,
    stance: "震荡" as const, researchDay: "2026-10-06", generatedAt });
  const turns = [
    { id: "t1", role: "assistant", text: "", timestamp: "2026-10-06T00:45:00Z", cards: [{ id: "morning", type: "research-judgment", payload: judgment("morning", "SHFE.rb2601", "2026-10-06T00:30:00Z") }] },
    { id: "t2", role: "assistant", text: "", timestamp: "2026-10-06T02:00:00Z", cards: [{ id: "asked", type: "research-judgment", payload: judgment("asked", "SHFE.rb2601", "2026-10-06T00:30:00Z") }] },
  ] as unknown as ConversationTurn[];
  const desks = [judgment("desk:SHFE.RB2601", "SHFE.RB2601", "2026-10-06T00:30:00Z"), judgment("desk:KQ.m@SHFE.rb", "KQ.m@SHFE.rb", "2026-10-06T00:30:00Z")];
  const items = buildMobileFeed({ turns, briefs: [], desks });
  expect(items.filter(item => item.kind === "judgment").map(item => item.id)).toEqual(["asked"]);
  // Another day's judgment of the same product is its own row.
  const yesterday = { ...judgment("desk:SHFE.rb2601", "SHFE.rb2601", "2026-10-05T00:30:00Z"), researchDay: "2026-10-05" };
  expect(buildMobileFeed({ turns, briefs: [], desks: [yesterday] }).map(item => item.id)).toEqual(["asked", "desk:SHFE.rb2601@2026-10-05@2026-10-05T00:30:00Z"]);
});

test("同日较新的调研台判断替换较旧的对话卡（#516）：胶囊与时间线显示新立场；较旧的调研台读数不覆盖较新的对话卡", () => {
  const chat = { turnId: "t1", cardId: "chat-1", symbol: "SHFE.rb2701", productName: "螺纹钢", status: "ready" as const, stance: "偏多" as const,
    researchDay: "2026-10-06", generatedAt: "2026-10-06T01:00:00Z" };
  const turns = [{ id: "t1", role: "assistant", text: "", timestamp: "2026-10-06T01:00:00Z", cards: [{ id: "chat-1", type: "research-judgment", payload: chat }] }] as unknown as ConversationTurn[];
  const desk = (generatedAt: string, stance: "偏多" | "偏空") => deskJudgmentCard(report({ status: "ready", today: true, stance, confidence: "中",
    headline: "", generatedAt, researchDay: "2026-10-06", points: [], risks: [], basis: {} }));
  const products = [{ product: "SHFE.rb", name: "螺纹钢" }];
  const now = Date.parse("2026-10-06T03:00:00Z");
  // The desk refreshed at 10:10 (02:10Z) to 偏空 after the chat's 09:00 偏多 card: the newer one shows.
  const newer = desk("2026-10-06T02:10:00Z", "偏空");
  for (const feed of [buildMobileFeed({ turns, briefs: [], desks: [newer] }), buildMobileFeed({ turns, briefs: [], desks: [newer, newer] })]) {
    expect(feed.filter(item => item.kind === "judgment").map(item => [item.id, item.kind === "judgment" && item.card.stance]))
      .toEqual([["desk:SHFE.rb2701@2026-10-06@2026-10-06T02:10:00Z", "偏空"]]);
    expect(judgmentChips(products, feed, { now })[0]).toMatchObject({ stance: "偏空", symbol: "SHFE.rb2701" });
  }
  // An older desk read never covers the newer conversation card.
  const older = buildMobileFeed({ turns, briefs: [], desks: [desk("2026-10-06T00:30:00Z", "偏空")] });
  expect(older.map(item => item.id)).toEqual(["chat-1"]);
  expect(judgmentChips(products, older, { now })[0].stance).toBe("偏多");
});

test("确认卡：确认有效期内（10 分钟）是待确认，过期、确认或放弃后回到时间线原来的时间位置", () => {
  const issued = "2026-10-06T02:00:00Z";
  const trade = (status: string, updatedAt: string) => ({ id: "t1", role: "assistant", text: "", timestamp: issued, cards: [
    { id: "p1", type: "trade", payload: { proposalId: "p1", status, updatedAt, side: "sell_close", name: "甲醇2701", symbol: "CZCE.MA701", volume: 3, limitPrice: 2380 } }] });
  const at = Date.parse(issued);
  const [card] = buildMobileFeed({ turns: [trade("approval_required", issued)] as unknown as ConversationTurn[], briefs: [] });
  expect(isPending(card, at + 60_000)).toBe(true);
  expect(approvalMinutesLeft(card, at + 60_000)).toBe(9);
  expect(isPending(card, at + 600_000)).toBe(false);
  // Confirmed later: the republished card keeps its issue time (no jump to the top of the timeline).
  const confirmed = buildMobileFeed({ turns: [trade("approval_required", issued), { ...trade("candidate_recorded", "2026-10-06T02:04:00Z"), id: "t2", timestamp: "2026-10-06T02:04:00Z" }] as unknown as ConversationTurn[], briefs: [] });
  expect(confirmed).toHaveLength(1);
  expect(confirmed[0].time).toBe(at);
  expect(isPending(confirmed[0], at + 60_000)).toBe(false);
});


test("调研台判断的行 id 带研究日与生成时间：读过旧版本不算读过新版本（#516）", () => {
  const card = (researchDay: string, generatedAt: string) => ({ turnId: "mobile-feed", cardId: "desk:SHFE.rb2701", symbol: "SHFE.rb2701", productName: "螺纹钢",
    status: "ready" as const, stance: "偏多" as const, researchDay, generatedAt, facts: [], invalidation: [], actions: [] });
  const ids = [card("2026-10-05", "2026-10-05T00:40:00Z"), card("2026-10-06", "2026-10-06T00:40:00Z"), card("2026-10-06", "2026-10-06T03:10:00Z")]
    .map(desk => buildMobileFeed({ turns: [], briefs: [], desks: [desk] })[0].id);
  expect(new Set(ids).size).toBe(3);
});
