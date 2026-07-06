import { useEffect, useRef, useState } from "react";
import { fetchModels, openTtsStream, ttsFile, TtsQos } from "../api/client";
import { StreamingPcmPlayer } from "../audio/player";
import { QosBadge } from "../components/QosBadge";

export default function TtsPage() {
  const [text, setText] = useState("你好，欢迎使用语音大模型合成服务。");
  const [voice, setVoice] = useState("中文女");
  const [voices, setVoices] = useState<string[]>(["中文女", "中文男"]);
  const [speed, setSpeed] = useState(1.0);
  const [audioUrl, setAudioUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [qos, setQos] = useState<TtsQos | null>(null);
  const playerRef = useRef<StreamingPcmPlayer | null>(null);

  useEffect(() => {
    fetchModels()
      .then((m) => {
        if (m.tts[0]?.languages?.length) {
          setVoices(m.tts[0].languages);
          setVoice(m.tts[0].languages[0]);
        }
      })
      .catch(() => {});
  }, []);

  async function onSynthFile() {
    setBusy(true);
    setError("");
    setStatus("合成中...");
    setAudioUrl("");
    setQos(null);
    try {
      const { blob, qos } = await ttsFile(text, voice, speed);
      setAudioUrl(URL.createObjectURL(blob));
      setQos({ ...qos, mode: "file" });
      setStatus("合成完成");
    } catch (err) {
      setError(String(err));
      setStatus("");
    } finally {
      setBusy(false);
    }
  }

  function onSynthStream() {
    setBusy(true);
    setError("");
    setStatus("流式合成中 (边合成边播)...");
    setAudioUrl("");
    setQos(null);
    const player = new StreamingPcmPlayer();
    playerRef.current = player;
    const t0 = performance.now();
    let firstChunk = true;
    let node: string | undefined;
    openTtsStream(
      text,
      voice,
      speed,
      (sr, n) => {
        player.setSampleRate(sr);
        node = n;
      },
      (pcm) => {
        if (firstChunk) {
          setStatus(`首包延迟 ${Math.round(performance.now() - t0)}ms (客户端侧)`);
          firstChunk = false;
        }
        player.push(pcm);
      },
      (serverQos) => {
        setAudioUrl(URL.createObjectURL(player.toWavBlob()));
        if (serverQos) setQos({ ...serverQos, mode: "stream" });
        else if (node) setQos({ node, mode: "stream" });
        setBusy(false);
      },
      (msg) => {
        setError(msg);
        setBusy(false);
      }
    );
  }

  return (
    <div className="panel">
      <label>合成文本</label>
      <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={5000} />

      <div className="row">
        <div>
          <label>音色</label>
          <select value={voice} onChange={(e) => setVoice(e.target.value)}>
            {voices.map((v) => (
              <option key={v} value={v}>{v}</option>
            ))}
          </select>
        </div>
        <div>
          <label>语速 ({speed.toFixed(1)}x) · 仅一次性合成生效</label>
          <input
            type="range"
            min={0.5}
            max={2}
            step={0.1}
            value={speed}
            onChange={(e) => setSpeed(parseFloat(e.target.value))}
            style={{ width: "100%" }}
          />
        </div>
      </div>

      <div className="tts-actions">
        <button className="primary" onClick={onSynthFile} disabled={busy || !text.trim()}>
          一次性合成
        </button>
        <button className="ghost" onClick={onSynthStream} disabled={busy || !text.trim()}>
          流式合成
        </button>
      </div>

      {error && <div className="result" style={{ color: "#f87171" }}>{error}</div>}
      {status && <div className="meta">{status}</div>}
      {qos && (
        <QosBadge
          node={qos.node}
          model={qos.model}
          mode={qos.mode}
          ttfbMs={qos.ttfb_ms}
          processMs={qos.process_ms}
          audioMs={qos.audio_ms}
          rtf={qos.rtf}
        />
      )}
      {audioUrl && (
        <>
          <audio controls src={audioUrl} autoPlay />
          <div className="meta">
            <a href={audioUrl} download="tts.wav" style={{ color: "var(--accent)" }}>
              下载音频
            </a>
          </div>
        </>
      )}
    </div>
  );
}
