/** ATVV 1.0 / IMA ADPCM, based on MiRemoteVoice (MIT); see THIRD_PARTY_NOTICES.md. */
export const ATVV_SERVICE = "ab5e0001-5a21-4f05-bc7d-af01f617b664";
export const ATVV_TX = "ab5e0002-5a21-4f05-bc7d-af01f617b664";
export const ATVV_AUDIO = "ab5e0003-5a21-4f05-bc7d-af01f617b664";
export const ATVV_CONTROL = "ab5e0004-5a21-4f05-bc7d-af01f617b664";
export const GET_CAPS = new Uint8Array([10, 1, 0, 0, 3, 3]);
const INDEX = [-1, -1, -1, -1, 2, 4, 6, 8];
const STEP = [7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,107,118,130,143,157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,2272,2494,2740,3008,3307,3638,4002,4402,4842,5327,5860,6446,7091,7800,8580,9438,10382,11420,12562,13818,15200,16720,18392,20231,22254,24479,26927,29620,32767];

export class AtvvDecoder {
  private predictor = 0;
  private step = 0;
  private pendingSync = false;
  active = false;
  ready = false;
  streamId = 0;
  frameSize = 0;

  control(bytes: Uint8Array): "ready" | "start" | "stop" | "search" | undefined {
    if (bytes[0] === 11) {
      if (bytes.length < 7 || bytes[1] !== 1 || bytes[2] !== 0) throw new Error("遥控器 ATVV 版本不支持");
      const codecs = (bytes[3] & 3) ? bytes[3] : bytes[4];
      if (!(codecs & 2)) throw new Error("遥控器不支持 16 kHz 语音");
      this.frameSize = (bytes[5] << 8) | bytes[6];
      if (!this.frameSize || this.frameSize > 512) throw new Error("遥控器音频帧大小无效");
      this.ready = true;
      return "ready";
    }
    if (!this.ready) return;
    if (bytes[0] === 4) {
      if (bytes.length < 4 || bytes[2] !== 2 || this.active) throw new Error("遥控器语音开始数据无效");
      if (!this.pendingSync) this.predictor = this.step = 0;
      this.pendingSync = false;
      this.active = true;
      this.streamId = bytes[3];
      return "start";
    }
    if (bytes[0] === 10) {
      if (bytes.length < 7 || bytes[1] !== 2 || bytes[6] > 88) throw new Error("遥控器音频同步数据无效");
      this.predictor = (bytes[4] << 24 >> 16) | bytes[5];
      this.step = bytes[6];
      this.pendingSync = !this.active;
    }
    if (bytes[0] === 0) {
      this.pendingSync = false;
      if (!this.active) return;
      this.active = false;
      return "stop";
    }
    if (bytes[0] === 8) { this.predictor = this.step = 0; this.pendingSync = false; return "search"; }
    if (bytes[0] === 12) throw new Error("遥控器麦克风启动失败");
  }

  decode(bytes: Uint8Array): Int16Array | undefined {
    if (!this.active) return;
    if (bytes.length !== this.frameSize) throw new Error("遥控器音频帧不完整，请重试");
    const pcm = new Int16Array(bytes.length * 2);
    let at = 0;
    for (const byte of bytes) for (const nibble of [byte >> 4, byte & 15]) {
      const step = STEP[this.step];
      let diff = step >> 3;
      if (nibble & 4) diff += step;
      if (nibble & 2) diff += step >> 1;
      if (nibble & 1) diff += step >> 2;
      this.predictor = Math.max(-32768, Math.min(32767, this.predictor + ((nibble & 8) ? -diff : diff)));
      this.step = Math.max(0, Math.min(88, this.step + INDEX[nibble & 7]));
      pcm[at++] = this.predictor;
    }
    return pcm;
  }
}
