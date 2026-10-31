/**
 * 按住说话的语音回复 (09-28): a push-to-talk message is answered out loud, in the call's voice.
 *
 * `expect()` runs when the held words are sent: it opens the Runtime's `/api/voice/speak` socket at
 * once (Doubao's session takes 1–2 s to start, hidden behind the agent's turn) and remembers which
 * answers already existed. The first new voice answer with a spoken summary is then handed to
 * `speak()`, whose audio frames play back to back. A new hold, a call or leaving the page `stop()`s it.
 * Playback is lifted (gain, then a limiter against clipping) and the page's audio session switched
 * back to "playback": after a hold phones read the reply at earpiece volume (10-05).
 * It plays through push-to-talk's AudioContext, which every hold resumes inside its press gesture:
 * `expect()` runs only once the final text is back, outside any gesture, and a context made there
 * stayed suspended on iPhone — the reply arrived and nothing was heard (10-05).
 */
import { localAuthTokenFor } from "./api";
import { setAudioSession } from "./micCapture";
import { pushToTalkStore } from "./pushToTalk";
import { VOICE_AUTH_PROTOCOL, VOICE_AUTH_TOKEN_PREFIX, decodeOutputAudio } from "./voiceRealtime";

const OUTPUT_RATE = 24000;
const START_DELAY = 0.08;
const REPLY_GAIN = 1.6;

class VoiceReply {
  private socket?: WebSocket;
  private outbox: string[] = [];
  private context?: AudioContext;
  /** gain → limiter → speaker, built once per context. */
  private output?: AudioNode;
  private outputContext?: AudioContext;
  private playhead = 0;
  private sources: AudioBufferSourceNode[] = [];
  /** Answers that existed when the message was sent; undefined when no spoken reply is awaited. */
  private seen?: Set<string>;

  get waiting(): boolean { return this.seen !== undefined; }

  expect(baseUrl: string, existingTurnIds: Iterable<string>) {
    this.stop();
    this.seen = new Set(existingTurnIds);
    setAudioSession("playback");
    this.context = pushToTalkStore.playbackContext();
    this.wake();
    const token = localAuthTokenFor(baseUrl);
    const url = baseUrl.replace(/^http/, "ws") + "/api/voice/speak";
    const socket = token ? new WebSocket(url, [VOICE_AUTH_PROTOCOL, VOICE_AUTH_TOKEN_PREFIX + token]) : new WebSocket(url);
    this.socket = socket;
    socket.onopen = () => { const queued = this.outbox; this.outbox = []; queued.forEach(item => socket.send(item)); };
    socket.onmessage = event => this.handle(event.data);
    socket.onclose = () => { if (this.socket === socket) { this.socket = undefined; this.seen = undefined; } };
  }

  /** The first answer that is new since `expect()`, spoken and marked voice, is read out once. */
  offer(turns: readonly { id: string; role: string; origin?: string; spoken?: string | null }[]) {
    const seen = this.seen;
    if (!seen) return;
    const answer = turns.find(turn => turn.role === "assistant" && !seen.has(turn.id) && turn.origin === "voice" && turn.spoken?.trim());
    if (!answer) return;
    this.seen = undefined;
    this.send(JSON.stringify({ type: "speak", text: answer.spoken!.trim() }));
  }

  /** Stop reading and drop the socket (a new hold, a call starting, the page closing). */
  stop() {
    this.seen = undefined;
    for (const source of this.sources) { try { source.stop(); } catch { /* already ended */ } }
    this.sources = []; this.playhead = 0; this.outbox = [];
    const socket = this.socket;
    this.socket = undefined;
    if (socket) {
      socket.onclose = null; socket.onmessage = null;
      if (socket.readyState === WebSocket.OPEN) { try { socket.send(JSON.stringify({ type: "cancel" })); socket.close(); } catch { /* closing anyway */ } }
      else if (socket.readyState === WebSocket.CONNECTING) socket.onopen = () => { try { socket.close(); } catch { /* closing anyway */ } };
    }
  }

  private send(item: string) {
    const socket = this.socket;
    if (socket?.readyState === WebSocket.OPEN) socket.send(item);
    else if (socket?.readyState === WebSocket.CONNECTING) this.outbox.push(item);
  }

  private handle(raw: unknown) {
    if (typeof raw !== "string") return;
    let event: { type?: string; data?: string; encoding?: "f32le" | "s16le" };
    try { event = JSON.parse(raw); } catch { return; }
    if (event.type === "audio" && event.data) this.play(event.data, event.encoding);
  }

  private play(base64: string, encoding: "f32le" | "s16le" = "f32le") {
    const context = this.context;
    if (!context) return;
    this.wake();
    const samples = decodeOutputAudio(Uint8Array.from(atob(base64), c => c.charCodeAt(0)), encoding);
    if (!samples.length) return;
    const buffer = context.createBuffer(1, samples.length, OUTPUT_RATE);
    buffer.getChannelData(0).set(samples);
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.outputFor(context));
    const at = Math.max(this.playhead, context.currentTime + START_DELAY);
    source.start(at);
    this.playhead = at + buffer.duration;
    this.sources.push(source);
    source.onended = () => { this.sources = this.sources.filter(item => item !== source); };
  }

  /** Safari parks the context ("interrupted") when the audio session switches; one unlocked by a
   *  gesture may resume again without one. */
  private wake() {
    if (this.context && this.context.state !== "running") void this.context.resume().catch(() => undefined);
  }

  private outputFor(context: AudioContext): AudioNode {
    if (!this.output || this.outputContext !== context) {
      const gain = context.createGain();
      gain.gain.value = REPLY_GAIN;
      const limiter = context.createDynamicsCompressor();
      limiter.threshold.value = -3; limiter.knee.value = 0; limiter.ratio.value = 20;
      limiter.attack.value = 0.003; limiter.release.value = 0.25;
      gain.connect(limiter);
      limiter.connect(context.destination);
      this.output = gain;
      this.outputContext = context;
    }
    return this.output;
  }
}

export const voiceReply = new VoiceReply();
