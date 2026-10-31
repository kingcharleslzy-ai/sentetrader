/**
 * Microphone → 16 kHz mono PCM16 in 20 ms frames: the capture the voice call and push-to-talk
 * (按住说话) share. The caller gets each frame (`onFrame`) and the raw device-rate samples
 * (`onSamples`, for loudness); the AudioContext is returned so a call can play through it too.
 * Push-to-talk passes in its own long-lived context (created inside the press gesture); a call lets
 * the capture make one and close it at the end.
 */
export const INPUT_RATE = 16000;
const FRAME_SAMPLES = 320; // 20 ms at 16 kHz

export class MicCapture {
  private stream?: MediaStream;
  private context?: AudioContext;
  /** False when the context was passed in: stop() leaves it open for the next hold. */
  private ownsContext = false;
  private processor?: ScriptProcessorNode;
  private source?: MediaStreamAudioSourceNode;
  private meter?: AnalyserNode;
  private pending: Float32Array[] = [];
  private pendingSamples = 0;

  constructor(private readonly onFrame: (frame: Int16Array) => void, private readonly onSamples?: (samples: Float32Array) => void) {}

  /** Opens the microphone; throws the getUserMedia error (see `microphoneError`) when it cannot.
   *  `shared` is a context the caller already resumed inside its gesture; it is used as is. */
  async start(shared?: AudioContext): Promise<AudioContext> {
    if (!navigator.mediaDevices?.getUserMedia) throw new DOMException("mediaDevices unavailable", "SecurityError");
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, sampleRate: INPUT_RATE, echoCancellation: true, noiseSuppression: true } });
    this.ownsContext = !shared;
    this.context = shared ?? new AudioContext();
    if (!shared) await this.context.resume().catch(() => undefined);
    const source = this.context.createMediaStreamSource(this.stream);
    this.source = source;
    // ScriptProcessor keeps this dependency-free; 20 ms frames are cut here regardless of the buffer size.
    this.processor = this.context.createScriptProcessor(1024, 1, 1);
    this.processor.onaudioprocess = event => this.push(event.inputBuffer.getChannelData(0), event.inputBuffer.sampleRate);
    source.connect(this.processor);
    this.processor.connect(this.context.destination);
    return this.context;
  }

  /** Device samples in, 20 ms PCM16 frames out (resampled to 16 kHz). */
  push(samples: Float32Array, rate: number) {
    this.onSamples?.(samples);
    const resampled = rate === INPUT_RATE ? samples : downsample(samples, rate, INPUT_RATE);
    this.pending.push(Float32Array.from(resampled));
    this.pendingSamples += resampled.length;
    while (this.pendingSamples >= FRAME_SAMPLES) {
      const frame = new Int16Array(FRAME_SAMPLES);
      let filled = 0;
      while (filled < FRAME_SAMPLES) {
        const head = this.pending[0];
        const take = Math.min(head.length, FRAME_SAMPLES - filled);
        for (let i = 0; i < take; i += 1) frame[filled + i] = Math.max(-32768, Math.min(32767, Math.round(head[i] * 32767)));
        filled += take;
        if (take === head.length) this.pending.shift(); else this.pending[0] = head.subarray(take);
      }
      this.pendingSamples -= FRAME_SAMPLES;
      this.onFrame(frame);
    }
  }

  /** A tap on the microphone for the call bar's waveform: an AnalyserNode on the same source,
   *  created on first use. It only listens — the frames sent upstream are unaffected. */
  analyser(): AnalyserNode | undefined {
    if (!this.meter && this.context && this.source) {
      this.meter = this.context.createAnalyser();
      this.meter.fftSize = 512;
      this.source.connect(this.meter);
    }
    return this.meter;
  }

  /** The opened input for the call's diagnostics (see `describeMicrophone`); undefined before start. */
  deviceInfo(): MicDeviceInfo | undefined {
    const track = this.stream?.getAudioTracks?.()[0];
    return track ? describeMicrophone(track, this.context) : undefined;
  }

  /** Mute keeps the device open: the tracks just stop delivering sound. */
  setEnabled(enabled: boolean) { this.stream?.getAudioTracks().forEach(track => { track.enabled = enabled; }); }

  stop() {
    if (this.processor) { this.processor.onaudioprocess = null; this.processor.disconnect(); this.processor = undefined; }
    this.meter?.disconnect(); this.meter = undefined;
    this.source?.disconnect(); this.source = undefined;
    this.stream?.getTracks().forEach(track => track.stop()); this.stream = undefined;
    if (this.ownsContext) void this.context?.close();
    this.context = undefined;
    this.pending = []; this.pendingSamples = 0;
  }
}

/**
 * The page's audio session where the browser has one (`navigator.audioSession`, Safari 16.4+):
 * "play-and-record" while the microphone is open, "playback" otherwise. After a hold iOS kept the
 * page in record mode, so the spoken reply came out at earpiece volume (10-05). Elsewhere a no-op.
 */
export function setAudioSession(type: "playback" | "play-and-record") {
  const session = (navigator as Navigator & { audioSession?: { type: string } }).audioSession;
  if (!session) return;
  try { session.type = type; } catch { /* not switchable here */ }
}

/** What the browser actually opened (10-14: a Windows web call sent nothing Doubao could hear — the
 *  wrong input device is the first suspect): the device label, the track's own settings and the
 *  AudioContext. A label's owner name ("张三的 AirPods", "Ann's AirPods") is dropped, and the deviceId
 *  is cut to a short prefix (Windows' "default" / "communications" stay whole: they say which entry). */
export interface MicDeviceInfo {
  label: string;
  deviceId: string;
  sampleRate?: number;
  channelCount?: number;
  echoCancellation?: boolean;
  noiseSuppression?: boolean;
  autoGainControl?: boolean;
  /** The browser or the OS silenced the source (an OS-level mute, a device that delivers nothing). */
  trackMuted?: boolean;
  readyState?: string;
  contextRate?: number;
  contextState?: string;
}

export function describeMicrophone(track: MediaStreamTrack, context?: AudioContext): MicDeviceInfo {
  const settings: MediaTrackSettings = typeof track.getSettings === "function" ? track.getSettings() : {};
  const id = settings.deviceId ?? "";
  return {
    label: microphoneLabel(track.label),
    deviceId: ["", "default", "communications"].includes(id) ? id : `${id.slice(0, 6)}…`,
    sampleRate: settings.sampleRate, channelCount: settings.channelCount,
    echoCancellation: settings.echoCancellation, noiseSuppression: settings.noiseSuppression, autoGainControl: settings.autoGainControl,
    trackMuted: track.muted, readyState: track.readyState,
    contextRate: context?.sampleRate, contextState: context?.state,
  };
}

/** A device label as shown and logged: an owner name before "'s" / "的" dropped — at the start or
 *  right inside the brackets ("Headset (Ann's AirPods Hands-Free)") — at most 80 characters. */
export function microphoneLabel(label: string | undefined): string {
  return (label ?? "").replace(/^[^()（）]{1,40}?(?:'s|’s|的)\s*/u, "")
    .replace(/([(（])\s*[^()（）]{1,40}?(?:'s|’s|的)\s*/gu, "$1").slice(0, 80);
}

// Windows splits one Bluetooth headset into a listen-only "Headphones / 耳机 (… Stereo / 立体声)" entry
// and a "Headset (… Hands-Free AG Audio / 免提)" entry that carries the microphone; a browser on the
// former records pure silence (所有者 10-14). These words are stripped to find the same headset's entries.
const HANDS_FREE = /hands-?\s?free|免提|headset|耳麦/i;
const ROLE_WORDS = /^(?:default|communications|默认|通讯)\s*-\s*|hands-?\s?free|ag audio|headphones?|headset|stereo|microphone|免提|立体声|耳机|耳麦|麦克风/gi;
const deviceFamily = (label: string) => label.replace(ROLE_WORDS, "").toLowerCase().replace(/[^a-z0-9\u4e00-\u9fa5]/g, "");

/** The same headset's Hands-Free / 免提 / Headset input when the current microphone is not it
 *  (the 没有收到麦克风声音 hint names it); undefined when there is none or the current one already is. */
export function handsFreeSuggestion(current: string, devices: { kind: string; label: string }[]): string | undefined {
  if (!current || HANDS_FREE.test(current)) return undefined;
  const family = deviceFamily(current);
  const match = devices.map(device => ({ kind: device.kind, label: microphoneLabel(device.label) }))
    .find(device => device.kind === "audioinput" && device.label && device.label !== current
      && !/^(?:default|communications|默认|通讯)\s*-/i.test(device.label)
      && HANDS_FREE.test(device.label) && deviceFamily(device.label) === family);
  return match?.label;
}

/** Root-mean-square of the samples (speech ≈ 0.04–0.2): the call's barge-in duck and orb halo, the push-to-talk meter. */
export function rootMeanSquare(samples: Float32Array): number {
  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
  return Math.sqrt(sum / Math.max(1, samples.length));
}

function downsample(samples: Float32Array, from: number, to: number): Float32Array {
  const ratio = from / to;
  const length = Math.floor(samples.length / ratio);
  const out = new Float32Array(length);
  for (let i = 0; i < length; i += 1) {
    const start = Math.floor(i * ratio), end = Math.min(samples.length, Math.floor((i + 1) * ratio));
    let sum = 0; let n = 0;
    for (let j = start; j < end; j += 1) { sum += samples[j]; n += 1; }
    out[i] = n ? sum / n : 0;
  }
  return out;
}
