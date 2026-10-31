import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { openExternal } from "../../externalLinks";
import { useDisclosureSurface } from "../../uiSurfaces";
import { SettingRow, SettingsSection } from "../SettingsPanel";
import { Button, Segmented, Switch } from "../settings/controls";
import {
  composeFeedback, environmentRows, FEEDBACK_KINDS, FEEDBACK_RECIPIENT, feedbackDiagnostics, feedbackMailto, feedbackPlainText,
  readFeedbackBrowser, type FeedbackEnvironment, type FeedbackKind,
} from "./feedback";
import "./FeedbackSettings.css";

/** Grow a read-only text to its content, so the page keeps its one scroll region. */
function useFitHeight(value: string) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const fit = () => {
      const area = ref.current;
      if (!area || area.offsetParent === null) return;
      area.style.height = "auto";
      area.style.height = `${area.scrollHeight + 2}px`;
    };
    fit();
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, [value]);
  return ref;
}

/**
 * 设置 → 关于 → 问题与建议 (2026-10-06): a feedback mail draft to the fixed support address. The
 * user's click opens their own mail program (or copies the text); nothing is stored, sent or
 * uploaded by the app. Typed text and the full draft are agent-private.
 */
export function FeedbackSettings({ environment }: { environment: FeedbackEnvironment }) {
  const [kind, setKind] = useState<FeedbackKind>("problem");
  const [description, setDescription] = useState("");
  const [expectation, setExpectation] = useState("");
  const [diagnostics, setDiagnostics] = useState(true);
  const [occurredAt] = useState(() => new Date());
  const [invalid, setInvalid] = useState(false);
  const [status, setStatus] = useState("");
  const [draftShown, setDraftShown] = useState(false);
  const [manualShown, setManualShown] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  useDisclosureSurface("feedback-diagnostics", { available: diagnostics, state: previewOpen, apply: setPreviewOpen });
  const descriptionRef = useRef<HTMLTextAreaElement>(null);
  const draftSection = useRef<HTMLDivElement>(null);
  const ids = useId();
  const meta = FEEDBACK_KINDS[kind];
  const diagnosticsText = diagnostics ? feedbackDiagnostics(environment, readFeedbackBrowser(), occurredAt) : null;
  const draft = composeFeedback({ kind, description, expectation, env: environment, occurredAt, diagnostics: diagnosticsText });
  const plainText = feedbackPlainText(draft);
  const valid = description.trim().length > 0;
  const draftBody = useFitHeight(draftShown && valid ? draft.body : "");
  const manualText = useFitHeight(manualShown ? plainText : "");
  // An emptied description withdraws the draft it no longer describes.
  useEffect(() => { if (!valid) setDraftShown(false); }, [valid]);

  const validate = () => {
    setInvalid(!valid);
    if (!valid) {
      setStatus("");
      descriptionRef.current?.focus();
      descriptionRef.current?.scrollIntoView?.({ block: "center", behavior: "smooth" });
    }
    return valid;
  };
  const edited = (update: () => void) => { update(); setStatus(""); };

  const openDraft = async () => {
    if (!validate()) return;
    setDraftShown(true);
    try {
      await openExternal(feedbackMailto(draft));
      // Only a request: the mail program decides; sending stays the user's own step.
      setStatus("已请求打开，请在邮件中确认并发送");
    } catch {
      setStatus("没能打开邮件程序，请复制反馈内容，或在下方草稿里手动选取");
    }
    requestAnimationFrame(() => draftSection.current?.scrollIntoView?.({ block: "start", behavior: "smooth" }));
  };

  const copy = async () => {
    if (!validate()) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(plainText);
      setManualShown(false);
      setStatus("已复制到剪贴板");
    } catch {
      setStatus("");
      setManualShown(true);
      requestAnimationFrame(() => { manualText.current?.focus(); manualText.current?.select(); });
    }
  };

  return <>
    <SettingsSection title="反馈内容">
      <SettingRow label="类别">
        <Segmented label="反馈类别" value={kind} options={[{ value: "problem", label: FEEDBACK_KINDS.problem.label }, { value: "idea", label: FEEDBACK_KINDS.idea.label }] as const}
          onChange={value => edited(() => setKind(value))} />
      </SettingRow>
      <div className="fb-field">
        <div className="fb-field-head"><label id={`${ids}-desc-label`} htmlFor={`${ids}-desc`}>{meta.field}</label><span className="fb-tag">必填</span></div>
        <textarea ref={descriptionRef} id={`${ids}-desc`} aria-labelledby={`${ids}-desc-label`} className="app-textarea" data-agent-private aria-required="true" aria-invalid={invalid}
          aria-describedby={invalid ? `${ids}-desc-error` : undefined} placeholder={meta.placeholder} value={description}
          onChange={event => edited(() => { setDescription(event.target.value); if (event.target.value.trim()) setInvalid(false); })} />
        {invalid ? <p className="fb-error" id={`${ids}-desc-error`} role="alert">{meta.empty}</p> : null}
      </div>
      <div className="fb-field">
        <div className="fb-field-head"><label id={`${ids}-expect-label`} htmlFor={`${ids}-expect`}>期待的结果</label><span className="fb-tag">选填</span></div>
        <textarea id={`${ids}-expect`} aria-labelledby={`${ids}-expect-label`} className="app-textarea short" data-agent-private placeholder={meta.expect} value={expectation}
          onChange={event => edited(() => setExpectation(event.target.value))} />
      </div>
    </SettingsSection>

    <SettingsSection title="自动附带" helpKey="feedback-attachments" help="写在邮件正文末尾，发送前可在邮件里修改">
      <dl className="fb-env">
        {environmentRows(environment, occurredAt).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
      </dl>
      <SettingRow label="附上基础诊断" helpKey="feedback-diagnostics" help="仅环境与连接状态；不含原始日志、语音文本、账号或交易信息">
        <Switch label="附上基础诊断" checked={diagnostics} on="开" off="关" onChange={value => edited(() => setDiagnostics(value))} />
      </SettingRow>
      {diagnosticsText ? <details className="fb-diag" open={previewOpen}>
        <summary onClick={event => { event.preventDefault(); setPreviewOpen(open => !open); }}>预览诊断内容</summary>
        <pre className="fb-pre">{diagnosticsText}</pre>
      </details> : null}
    </SettingsSection>

    <SettingsSection title="发送">
      <SettingRow label="收件邮箱"><strong>{FEEDBACK_RECIPIENT}</strong></SettingRow>
      <div className="fb-actions">
        <p className="fb-note">截图或日志可在邮件里自行添加</p>
        <span className="app-setting-status" role="status">{status}</span>
        <Button onClick={() => void copy()}>复制反馈内容</Button>
        <Button primary onClick={() => void openDraft()}>打开邮件草稿</Button>
      </div>
      {manualShown ? <div className="fb-manual">
        <p>没能自动复制，请手动选取下面的文字</p>
        <textarea ref={manualText} className="app-textarea fb-fit" readOnly data-agent-private aria-label="反馈内容（手动复制）" value={plainText} />
      </div> : null}
    </SettingsSection>

    {draftShown && valid ? <div ref={draftSection}>
      <SettingsSection title="邮件草稿" hint={<span className="fb-pill">这只是草稿，尚未发送</span>}>
        <SettingRow label="收件人"><strong>{FEEDBACK_RECIPIENT}</strong></SettingRow>
        <SettingRow label="主题"><strong>{draft.subject}</strong></SettingRow>
        <div className="fb-field">
          <div className="fb-field-head"><label htmlFor={`${ids}-draft`}>正文</label><span className="fb-tag">只读，可手动选取复制</span></div>
          <textarea ref={draftBody} id={`${ids}-draft`} className="app-textarea fb-fit" readOnly data-agent-private value={draft.body} />
        </div>
      </SettingsSection>
    </div> : null}
  </>;
}
