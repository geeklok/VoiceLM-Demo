import { useEffect, useRef, useState } from "react";
import { asrFile, fetchModels, openAsrStream, ModelInfo } from "../api/client";
import { MicRecorder, TARGET_SR } from "../audio/recorder";
import { QosBadge } from "../components/QosBadge";

interface AsrQos {
  node?: string;
  model?: string;
  processMs?: number | null;
  audioMs?: number | null;
  rtf?: number | null;
  degraded?: boolean;
  mode?: string;
}

export default function AsrPage() {
  const [language, setLanguage] = useState("auto");
  const [hotwords, setHotwords] = useState("");
  const [text, setText] = useState("");
  const [meta, setMeta] = useState("");
  const [qos, setQos] = useState<AsrQos | null>(null);
  const [busy, setBusy] = useState(false);
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState("");
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useState("");

  const wsRef = useRef<WebSocket | null>(null);
  const recRef = useRef<MicRecorder | null>(null);

  useEffect(() => {
    fetchModels()
      .then((res) => {
        setModels(res.asr);
        const def = res.asr.find((m) => m.default) ?? res.asr[0];
        if (def) setModel(def.name);
      })
      .catch(() => {});
  }, []);

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setError("");
    setText("");
    setMeta("");
    setQos(null);
    try {
      const res = await asrFile(file, language, hotwords, model || undefined);
      setText(res.text);
      setMeta(`音频 ${res.audio_duration_ms}ms`);
      setQos({
        node: res.node,
        model: res.model,
        processMs: res.process_ms,
        audioMs: res.audio_duration_ms,
        rtf: res.rtf,
        degraded: res.degraded,
        mode: "file",
      });
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
      e.target.value = "";
    }
  }

  async function toggleRecord() {
    if (recording) {
      recRef.current?.stop();
      wsRef.current?.send(JSON.stringify({ type: "end" }));
      setRecording(false);
      return;
    }
    setError("");
    setText("");
    setMeta("实时转写中...");
    setQos(null);
    const ws = openAsrStream(
      TARGET_SR,
      language,
      (t, isFinal, node) => {
        setText(t);
        if (node) setQos({ node, mode: "stream" });
        if (isFinal) setMeta("转写完成");
      },
      (msg) => setError(msg),
      model || undefined
    );
    wsRef.current = ws;

    const rec = new MicRecorder((pcm) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(pcm.buffer);
    });
    recRef.current = rec;
    try {
      await rec.start();
      setRecording(true);
    } catch (err) {
      setError("无法访问麦克风: " + String(err));
      ws.close();
    }
  }

  return (
    <div className="panel">
      <div className="row">
        <div>
          <label>语言</label>
          <select value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="auto">自动</option>
            <option value="zh">中文</option>
            <option value="en">English</option>
          </select>
        </div>
        {models.length > 1 && (
          <div>
            <label>ASR 模型</label>
            <select value={model} onChange={(e) => setModel(e.target.value)} disabled={recording}>
              {models.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}
                  {m.default ? " (默认)" : ""}
                </option>
              ))}
            </select>
          </div>
        )}
        <div>
          <label>热词 (逗号分隔, 可选)</label>
          <input type="text" value={hotwords} onChange={(e) => setHotwords(e.target.value)} />
        </div>
      </div>

      <label>上传音频文件 (任意格式/采样率)</label>
      <input type="file" accept="audio/*,video/*" onChange={onUpload} disabled={busy || recording} />

      <div style={{ marginTop: 16 }}>
        <button className={`ghost rec ${recording ? "active" : ""}`} onClick={toggleRecord}>
          {recording ? "■ 停止录音" : "● 实时录音转写"}
        </button>
      </div>

      {error && <div className="result" style={{ color: "#f87171" }}>{error}</div>}
      <div className="result">{text || (busy ? "识别中..." : "识别结果将显示在这里")}</div>
      {meta && <div className="meta">{meta}</div>}
      {qos && (
        <QosBadge
          node={qos.node}
          model={qos.model}
          mode={qos.mode}
          processMs={qos.processMs}
          audioMs={qos.audioMs}
          rtf={qos.rtf}
          degraded={qos.degraded}
        />
      )}
    </div>
  );
}
