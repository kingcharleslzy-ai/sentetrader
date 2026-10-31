import { useEffect, useRef, useState, type FormEvent } from "react";
import type { FuturesWorkspacePromptContext, MessageExtras } from "../../api";
import { usePersistentDraft } from "../../hooks/usePersistentDraft";
import type { FuturesDecisionWorkspaceProps } from "./props";

interface ChatInput {
  workspace: FuturesDecisionWorkspaceProps;
  /** The workspace snapshot that rides with every prompt. */
  promptContext: () => FuturesWorkspacePromptContext | undefined;
}

/** Prompt draft, sending state and chat switching/history for the AI dock. */
export function useWorkspaceChat({ workspace, promptContext }: ChatInput) {
  const [prompt, setPrompt, promptSaveError] = usePersistentDraft(workspace.workspaceScope ? `ai-trader-chat-draft:${workspace.workspaceScope}` : undefined, "");
  const [promptError, setPromptError] = useState("");
  const [sendingPrompt, setSendingPrompt] = useState(false);
  const [chatHistory, setChatHistory] = useState<{sessionId: string; createdAt: string}[] | undefined>();
  const [chatError, setChatError] = useState("");
  const chatScope = useRef(workspace.workspaceScope);
  chatScope.current = workspace.workspaceScope;
  // Only a switch already in flight or a prompt still being sent blocks the chat buttons. Server
  // sessions and runs are independent of the session the UI shows, so switching never cancels an
  // old turn — and "last assistant turn not completed" / "a task card still running" must not lock
  // 新对话 / 历史对话 either: on a suspended mobile tab a missed message.completed or a stale
  // running card stays true for the life of the session (8bc211b was not enough).
  const chatBlocked = Boolean(workspace.chatBusy || sendingPrompt);
  const selectChat = async (id?: string) => {
    if (chatBlocked) return;
    setChatError("");
    try { await workspace.onChangeChat?.(id); setChatHistory(undefined); setPrompt(""); }
    catch { setChatError("对话切换未完成，当前对话已保留，请重试。"); }
  };
  const toggleHistory = async () => {
    if (chatHistory) { setChatHistory(undefined); return; }
    const scope = workspace.workspaceScope;
    try {
      const result = await workspace.onListChats?.();
      if (chatScope.current === scope) { setChatHistory(result?.items || []); setChatError(""); }
    } catch { if (chatScope.current === scope) setChatError("历史对话读取失败，请重试。"); }
  };
  useEffect(() => { setChatHistory(undefined); setChatError(""); }, [workspace.workspaceScope]);

  const submitPrompt = async (event: FormEvent) => {
    event.preventDefault();
    const text = prompt.trim();
    if (!text) return;
    if (sendingPrompt) return;
    setSendingPrompt(true); setPromptError("");
    try {
      await workspace.onPrompt(text, promptContext());
      setPrompt(current => current.trim() === text ? "" : current);
    } catch { setPromptError("发送未确认，原文已保留，请检查对话后再重试。"); }
    finally { setSendingPrompt(false); }
  };

  /** A quick-skill chip's fixed prompt: the same send path as the composer (the
   *  user's own turn), the draft stays untouched. An object's "AI ▾" menu adds its `focus`
   *  to this turn's workspace context. */
  const sendQuickPrompt = async (text: string, focus?: FuturesWorkspacePromptContext["focus"], extras?: MessageExtras) => {
    const trimmed = text.trim();
    if (!trimmed || sendingPrompt) return;
    setSendingPrompt(true); setPromptError("");
    const context = promptContext();
    const withFocus = context && focus ? { ...context, focus } : context;
    try { await (extras ? workspace.onPrompt(trimmed, withFocus, extras) : workspace.onPrompt(trimmed, withFocus)); }
    catch { setPromptError("发送未确认，请检查对话后再重试。"); }
    finally { setSendingPrompt(false); }
  };

  return { prompt, setPrompt, promptError, promptSaveError, sendingPrompt, chatHistory, chatError, chatBlocked, selectChat, toggleHistory,
    submitPrompt, sendQuickPrompt };
}

export type WorkspaceChat = ReturnType<typeof useWorkspaceChat>;
